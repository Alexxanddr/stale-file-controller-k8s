import os
import unittest
from unittest.mock import MagicMock, patch
from stale.controller import API, Recovery
from stale.model import discover
from stale.settings import Settings
from test_recovery import Fake, consumer


class Configuration(unittest.TestCase):
    def test_required_endpoint_fails_before_cluster_access(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(ValueError, 'NFS_SERVER'):
            Settings.from_env()

    def test_custom_topology_and_intervals(self):
        env = {'NFS_SERVER': 'nfs.example.test', 'STORAGE_NAMESPACE': 'filesystems',
               'MERGERFS_NAMESPACE': 'union', 'NFS_NAMESPACE': 'exports',
               'MERGERFS_SELECTOR': 'component=union', 'NFS_SELECTOR': 'role=server',
               'MERGERFS_CONTAINER': 'union', 'NFS_CONTAINER': 'exports',
               'MERGERFS_PATH': '/union/library', 'NFS_BACKING_PATH': '/srv/library',
               'MERGERFS_HOST_PATH': '/var/mnt/union', 'NFS_EXPORT_PATH': '/library',
               'EXTRA_PATHS': '["incoming", "nested/directory"]',
               'IDLE_INTERVAL': '60', 'RECOVERY_INTERVAL': '10',
               'TRANSIENT_THRESHOLD': '5', 'BLOCKED_AFTER': '1200'}
        with patch.dict(os.environ, env, clear=True):
            settings = Settings.from_env()
        self.assertEqual(settings.namespace('mergerfs'), 'union')
        self.assertEqual(settings.namespace('nfs-ganesha'), 'exports')
        self.assertEqual(settings.extra_paths, ('incoming', 'nested/directory'))
        self.assertEqual(settings.idle_interval, 60)
        api = object.__new__(API)
        api.settings = settings
        api.pods = MagicMock(return_value=[])
        api.infra('mergerfs')
        api.pods.assert_called_with('union', 'component=union')
        api.infra('nfs-ganesha')
        api.pods.assert_called_with('exports', 'role=server')
        api = Fake()
        api.settings = settings
        api.probe = MagicMock(return_value={'status': 'ok'})
        api.agent = MagicMock(return_value={'status': 'ok'})
        self.assertTrue(Recovery(api).infra_up('nfs-ganesha'))
        self.assertEqual(api.probe.call_args.args[1],
                         {'container': 'exports', 'path': '/srv/library', 'kind': 'fuse'})
        request = api.agent.call_args.args[1]
        self.assertEqual(request['exportPath'], '/library')
        self.assertEqual(request['server'], 'nfs.example.test')

    def test_unsafe_or_invalid_settings(self):
        for override in ({'EXTRA_PATHS': '["../escape"]'}, {'EXTRA_PATHS': '["/absolute"]'},
                         {'EXTRA_PATHS': '"torrents"'}, {'IDLE_INTERVAL': '0'},
                         {'MERGERFS_HOST_PATH': '/'}, {'NFS_EXPORT_PATH': '/a/../b'},
                         {'NFS_SERVER': '-invalid'}, {'TRANSIENT_THRESHOLD': '-1'}):
            with self.subTest(override=override), patch.dict(os.environ,
                    {'NFS_SERVER': 'nfs.example.test', **override}, clear=True), self.assertRaises(ValueError):
                Settings.from_env()

    def test_custom_host_root_and_infrastructure_exclusion(self):
        p = consumer()['pod']
        p['spec']['volumes'] = [{'name': 'data', 'hostPath': {'path': '/var/mnt/union/library'}}]
        self.assertEqual(discover([p], [], []), [])
        self.assertEqual(len(discover([p], [], [], host_path='/var/mnt/union')), 1)
        self.assertEqual(discover([p], [], [], host_path='/var/mnt/union',
                                  excluded_uids={p['metadata']['uid']}), [])

    def test_ambiguous_infrastructure_selector_never_deletes_multiple_servers(self):
        import copy
        api, state = Fake(), {}
        duplicate = copy.deepcopy(api.infrastructure['mergerfs'][0])
        duplicate['metadata']['uid'] = 'another-server'
        api.infrastructure['mergerfs'].append(duplicate)
        recovery = Recovery(api)
        for _ in range(4):
            recovery.tick(state)
        self.assertEqual(state['phase'], 'mergerfs')
        self.assertFalse(any(e.startswith('restart:') for e in api.events))

    def test_poll_tuning_does_not_restart_storage(self):
        self.assertEqual(Settings(idle_interval=30).identity(), Settings(idle_interval=60).identity())

    def test_configuration_change_invalidates_inflight_checkpoint(self):
        api = Fake()
        api.settings = Settings(nfs_server='nfs.example.test')
        state = {}
        Recovery(api).tick(state)
        old_identity = state['configuration']
        api.settings = Settings(nfs_server='other.example.test')
        Recovery(api).tick(state)
        self.assertNotEqual(state['configuration'], old_identity)
        self.assertIn('configuration changed', state['reason'])
        self.assertEqual(state['expected'], {})

class FilesystemIsolation(unittest.TestCase):
    def test_extra_path_statfs_detects_the_actual_stale_child(self):
        import errno
        import stat
        from stale.probe import read_directory
        from types import SimpleNamespace
        entries = MagicMock()
        entries.__enter__.return_value = []
        with patch('stale.probe.os.stat'), patch('stale.probe.os.scandir', return_value=entries), \
                patch('stale.probe.os.lstat', return_value=SimpleNamespace(st_mode=stat.S_IFDIR)), \
                patch('stale.probe.os.statvfs', side_effect=[None, OSError(errno.ESTALE, 'Stale file handle')]):
            with self.assertRaises(OSError) as error:
                read_directory('/export/MediaFactory', ('torrents',))
        self.assertEqual(error.exception.errno, errno.ESTALE)

    def test_unrelated_nested_mount_is_not_probed(self):
        from stale.probe import read_directory
        mountinfo = ('1 0 0:1 / /data rw - nfs4 server:/data rw\n'
                     '2 1 0:2 / /data/other rw - nfs4 unrelated:/share rw\n')
        entries = MagicMock()
        entry = MagicMock(path='/data/other')
        entries.__enter__.return_value = [entry]
        with patch('stale.probe.os.stat'), patch('stale.probe.os.scandir', return_value=entries), \
                patch('stale.probe.os.lstat') as lstat, patch('stale.probe.os.statvfs') as statfs:
            read_directory('/data', ('other/child',), mountinfo)
        entry.stat.assert_not_called()
        lstat.assert_not_called()
        statfs.assert_called_once_with('/data')


class CommandLine(unittest.TestCase):
    def test_config_check_does_not_access_kubernetes(self):
        import contextlib
        import io
        import json
        from stale.controller import main
        output = io.StringIO()
        with patch.dict(os.environ, {'NFS_SERVER': 'nfs.example.test'}, clear=True), \
                patch('sys.argv', ['stale-file-controller', '--check-config']), \
                patch('stale.controller.API') as api, contextlib.redirect_stdout(output):
            main()
        api.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())['nfs_server'], 'nfs.example.test')

    def test_help_does_not_require_environment_or_cluster(self):
        import contextlib
        import io
        from stale.controller import main
        with patch.dict(os.environ, {}, clear=True), patch('sys.argv', ['stale-file-controller', '--help']), \
                contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as exit:
            main()
        self.assertEqual(exit.exception.code, 0)


if __name__ == '__main__':
    unittest.main()
