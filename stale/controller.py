"""Serialized, checkpointed recovery. All mutations use UID or resourceVersion fences."""
import datetime as dt
import json
import logging
import os
import threading
import time
import uuid
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException
from kubernetes.stream import stream
from stale.model import advance, discover, fingerprint, ready
from stale.settings import Settings

LOG = logging.getLogger('recovery')
NAMESPACE = os.getenv('POD_NAMESPACE', 'storage-recovery')
NAME = 'storage-recovery'


class API:
    def __init__(self, settings):
        self.settings = settings
        config.load_incluster_config()
        configuration = client.Configuration.get_default_copy()
        configuration.retries = 0
        client.Configuration.set_default(configuration)
        self.core = client.CoreV1Api()
        self.coord = client.CoordinationV1Api()
        self.serializer = client.ApiClient()
        self.identity = str(uuid.uuid4())
        self.locked = False

    def plain(self, obj):
        return self.serializer.sanitize_for_serialization(obj)

    def pods(self, namespace=None, selector=None):
        method = self.core.list_namespaced_pod if namespace else self.core.list_pod_for_all_namespaces
        args = (namespace,) if namespace else ()
        return self.plain(method(*args, label_selector=selector, _request_timeout=15))['items']

    def clients(self):
        excluded = {p['metadata']['uid'] for role in ('mergerfs', 'nfs-ganesha') for p in self.infra(role)}
        return discover(self.pods(), self.plain(self.core.list_persistent_volume_claim_for_all_namespaces(
            _request_timeout=15))['items'], self.plain(self.core.list_persistent_volume(
                _request_timeout=15))['items'], NAMESPACE, self.settings.mergerfs_host_path, excluded)

    def infra(self, app):
        return self.pods(self.settings.namespace(app), self.settings.selector(app))

    def load(self):
        cm = self.core.read_namespaced_config_map(NAME, NAMESPACE, _request_timeout=15)
        return json.loads((cm.data or {}).get('state', '{}')), cm.metadata.resource_version

    def save(self, state, version):
        self.core.patch_namespaced_config_map(NAME, NAMESPACE, {
            'metadata': {'resourceVersion': version}, 'data': {'state': json.dumps(state)}},
            _request_timeout=15)

    def delete(self, pod):
        m = pod['metadata']
        LOG.warning('Requesting graceful Pod restart %s/%s uid=%s', m['namespace'], m['name'], m['uid'])
        try:
            self.core.delete_namespaced_pod(m['name'], m['namespace'], body={
                'preconditions': {'uid': m['uid']}}, _request_timeout=15)
        except ApiException as exc:
            if exc.status not in (404, 409):
                raise

    def probe(self, pod, mount):
        statuses = pod.get('status', {}).get('containerStatuses', []) + pod.get('status', {}).get(
            'initContainerStatuses', [])
        status = next((s for s in statuses if s['name'] == mount['container']), {})
        if 'terminated' in status.get('state', {}):
            return {'status': 'ok', 'detail': 'completed init container has no live mount'}
        cid = status.get('containerID')
        if not cid:
            return {'status': 'temporary', 'detail': 'container not running'}
        return self.agent(pod['spec'].get('nodeName'), {'mode': 'container', 'containerID': cid,
                                                      'path': mount['path'], 'kind': mount['kind'],
                                                      'extraPaths': list(self.settings.extra_paths)})

    def agent(self, node, request):
        agents = [p for p in self.pods(NAMESPACE, 'app=storage-probe')
                  if p['spec'].get('nodeName') == node and ready(p)]
        if len(agents) != 1:
            return {'status': 'temporary', 'detail': 'node diagnostic agent unavailable'}
        # Separate client: stream changes the transport of its ApiClient.
        core = client.CoreV1Api(client.ApiClient())
        ws = stream(core.connect_get_namespaced_pod_exec, agents[0]['metadata']['name'], NAMESPACE,
                    container='probe', command=['python', '-m', 'stale.probe', json.dumps(request)],
                    stderr=True, stdout=True, stdin=False, tty=False, _preload_content=False,
                    _request_timeout=15)
        try:
            deadline = time.monotonic() + 30
            output = ''
            while ws.is_open() and time.monotonic() < deadline:
                ws.update(timeout=1)
                if ws.peek_stdout():
                    output += ws.read_stdout()
            if ws.is_open():
                return {'status': 'temporary', 'detail': 'agent transport timeout'}
            return json.loads(output)
        except (ValueError, OSError) as exc:
            return {'status': 'error', 'detail': str(exc)}
        finally:
            ws.close()

    def lease(self):
        now = dt.datetime.now(dt.timezone.utc)
        lease = self.coord.read_namespaced_lease(NAME, NAMESPACE, _request_timeout=10)
        spec = lease.spec
        renewed = spec.renew_time or spec.acquire_time
        if spec.holder_identity not in (None, '', self.identity) and renewed and (
                now - renewed).total_seconds() < (spec.lease_duration_seconds or 60):
            return False
        lease.spec.holder_identity = self.identity
        lease.spec.renew_time = now
        lease.spec.lease_duration_seconds = 60
        try:
            self.coord.replace_namespaced_lease(NAME, NAMESPACE, lease, _request_timeout=10)
            return True
        except ApiException as exc:
            if exc.status == 409:
                return False
            raise

    def keep_lease(self):
        while True:
            time.sleep(10)
            try:
                if not self.lease():
                    raise RuntimeError('lease ownership lost')
            except Exception:
                LOG.exception('Lease renewal failed; stopping before further mutations')
                os._exit(1)


