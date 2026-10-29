"""Runs in a privileged, hostPID agent. No binaries from the target image are used."""
import ctypes
import errno
import fcntl
import json
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path
from stale.model import classify


def unescape(value):
    for a, b in ((r'\040', ' '), (r'\011', '\t'), (r'\012', '\n'), (r'\134', '\\')):
        value = value.replace(a, b)
    return value


def mount_record(text, path):
    matches = []
    for line in text.splitlines():
        left, right = line.split(' - ', 1)
        fields = left.split()
        target = unescape(fields[4])
        if path == target or path.startswith(target.rstrip('/') + '/'):
            matches.append((len(target), fields[0], right.split()[0]))
    return max(matches, default=(0, 'missing', 'missing'))


def mount_type(text, path):
    return mount_record(text, path)[2]


def read_directory(path, extra_paths=(), mountinfo=None):
    root_id = mount_record(mountinfo, path)[1] if mountinfo else None

    def same_mount(candidate):
        return mountinfo is None or mount_record(mountinfo, candidate)[1] == root_id

    os.stat(path)
    os.statvfs(path)
    with os.scandir(path) as entries:
        for i, entry in enumerate(entries):
            if same_mount(entry.path):
                # Never traverse a symlink into an unrelated filesystem.
                entry.stat(follow_symlinks=False)
            if i >= 127:
                break
    for relative in extra_paths:
        if not relative or relative.startswith('/') or '..' in relative.split('/'):
            raise ValueError('extra path must remain below the mount')
        candidate = path.rstrip('/')
        try:
            for part in relative.split('/'):
                candidate += '/' + part
                if not same_mount(candidate) or stat.S_ISLNK(os.lstat(candidate).st_mode):
                    break
            else:
                os.statvfs(candidate)
        except FileNotFoundError:
            # Optional application paths need not exist on every mounted share.
            pass


def worker(request):
    if request['mode'] == 'export':
        # Private mount namespace, destroyed on exit. Never touch kubelet mounts.
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.unshare(0x00020000) != 0:
            raise OSError(ctypes.get_errno(), 'unshare')
        subprocess.run(['mount', '--make-rprivate', '/'], check=True, timeout=5)
        os.makedirs('/tmp/nfs-check', exist_ok=True)
        subprocess.run(['mount', '-t', 'nfs', '-o',
                        'ro,vers=4.1,proto=tcp,soft,timeo=10,retrans=1',
                        request['server'] + ':' + request['exportPath'], '/tmp/nfs-check'],
                       check=True, timeout=12, capture_output=True)
        read_directory('/tmp/nfs-check', request.get('extraPaths', ()),
                       Path('/proc/self/mountinfo').read_text())
        return
    cid = request['containerID'].split('://')[-1]
    if len(cid) < 12 or not all(c in '0123456789abcdef' for c in cid):
        raise ValueError('invalid container ID')
    target = None
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            if cid in (proc / 'cgroup').read_text():
                target = proc
                break
        except (FileNotFoundError, ProcessLookupError):
            continue
    if target is None:
        raise OSError(errno.EAGAIN, 'container process unavailable')
    mountinfo = (target / 'mountinfo').read_text()
    fs = mount_type(mountinfo, request['path'])
    expected = request['kind']
    if not (fs.startswith('fuse') if expected == 'fuse' else fs in ('nfs', 'nfs4')):
        raise OSError(errno.ENOTCONN, 'expected ' + expected + ', found ' + fs)
    # Open root before setns: the target's /proc may use a different PID namespace
    # (or be absent entirely in a shell-less/chrooted client).
    root_fd = os.open(target / 'root', os.O_RDONLY | os.O_DIRECTORY)
    fd = os.open(target / 'ns/mnt', os.O_RDONLY)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.setns(fd, 0) != 0:
        raise OSError(ctypes.get_errno(), 'setns')
    os.close(fd)
    os.fchdir(root_fd)
    os.chroot('.')
    os.close(root_fd)
    os.chdir('/')
    read_directory(request['path'], request.get('extraPaths', ()), mountinfo)


def supervise(request):
    # flock plus /proc cap prevents a hard NFS mount from accumulating unlimited
    # unkillable D-state diagnostic processes across repeated exec sessions.
    with open('/tmp/probe.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        active = 0
        for proc in Path('/proc').glob('[0-9]*/cmdline'):
            try:
                if b'stale.probe\x00--worker' in proc.read_bytes():
                    active += 1
            except (OSError, ProcessLookupError):
                pass
        if active >= 4:
            return {'status': 'temporary', 'detail': 'diagnostic worker cap reached; inspect D-state I/O'}
        child = subprocess.Popen([sys.executable, '-m', 'stale.probe', '--worker', json.dumps(request)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        out, err = child.communicate(timeout=20)
        if child.returncode:
            return {'status': 'error', 'detail': err.decode()[-1000:]}
        return json.loads(out)
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGKILL)
        # Do not wait: an uninterruptible kernel I/O cannot be killed yet.
        return {'status': 'temporary', 'detail': 'filesystem operation timed out (possible blocked NFS I/O)'}


def main():
    request = json.loads(sys.argv[-1])
    if '--worker' in sys.argv:
        try:
            worker(request)
            result = {'status': 'ok'}
        except Exception as exc:
            detail = str(exc)
            if isinstance(exc, subprocess.CalledProcessError):
                detail += ': ' + (exc.stderr or b'').decode(errors='replace')[-1000:]
            result = {'status': classify(exc), 'detail': detail}
    else:
        result = supervise(request)
    print(json.dumps(result), flush=True)


if __name__ == '__main__':
    main()
