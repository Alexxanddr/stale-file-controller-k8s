"""Environment configuration shared by discovery, orchestration and diagnostics."""
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
import posixpath


@dataclass(frozen=True)
class Settings:
    storage_namespace: str = 'storage-system'
    mergerfs_namespace: str = ''
    nfs_namespace: str = ''
    mergerfs_selector: str = 'app=mergerfs'
    nfs_selector: str = 'app=nfs-ganesha'
    mergerfs_container: str = 'mergerfs'
    nfs_container: str = 'nfs-ganesha'
    mergerfs_path: str = '/mnt/merged/data'
    nfs_backing_path: str = '/export/data'
    mergerfs_host_path: str = '/run/mergerfs'
    nfs_server: str = ''
    nfs_export_path: str = '/data'
    extra_paths: tuple = ()
    idle_interval: float = 30
    recovery_interval: float = 5
    blocked_after: float = 900
    transient_threshold: int = 3

    @classmethod
    def from_env(cls):
        defaults = cls()
        values = {}
        for key in asdict(defaults):
            value = os.getenv(key.upper())
            if value is None:
                continue
            if key == 'extra_paths':
                value = json.loads(value)
                if not isinstance(value, list) or not all(isinstance(p, str) for p in value):
                    raise ValueError('EXTRA_PATHS must be a JSON array of relative paths')
                value = tuple(value)
            elif key in ('idle_interval', 'recovery_interval', 'blocked_after'):
                value = float(value)
            elif key == 'transient_threshold':
                value = int(value)
            values[key] = value
        result = cls(**values)
        result.validate()
        return result

    def validate(self):
        if not self.nfs_server or any(c.isspace() for c in self.nfs_server) or self.nfs_server.startswith('-'):
            raise ValueError('NFS_SERVER is required and must be a host/IP without whitespace')
        for key in ('mergerfs_path', 'nfs_backing_path', 'mergerfs_host_path', 'nfs_export_path'):
            value = getattr(self, key)
            if not value.startswith('/') or posixpath.normpath(value) != value or value == '/':
                raise ValueError(key.upper() + ' must be a normalized absolute non-root path')
        for path in self.extra_paths:
            if not path or path.startswith('/') or '..' in path.split('/'):
                raise ValueError('EXTRA_PATHS entries must stay below the mounted path')
        if not all((self.storage_namespace, self.mergerfs_selector, self.nfs_selector,
                    self.mergerfs_container, self.nfs_container)):
            raise ValueError('Storage namespace, selectors and container names must not be empty')
        intervals = (self.idle_interval, self.recovery_interval, self.blocked_after, self.transient_threshold)
        if not all(math.isfinite(value) and value > 0 for value in intervals):
            raise ValueError('Intervals and thresholds must be positive')

    def identity(self):
        topology = asdict(self)
        for key in ('idle_interval', 'recovery_interval', 'blocked_after', 'transient_threshold'):
            topology.pop(key)
        return hashlib.sha256(json.dumps(topology, sort_keys=True).encode()).hexdigest()

    def namespace(self, role):
        specific = self.mergerfs_namespace if role == 'mergerfs' else self.nfs_namespace
        return specific or self.storage_namespace

    def selector(self, role):
        return self.mergerfs_selector if role == 'mergerfs' else self.nfs_selector

    def container(self, role):
        return self.mergerfs_container if role == 'mergerfs' else self.nfs_container

    def path(self, role):
        return self.mergerfs_path if role == 'mergerfs' else self.nfs_backing_path