def same_fingerprint(a, b):
    # Normalize tuples across JSON persistence.
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def owner(pod):
    return next((o['uid'] for o in pod['metadata'].get('ownerReferences', []) if o.get('controller')), None)


class Recovery:
    def __init__(self, api):
        self.api = api
        self.settings = getattr(api, 'settings', Settings())

    def infra_up(self, app):
        pods = self.api.infra(app)
        if len(pods) != 1 or not ready(pods[0]):
            return False
        result = self.api.probe(pods[0], {'container': self.settings.container(app),
                                          'path': self.settings.path(app), 'kind': 'fuse'})
        if result['status'] != 'ok':
            LOG.warning('%s filesystem check: %s', app, result)
            return False
        if app == 'nfs-ganesha':
            result = self.api.agent(pods[0]['spec']['nodeName'], {'mode': 'export', 'server': self.settings.nfs_server,
                           'exportPath': self.settings.nfs_export_path,
                           'extraPaths': list(self.settings.extra_paths)})
            if result['status'] != 'ok':
                LOG.warning('NFS export check: %s', result)
                return False
        return True

    def signature(self):
        return {a: fingerprint(self.api.infra(a)) for a in ('mergerfs', 'nfs-ganesha')}

    def start(self, state, reason):
        LOG.warning('Starting recovery: %s', reason)
        state.update(active=True, phase='mergerfs', started=time.time(), reason=reason,
                     requests={}, expected={}, targets=[], failures=0, snapshotted=False)

    def restart_infra(self, state, app):
        # Checkpoint intent before deleting; a retry only ever deletes the same UID.
        requests = state['requests']
        if app not in requests:
            requests[app] = [dict(metadata={k: p['metadata'][k] for k in ('name', 'namespace', 'uid')},
                                  restart=not p['metadata'].get('deletionTimestamp') and
                                  p.get('status', {}).get('phase') == 'Running' and
                                  not any('waiting' in c.get('state', {}) or 'terminated' in c.get('state', {})
                                          for c in p.get('status', {}).get('containerStatuses', [])))
                             for p in self.api.infra(app)]
            return
        for pod in requests[app]:
            if pod['restart']:
                self.api.delete(pod)
        advance(state)

    def tick(self, state):
        configuration = self.settings.identity()
        if state.get('configuration', configuration) != configuration or (
                state.get('active') and 'configuration' not in state):
            self.start(state, 'storage configuration changed')
        state['configuration'] = configuration
        signature = self.signature()
        if not state.get('active'):
            reason = None
            if not same_fingerprint(signature, state.get('baseline')):
                reason = 'infrastructure identity/restart changed (or initial startup)'
            elif not all(self.infra_up(a) for a in ('mergerfs', 'nfs-ganesha')):
                reason = 'infrastructure filesystem unavailable'
            else:
                temporary = False
                for entry in self.api.clients():
                    for mount in entry['mounts']:
                        result = self.api.probe(entry['pod'], mount)
                        if result['status'] == 'stale':
                            reason = 'ESTALE on ' + entry['pod']['metadata']['name'] + ':' + mount['path']
                        elif result['status'] == 'temporary':
                            temporary = True
                        elif result['status'] != 'ok':
                            LOG.error('Client diagnostic requires attention: %s %s %s',
                                      entry['pod']['metadata']['name'], mount, result)
                state['failures'] = state.get('failures', 0) + 1 if temporary else 0
                if state['failures'] >= self.settings.transient_threshold:
                    reason = 'client filesystem unavailable for configured consecutive scans'
            if reason:
                self.start(state, reason)
            return
        phase = state['phase']
        LOG.info('Recovery phase=%s elapsed=%ss', phase, int(time.time() - state['started']))
        # A later infrastructure failure invalidates downstream work. Deliberate
        # restart identities are captured only after their filesystem checks pass.
        for app, expected in state['expected'].items():
            if not same_fingerprint(signature[app], expected):
                self.start(state, app + ' changed during recovery')
                return
        if phase in ('nfs', 'nfs-up', 'clients', 'clients-up', 'complete'):
            if not self.infra_up('mergerfs'):
                self.start(state, 'MergerFS regressed during recovery')
                return
        if phase in ('clients', 'clients-up', 'complete') and not self.infra_up('nfs-ganesha'):
            self.start(state, 'NFS regressed during recovery')
            return
        if phase == 'mergerfs':
            self.restart_infra(state, 'mergerfs')
        elif phase == 'mergerfs-up':
            if self.restarted_up(state, 'mergerfs'):
                state['expected']['mergerfs'] = signature['mergerfs']
                advance(state)
        elif phase == 'nfs':
            # Always restart Ganesha after MergerFS is UP, even if Ganesha had
            # already restarted concurrently with the original failure.
            self.restart_infra(state, 'nfs-ganesha')
        elif phase == 'nfs-up':
            if self.restarted_up(state, 'nfs-ganesha'):
                state['expected']['nfs-ganesha'] = signature['nfs-ganesha']
                advance(state)
        elif phase == 'clients':
            if not state.get('snapshotted'):
                state['targets'] = [dict(pod={'metadata': {k: e['pod']['metadata'][k]
                        for k in ('name', 'namespace', 'uid', 'ownerReferences')
                        if k in e['pod']['metadata']}}) for e in self.api.clients()]
                LOG.info('Discovered %s clients for restart', len(state['targets']))
                state['snapshotted'] = True
                return
            deletion_failed = False
            for entry in state['targets']:
                pod = entry['pod']
                if not owner(pod):
                    LOG.error('Unmanaged pod %s/%s cannot be recreated; manual action required',
                              pod['metadata']['namespace'], pod['metadata']['name'])
                    continue
                try:
                    self.api.delete(pod)
                except Exception:
                    deletion_failed = True
                    LOG.exception('Client restart request failed; continuing with remaining clients')
            if not deletion_failed:
                advance(state)
        elif phase == 'clients-up':
            current = self.api.clients()
            old_uids = {e['pod']['metadata']['uid'] for e in state['targets']}
            for entry in state['targets']:
                pod = entry['pod']
                replacements = [e for e in current if owner(e['pod']) == owner(pod) and
                                e['pod']['metadata']['namespace'] == pod['metadata']['namespace'] and
                                e['pod']['metadata']['uid'] not in old_uids]
                # Owner UID for ReplicaSet/DaemonSet, stable name for StatefulSet.
                if any(o['kind'] == 'StatefulSet' for o in pod['metadata'].get('ownerReferences', [])):
                    replacements = [e for e in replacements if e['pod']['metadata']['name'] == pod['metadata']['name']]
                required = sum(owner(e['pod']) == owner(pod) and
                               e['pod']['metadata']['namespace'] == pod['metadata']['namespace']
                               for e in state['targets'])
                is_sts = any(o['kind'] == 'StatefulSet' for o in pod['metadata'].get('ownerReferences', []))
                if not owner(pod) or len(replacements) < (1 if is_sts else required):
                    return
            if any(e['pod']['metadata']['uid'] in old_uids for e in current):
                return
            if any(not ready(e['pod']) for e in current):
                return
            for entry in current:
                for mount in entry['mounts']:
                    result = self.api.probe(entry['pod'], mount)
                    if result['status'] != 'ok':
                        LOG.warning('Replacement mount not healthy: %s %s %s',
                                    entry['pod']['metadata']['name'], mount, result)
                        return
                    LOG.info('Client mount verified %s/%s %s (%s)',
                             entry['pod']['metadata']['namespace'], entry['pod']['metadata']['name'],
                             mount['path'], mount['kind'])
            advance(state)
        elif phase == 'complete':
            state.update(active=False, baseline=signature, failures=0)
            LOG.info('Recovery complete: infrastructure and all current client mounts verified')

    def restarted_up(self, state, app):
        pods = self.api.infra(app)
        # If a pod was already restarting/unready, wait for it instead of deleting.
        deleted = {p['metadata']['uid'] for p in state['requests'].get(app, []) if p['restart']}
        return not any(p['metadata']['uid'] in deleted for p in pods) and self.infra_up(app)


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    settings = Settings.from_env()
    LOG.info('Configuration: storage namespace=%s mergerfs=%s nfs=%s export=%s',
             settings.storage_namespace, settings.mergerfs_selector, settings.nfs_selector, settings.nfs_export_path)
    api = API(settings)
    while not api.lease():
        time.sleep(10)
    threading.Thread(target=api.keep_lease, daemon=True).start()
    recovery = Recovery(api)
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Health(BaseHTTPRequestHandler):
        state = {}

        def do_GET(self):
            state = Health.state
            blocked = state.get('active') and time.time() - state.get('started', time.time()) > settings.blocked_after
            self.send_response(503 if blocked else 200)
            self.end_headers()
            self.wfile.write(json.dumps({'active': state.get('active', False),
                                        'phase': state.get('phase', 'initializing'),
                                        'blocked': bool(blocked)}).encode())

        def log_message(self, *args):
            pass

    threading.Thread(target=HTTPServer(('0.0.0.0', 8080), Health).serve_forever, daemon=True).start()
    delay = 5
    while True:
        try:
            state, version = api.load()
            recovery.tick(state)
            api.save(state, version)
            Health.state = state
            if state.get('active') and time.time() - state['started'] > settings.blocked_after:
                LOG.error('Recovery exceeded configured deadline in %s; no forced deletion; inspect pod events and node I/O',
                          state['phase'])
            delay = settings.recovery_interval if state.get('active') else settings.idle_interval
        except Exception:
            LOG.exception('Recovery operation failed; retaining checkpoint and retrying')
            delay = min(delay * 2, 60)
        time.sleep(delay)


if __name__ == '__main__':
    main()
