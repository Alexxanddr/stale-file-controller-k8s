import datetime as dt
import errno
import json
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, mock_open
from kubernetes.client.exceptions import ApiException
from stale.controller import API, Recovery
from stale.probe import supervise
from test_recovery import Fake


class Safety(unittest.TestCase):
    def api(self):
        api = object.__new__(API)
        api.identity = 'this-worker'
        api.coord = MagicMock()
        api.core = MagicMock()
        return api

    def test_unexpired_lease_prevents_second_worker(self):
        api = self.api()
        api.coord.read_namespaced_lease.return_value = SimpleNamespace(spec=SimpleNamespace(
            holder_identity='other-worker', renew_time=dt.datetime.now(dt.timezone.utc),
            acquire_time=None, lease_duration_seconds=60))
        self.assertFalse(api.lease())
        api.coord.replace_namespaced_lease.assert_not_called()

    def test_expired_lease_resource_version_conflict(self):
        api = self.api()
        lease = SimpleNamespace(metadata=SimpleNamespace(resource_version='10'), spec=SimpleNamespace(
            holder_identity='dead-worker', renew_time=dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=120),
            acquire_time=None, lease_duration_seconds=60))
        api.coord.read_namespaced_lease.return_value = lease
        api.coord.replace_namespaced_lease.side_effect = ApiException(status=409)
        self.assertFalse(api.lease())
        self.assertEqual(api.coord.replace_namespaced_lease.call_args.args[2].metadata.resource_version, '10')

    def test_delete_uses_uid_without_force_or_grace_override(self):
        api = self.api()
        api.delete({'metadata': {'name': 'client', 'namespace': 'apps', 'uid': 'old'}})
        self.assertEqual(api.core.delete_namespaced_pod.call_args.kwargs['body'],
                         {'preconditions': {'uid': 'old'}})
        api.core.delete_namespaced_pod.side_effect = ApiException(status=409)
        api.delete({'metadata': {'name': 'client', 'namespace': 'apps', 'uid': 'old'}})

    def test_timeout_is_bounded_and_does_not_wait_on_unkillable_worker(self):
        child = MagicMock(pid=123)
        child.communicate.side_effect = subprocess.TimeoutExpired('probe', 20)
        with patch('stale.probe.Path.glob', return_value=[]), patch('builtins.open', mock_open()), \
                patch('stale.probe.fcntl.flock'), patch('stale.probe.subprocess.Popen', return_value=child), \
                patch('stale.probe.os.killpg') as kill:
            self.assertEqual(supervise({'mode': 'container'})['status'], 'temporary')
        kill.assert_called_once()
        child.wait.assert_not_called()
        self.assertEqual(child.communicate.call_args.kwargs['timeout'], 20)

    def test_worker_cap_refuses_additional_probes(self):
        processes = [MagicMock() for _ in range(4)]
        for process in processes:
            process.read_bytes.return_value = b'python\x00-m\x00stale.probe\x00--worker\x00{}'
        with patch('stale.probe.Path.glob', return_value=processes), patch('builtins.open', mock_open()), \
                patch('stale.probe.fcntl.flock'), patch('stale.probe.subprocess.Popen') as spawn:
            result = supervise({'mode': 'container'})
        self.assertEqual(result['status'], 'temporary')
        spawn.assert_not_called()

    def test_completed_init_is_skipped_without_client_exec(self):
        api = self.api()
        api.agent = MagicMock()
        result = api.probe({'status': {'initContainerStatuses': [
            {'name': 'init', 'state': {'terminated': {'exitCode': 0}}}]}},
            {'container': 'init', 'path': '/data', 'kind': 'nfs'})
        self.assertEqual(result['status'], 'ok')
        api.agent.assert_not_called()

    def test_concurrent_nfs_restart_absorbed_before_gate(self):
        api, state = Fake(), {}
        recovery = Recovery(api)
        for _ in range(3):
            recovery.tick(state)
        self.assertEqual(state['phase'], 'mergerfs-up')
        api.infrastructure['nfs-ganesha'][0]['metadata']['uid'] += '-external'
        api.infrastructure['nfs-ganesha'][0]['status']['containerStatuses'][0]['restartCount'] += 1
        for _ in range(20):
            recovery.tick(state)
            state = json.loads(json.dumps(state))
            if state.get('phase') == 'complete' and not state['active']:
                break
        self.assertFalse(state['active'])
        self.assertEqual([e for e in api.events if e.startswith('restart:')],
                         ['restart:mergerfs', 'restart:nfs-ganesha', 'restart:one', 'restart:two'])

    def test_replacement_stale_blocks_completion_without_redelete(self):
        api, state = Fake(), {}
        recovery = Recovery(api)
        for _ in range(15):
            recovery.tick(state)
            if state.get('phase') == 'clients-up':
                break
        api.bad = 'one'
        count = sum(e.startswith('restart:') for e in api.events)
        for _ in range(3):
            recovery.tick(state)
        self.assertEqual(state['phase'], 'clients-up')
        self.assertTrue(state['active'])
        self.assertEqual(sum(e.startswith('restart:') for e in api.events), count)

    def test_deletion_error_does_not_skip_other_clients(self):
        api, state = Fake(), {}
        recovery = Recovery(api)
        for _ in range(15):
            recovery.tick(state)
            if state.get('snapshotted'):
                break
        original = api.delete
        def delete(pod):
            if pod['metadata']['name'] == 'one':
                raise OSError(errno.ETIMEDOUT, 'API timeout')
            original(pod)
        api.delete = delete
        recovery.tick(state)
        self.assertIn('restart:two', api.events)
        self.assertEqual(state['phase'], 'clients')


if __name__ == '__main__':
    unittest.main()
