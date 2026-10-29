import copy
import errno
import unittest
from unittest.mock import patch
from stale.model import classify, discover, fingerprint
from stale.controller import Recovery, same_fingerprint
from stale.probe import mount_type, read_directory


def pod(name, namespace='mergerfs', uid=None, app=None):
    return {'metadata': {'name': name, 'namespace': namespace, 'uid': uid or name,
                         'labels': {'app': app or name},
                         'ownerReferences': [{'uid': 'owner-' + name, 'kind': 'ReplicaSet', 'controller': True}]},
            'spec': {'nodeName': 'node1', 'containers': [{'name': name}]},
            'status': {'phase': 'Running', 'conditions': [{'type': 'Ready', 'status': 'True'}],
                       'containerStatuses': [{'name': name, 'restartCount': 0, 'containerID': 'containerd://abc'}]}}


def consumer(name='app', kind='nfs'):
    p = pod(name, 'apps')
    p['spec']['volumes'] = [{'name': 'data', 'persistentVolumeClaim': {'claimName': 'media'}}]
    p['spec']['containers'][0]['volumeMounts'] = [{'name': 'data', 'mountPath': '/export/MediaFactory'}]
    return {'pod': p, 'mounts': [{'container': name, 'path': '/export/MediaFactory', 'kind': kind}]}


class Fake:
    def __init__(self):
        self.infrastructure = {a: [pod(a)] for a in ('mergerfs', 'nfs-ganesha')}
        self.entries = [consumer('one'), consumer('two', 'fuse')]
        self.events = []
        self.bad = None

    def infra(self, app):
        return copy.deepcopy(self.infrastructure[app])

    def clients(self):
        return copy.deepcopy(self.entries)

    def delete(self, p):
        uid = p['metadata']['uid']
        candidates = [p for ps in self.infrastructure.values() for p in ps] + [e['pod'] for e in self.entries]
        for current in candidates:
            if current['metadata']['uid'] == uid:
                self.events.append('restart:' + current['metadata']['name'])
                current['metadata']['uid'] += '-new'
                return

    def probe(self, p, mount):
        self.events.append('check:' + p['metadata']['name'])
        return {'status': 'stale' if self.bad == p['metadata']['name'] else 'ok'}

    def agent(self, node, request):
        self.events.append('check:export')
        return {'status': 'ok'}


class Discovery(unittest.TestCase):
    def test_csi_claim_binding_and_local_pv(self):
        p = consumer()['pod']
        pvc = {'metadata': {'name': 'media', 'namespace': 'apps'}, 'spec': {'volumeName': 'pv'}}
        for spec, expected in (({'csi': {'driver': 'nfs.csi.k8s.io'}}, 'nfs'),
                               ({'local': {'path': '/run/mergerfs/MediaFactory/media'}}, 'fuse'),
                               ({'hostPath': {'path': '/run/mergerfs'}}, 'fuse')):
            result = discover([p], [pvc], [{'metadata': {'name': 'pv'}, 'spec': spec}])
            self.assertEqual(result[0]['mounts'][0]['kind'], expected)
        pvc['metadata']['namespace'] = 'wrong'
        self.assertEqual(discover([p], [pvc], []), [])

    def test_direct_hostpath_exclusions_and_path_boundary(self):
        p = consumer()['pod']
        p['spec']['volumes'] = [{'name': 'data', 'hostPath': {'path': '/run/mergerfs'}}]
        self.assertEqual(len(discover([p], [], [])), 1)
        for ns, app in [('mergerfs', 'mergerfs'), ('mergerfs', 'nfs-ganesha'), ('storage-recovery', 'x')]:
            p['metadata'].update(namespace=ns, labels={'app': app})
            self.assertEqual(discover([p], [], []), [])
        p['metadata']['namespace'] = 'apps'
        p['spec']['volumes'][0]['hostPath']['path'] = '/run/mergerfs-other'
        self.assertEqual(discover([p], [], []), [])

    def test_all_containers_subpaths_and_init_mounts(self):
        p = consumer()['pod']
        p['spec']['volumes'] = [{'name': 'data', 'nfs': {'server': 'x', 'path': '/MediaFactory'}}]
        p['spec']['containers'][0]['volumeMounts'][0]['subPath'] = 'torrents'
        p['spec']['initContainers'] = [{'name': 'init', 'volumeMounts': [{'name': 'data', 'mountPath': '/input'}]}]
        self.assertEqual([m['path'] for m in discover([p], [], [])[0]['mounts']],
                         ['/export/MediaFactory', '/input'])


