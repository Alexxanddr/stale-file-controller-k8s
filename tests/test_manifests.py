"""Deployment authorization contracts needed by real Kubernetes API servers."""
from pathlib import Path
import unittest
import yaml


class Authorization(unittest.TestCase):
    def test_websocket_upgrade_has_get_and_synthetic_create_only_in_agent_namespace(self):
        role = yaml.safe_load(Path('deploy/role.yaml').read_text())
        permissions = {verb for rule in role['rules'] if 'pods/exec' in rule['resources']
                       for verb in rule['verbs']}
        self.assertTrue({'get', 'create'} <= permissions)
        self.assertEqual(role['metadata']['namespace'], 'storage-recovery')
        cluster = yaml.safe_load(Path('deploy/clusterrole.yaml').read_text())
        self.assertFalse(any('pods/exec' in rule['resources'] for rule in cluster['rules']))
        self.assertFalse(any('persistentvolumes' in rule['resources'] and
                             set(rule['verbs']) - {'get', 'list', 'watch'} for rule in cluster['rules']))
