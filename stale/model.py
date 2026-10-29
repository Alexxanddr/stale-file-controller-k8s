"""Pure discovery and recovery state transitions (API objects are plain dictionaries)."""
import errno


def classify(error):
    number = getattr(error, 'errno', None)
    if number == errno.ESTALE or 'stale file handle' in str(error).lower():
        return 'stale'
    if number in (errno.EACCES, errno.EPERM):
        return 'permission'
    if number in (errno.ETIMEDOUT, errno.EIO, errno.ENOTCONN, errno.ECONNREFUSED,
                  errno.EAGAIN, errno.ECONNRESET, errno.EHOSTUNREACH, errno.ENETUNREACH):
        return 'temporary'
    return 'error'


def under(path, root='/run/mergerfs'):
    return path == root or path.startswith(root + '/')


def ready(pod):
    return pod.get('status', {}).get('phase') == 'Running' and not pod['metadata'].get('deletionTimestamp') and any(
        c['type'] == 'Ready' and c['status'] == 'True'
        for c in pod.get('status', {}).get('conditions', []))


def fingerprint(pods):
    return sorted((p['metadata']['uid'], tuple(sorted(
        (s['name'], s.get('restartCount', 0), s.get('containerID', ''))
        for s in p.get('status', {}).get('containerStatuses', [])))) for p in pods)


def discover(pods, claims, volumes, namespace='storage-recovery',
             host_path='/run/mergerfs', excluded_uids=()):
    claims = {(p['metadata']['namespace'], p['metadata']['name']): p for p in claims}
    volumes = {p['metadata']['name']: p for p in volumes}
    found = []
    for pod in pods:
        meta, spec = pod['metadata'], pod['spec']
        if meta['uid'] in excluded_uids or (meta['namespace'] == namespace and
                meta.get('labels', {}).get('app') in ('storage-recovery', 'storage-probe')):
            continue
        if pod.get('status', {}).get('phase') in ('Succeeded', 'Failed'):
            continue
        selected = {}
        for vol in spec.get('volumes', []):
            pv = {}
            claim = vol.get('persistentVolumeClaim')
            if claim:
                pvc = claims.get((meta['namespace'], claim['claimName']), {})
                pv = volumes.get(pvc.get('spec', {}).get('volumeName'), {}).get('spec', {})
            csi = pv.get('csi', vol.get('csi', {}))
            direct = vol.get('hostPath', {}).get('path', pv.get('hostPath', {}).get(
                'path', pv.get('local', {}).get('path', '')))
            if csi.get('driver') == 'nfs.csi.k8s.io' or 'nfs' in pv or 'nfs' in vol:
                selected[vol['name']] = 'nfs'
            elif under(direct, host_path):
                selected[vol['name']] = 'fuse'
        mounts = []
        for container in spec.get('containers', []) + spec.get('initContainers', []):
            for mount in container.get('volumeMounts', []):
                if mount['name'] in selected:
                    mounts.append({'container': container['name'], 'path': mount['mountPath'],
                                   'kind': selected[mount['name']]})
        if mounts:
            found.append({'pod': pod, 'mounts': mounts})
    return found


PHASES = ('mergerfs', 'mergerfs-up', 'nfs', 'nfs-up', 'clients', 'clients-up', 'complete')


def advance(state):
    state['phase'] = PHASES[PHASES.index(state['phase']) + 1]
    return state