class Probe(unittest.TestCase):
    def test_estale_not_permissions_or_transient(self):
        self.assertEqual(classify(OSError(errno.ESTALE, 'stale')), 'stale')
        self.assertEqual(classify('df: /export/MediaFactory/torrents: Stale file handle'), 'stale')
        self.assertEqual(classify(OSError(errno.EACCES, 'no access')), 'permission')
        self.assertEqual(classify(OSError(errno.ETIMEDOUT, 'timeout')), 'temporary')
        self.assertEqual(classify(OSError(errno.EIO, 'io')), 'temporary')

    def test_mount_namespace_longest_match_not_unrelated_mount(self):
        mounts = ('1 0 0:1 / / rw - overlay overlay rw\n'
                  '2 1 0:2 / /export/MediaFactory rw - nfs4 server:/MediaFactory rw\n'
                  '3 1 0:3 / /unrelated rw - fuse.mergerfs mergerfs rw\n')
        self.assertEqual(mount_type(mounts, '/export/MediaFactory/torrents'), 'nfs4')
        self.assertEqual(mount_type(mounts, '/unrelated/media'), 'fuse.mergerfs')
        self.assertEqual(mount_type(mounts, '/export/MediaFactory-other'), 'overlay')

    def test_real_torrents_error_propagates(self):
        with patch('stale.probe.os.stat', side_effect=OSError(errno.ESTALE, 'Stale file handle')):
            with self.assertRaises(OSError) as ctx:
                read_directory('/export/MediaFactory/torrents')
        self.assertEqual(classify(ctx.exception), 'stale')


class Workflow(unittest.TestCase):
    def run_cycle(self, api, state):
        recovery = Recovery(api)
        for _ in range(20):
            recovery.tick(state)
            # Simulate checkpoint round trip and controller restart each tick.
            import json
            state = json.loads(json.dumps(state))
            recovery = Recovery(api)
            if state.get('phase') == 'complete' and not state.get('active'):
                return state
        self.fail('cycle did not finish: ' + str(state))

    def test_order_and_deduplicate_both_infra_restarts(self):
        api = Fake()
        state = self.run_cycle(api, {})
        events = api.events
        self.assertLess(events.index('restart:mergerfs'), events.index('check:mergerfs'))
        self.assertLess(events.index('check:mergerfs'), events.index('restart:nfs-ganesha'))
        self.assertLess(events.index('restart:nfs-ganesha'), events.index('check:export'))
        self.assertLess(events.index('check:export'), events.index('restart:one'))
        self.assertEqual(sum(e.startswith('restart:') for e in events), 4)
        Recovery(api).tick(state)
        self.assertFalse(state['active'])
        self.assertEqual(sum(e.startswith('restart:') for e in events), 4)

    def test_nfs_failure_still_restarts_mergerfs_first(self):
        api = Fake()
        state = {'baseline': Recovery(api).signature()}
        api.infrastructure['nfs-ganesha'][0]['status']['containerStatuses'][0]['restartCount'] += 1
        self.run_cycle(api, state)
        self.assertEqual([e for e in api.events if e.startswith('restart:')][:2],
                         ['restart:mergerfs', 'restart:nfs-ganesha'])

    def test_wait_running_restart_and_no_clients_before_export_up(self):
        api = Fake()
        api.infrastructure['mergerfs'][0]['status']['phase'] = 'Pending'
        recovery, state = Recovery(api), {}
        for _ in range(5):
            recovery.tick(state)
        self.assertEqual(state['phase'], 'mergerfs-up')
        self.assertFalse(any(e.startswith('restart:') for e in api.events))
        api.infrastructure['mergerfs'][0]['status']['phase'] = 'Running'
        api.bad = 'nfs-ganesha'
        for _ in range(6):
            recovery.tick(state)
        self.assertEqual(state['phase'], 'nfs-up')
        self.assertNotIn('restart:one', api.events)

    def test_stale_triggers_permission_does_not(self):
        api = Fake()
        recovery = Recovery(api)
        state = {'baseline': recovery.signature()}
        api.bad = 'one'
        recovery.tick(state)
        self.assertTrue(state['active'])
        self.assertIn('ESTALE', state['reason'])
        api.bad = None
        api.probe = lambda p, m: {'status': 'permission'} if p['metadata']['name'] == 'one' else {'status': 'ok'}
        state = {'baseline': recovery.signature()}
        recovery.tick(state)
        self.assertFalse(state.get('active', False))

    def test_infra_changes_during_clients_invalidates_downstream(self):
        api = Fake()
        recovery, state = Recovery(api), {}
        for _ in range(15):
            recovery.tick(state)
            if state.get('phase') == 'clients':
                break
        api.infrastructure['mergerfs'][0]['status']['containerStatuses'][0]['restartCount'] += 1
        recovery.tick(state)
        self.assertEqual(state['phase'], 'mergerfs')
        self.assertFalse(state['snapshotted'])
        self.assertNotIn('restart:one', api.events)

    def test_checkpoint_has_no_container_environment(self):
        api = Fake()
        api.entries[0]['pod']['spec']['containers'][0]['env'] = [{'name': 'password', 'value': 'private'}]
        recovery, state = Recovery(api), {}
        for _ in range(15):
            recovery.tick(state)
            if state.get('snapshotted'):
                break
        import json
        self.assertNotIn('private', json.dumps(state))

    def test_fingerprint_survives_json_roundtrip(self):
        import json
        sig = fingerprint([pod('mergerfs')])
        self.assertTrue(same_fingerprint(sig, json.loads(json.dumps(sig))))


if __name__ == '__main__':
    unittest.main()
