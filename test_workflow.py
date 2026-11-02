"""Workflow tests: state checkpoints, confirmation and safe launch recovery."""
import hashlib
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import workflow


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.models = Path(self.tmp.name) / 'models'
        self.state = self.models / '.vast'
        self.models_patch = patch.object(workflow, 'MODELS', self.models)
        self.state_patch = patch.object(workflow, 'STATE', self.state)
        self.models_patch.start(); self.state_patch.start()
        self.addCleanup(self.models_patch.stop); self.addCleanup(self.state_patch.stop)
        (self.models / 'qwen' / 'pack').mkdir(parents=True)
        (self.models / 'qwen' / 'pack' / 'index.txt').write_bytes(b'abc')
        self.md5 = hashlib.md5(b'abc').hexdigest() + '  pack/index.txt\n'

    def register(self):
        def run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, stdout=self.md5 if 'md5sum' in argv else '')
        with patch('builtins.input', return_value='yes'), patch.object(workflow.subprocess, 'run', side_effect=run):
            workflow.upload('qwen', 'gdrive_vast')

    def test_register_existing_drive_model_and_reject_unsafe_paths(self):
        def run(argv, **kwargs):
            if 'size' in argv:
                return subprocess.CompletedProcess(argv, 0, stdout='{"bytes": 3, "count": 1}')
            return subprocess.CompletedProcess(argv, 0, stdout=self.md5)
        with patch.object(workflow.subprocess, 'run', side_effect=run):
            workflow.register('flash', 'drive', 'models/flash')
        data = workflow.read('flash')
        self.assertEqual((data['drive_path'], data['format']), ('/models/flash', 'strata-flash'))
        with self.assertRaisesRegex(ValueError, 'relative'):
            workflow.register('bad', 'drive', '../private')

    def test_upload_and_prepare_persist_confirmed_stages(self):
        self.register()
        self.assertEqual(workflow.read('qwen')['drive_path'], '/models/qwen')
        self.assertEqual((self.state / 'qwen.md5').read_text(), self.md5)
        api = Mock(); api.client = Mock()
        api.client.post.return_value.json.return_value = {'offers': [{'machine_id': 42, 'inet_down_cost': .001}]}
        with patch.object(workflow.vast, 'client', return_value=api), \
             patch.object(workflow.vast, 'volume_offer', return_value={'id': 123, 'storage_total_cost': .04}), \
             patch.object(workflow.storage, 'create_volume', return_value={'success': True, 'volume_id': 99}), \
             patch.object(workflow.storage, 'show_volumes', return_value=[{'id': 99, 'status': 'created'}]), \
             patch.object(workflow.storage, 'copy', return_value={'success': True, 'copy_id': 4, 'result_url': 'https://example.invalid/private'}), \
             patch('builtins.input', return_value='yes'):
            workflow.prepare('qwen', 42, 20, 7)
        data = workflow.read('qwen')
        self.assertEqual(data['volume']['id'], 99)
        self.assertEqual(data['copy']['state'], 'submitted')
        self.assertEqual((self.state / 'qwen.json').stat().st_mode & 0o777, 0o600)
        with self.assertRaisesRegex(ValueError, 'already exists'):
            workflow.prepare('qwen', 42, 20, 7)

    def test_filters_incompatible_gpu_offers(self):
        good = {'cpu_arch': 'amd64', 'driver_version': '595.91', 'cpu_ram': 65000,
                'gpu_ram': 32000, 'compute_cap': 1200}
        self.assertTrue(workflow.compatible(good))
        for field, value in [('cpu_arch', 'arm64'), ('driver_version', '570.1'),
                             ('cpu_ram', 32000), ('gpu_ram', 8000), ('compute_cap', 900)]:
            with self.subTest(field=field):
                self.assertFalse(workflow.compatible({**good, field: value}))

    def test_rejected_name_and_uncertain_http_timeout_are_distinct(self):
        import requests
        self.register()
        api = Mock(); api.client = Mock()
        api.client.post.return_value.json.return_value = {'offers': [{'machine_id': 42, 'inet_down_cost': .001}]}
        error = requests.HTTPError(response=Mock(status_code=422, json=Mock(return_value={'error': 'invalid_args'})))
        with patch.object(workflow.vast, 'client', return_value=api), \
             patch.object(workflow.vast, 'volume_offer', return_value={'id': 123, 'storage_total_cost': .04}), \
             patch.object(workflow.storage, 'create_volume', side_effect=error), patch('builtins.input', return_value='yes'):
            with self.assertRaisesRegex(RuntimeError, 'rejected volume creation'):
                workflow.prepare('qwen', 42, 20, 7)
        self.assertNotIn('volume_pending', workflow.read('qwen'))
        error = requests.HTTPError(response=Mock(status_code=408))
        with patch.object(workflow.vast, 'client', return_value=api), \
             patch.object(workflow.vast, 'volume_offer', return_value={'id': 123, 'storage_total_cost': .04}), \
             patch.object(workflow.storage, 'create_volume', side_effect=error), patch('builtins.input', return_value='yes'):
            with self.assertRaisesRegex(RuntimeError, 'outcome unknown'):
                workflow.prepare('qwen', 42, 20, 7)
        self.assertIn('volume_pending', workflow.read('qwen'))

    def test_dense_gguf_is_rejected_before_paid_volume(self):
        self.state.mkdir(parents=True)
        workflow.save(self.state / 'dense.json', {'format': 'gguf', 'bytes': 1})
        with patch.object(workflow.vast, 'client') as client:
            with self.assertRaisesRegex(ValueError, 'not standalone GGUF'):
                workflow.prepare('dense', 42, 20, 7)
            client.assert_not_called()

    def test_stage_derives_volume_size_from_model(self):
        self.register()
        api = Mock(); api.client = Mock()
        api.client.post.return_value.json.return_value = {'offers': [{'machine_id': 42, 'inet_down_cost': .001}]}
        with patch.object(workflow.vast, 'client', return_value=api), \
             patch.object(workflow.vast, 'volume_offer', return_value={'id': 123, 'storage_total_cost': .04}) as offers, \
             patch.object(workflow.storage, 'create_volume', return_value={'success': True, 'volume_id': 99}), \
             patch.object(workflow.storage, 'show_volumes', return_value=[{'id': 99, 'status': 'created'}]), \
             patch.object(workflow.storage, 'copy', return_value={'success': True, 'copy_id': 4}), \
             patch('builtins.input', return_value='yes'):
            workflow.prepare('qwen', 42, None, 7)
        self.assertEqual(offers.call_args.args[-1], 1)
        self.assertEqual(workflow.read('qwen')['volume']['size_gb'], 1)

    def test_multiple_drive_connections_require_explicit_choice(self):
        self.register()
        api = Mock(); api.client = Mock()
        with patch.object(workflow.vast, 'client', return_value=api), \
             patch.object(workflow.storage, 'show_connections', return_value=[
                 {'id': 1, 'cloud_type': 'drive'}, {'id': 2, 'cloud_type': 'drive'}]), \
             patch.object(workflow.storage, 'create_volume') as create:
            with self.assertRaisesRegex(ValueError, 'Expected one Google Drive connection'):
                workflow.prepare('qwen', 42, 20, None)
        create.assert_not_called()

    def test_unknown_creation_blocks_paid_retry(self):
        self.register()
        api = Mock(); api.client = Mock()
        api.client.post.return_value.json.return_value = {'offers': [{'machine_id': 42, 'inet_down_cost': .001}]}
        with patch.object(workflow.vast, 'client', return_value=api), \
             patch.object(workflow.vast, 'volume_offer', return_value={'id': 123, 'storage_total_cost': .04}), \
             patch.object(workflow.storage, 'create_volume', side_effect=TimeoutError()), \
             patch('builtins.input', return_value='yes'):
            with self.assertRaisesRegex(RuntimeError, 'outcome unknown'):
                workflow.prepare('qwen', 42, 20, 7)
            with self.assertRaisesRegex(ValueError, 'outcome is unknown'):
                workflow.prepare('qwen', 42, 20, 7)

    def test_reset_volume_requires_it_to_be_gone(self):
        self.register()
        model = workflow.read('qwen'); model['volume'] = {'id': 99, 'label': 'model_abc'}
        workflow.save(self.state / 'qwen.json', model)
        api = Mock(); api.client = Mock()
        with patch.object(workflow.vast, 'client', return_value=api), \
             patch.object(workflow.storage, 'show_volumes', return_value=[{'id': 99, 'label': 'model_abc'}]):
            with self.assertRaisesRegex(ValueError, 'still exists'):
                workflow.reset_volume('qwen')
        with patch.object(workflow.vast, 'client', return_value=api), \
             patch.object(workflow.storage, 'show_volumes', return_value=[]), \
             patch('builtins.input', return_value='RESET'):
            workflow.reset_volume('qwen')
        self.assertIsNone(workflow.read('qwen')['volume'])

    def test_recover_volume_requires_exact_matching_label_and_machine(self):
        self.register()
        m = workflow.read('qwen')
        m['volume_pending'] = {'machine': 42, 'label': 'model-abc', 'size_gb': 20, 'connection': 7}
        workflow.save(self.state / 'qwen.json', m)
        api = Mock(); api.client = Mock()
        with patch.object(workflow.vast, 'client', return_value=api), \
             patch.object(workflow.storage, 'show_volumes', return_value=[{'id': 99, 'machine_id': 42, 'label': 'wrong'}]), \
             patch('builtins.input', return_value='ADOPT'):
            with self.assertRaisesRegex(ValueError, 'does not match'):
                workflow.recover_volume('qwen', 99)
        with patch.object(workflow.vast, 'client', return_value=api), \
             patch.object(workflow.storage, 'show_volumes', return_value=[{'id': 99, 'machine_id': 42, 'label': 'model-abc'}]), \
             patch('builtins.input', return_value='ADOPT'):
            workflow.recover_volume('qwen', 99)
        self.assertEqual(workflow.read('qwen')['volume']['id'], 99)
        self.assertEqual(workflow.read('qwen')['copy']['state'], 'pending')

    def test_recover_rent_never_clears_live_instance(self):
        self.register()
        receipt = self.state / 'qwen.rent.json'
        workflow.save(receipt, {'outcome': 'unknown', 'label': 'vast-unique'})
        with patch.object(workflow.vast, 'client'), \
             patch.object(workflow.vast, 'instances', return_value=[{'id': 777, 'label': 'vast-unique'}]):
            with self.assertRaisesRegex(ValueError, 'instance exists'):
                workflow.recover_rent('qwen')
        self.assertTrue(receipt.exists())
        m = workflow.read('qwen'); m['volume'] = {'id': 99, 'machine': 42}
        workflow.save(self.state / 'qwen.json', m)
        with patch.object(workflow.vast, 'client'), \
             patch.object(workflow.vast, 'instances', return_value=[{'id': 777, 'label': 'vast-unique', 'volume_info': [{'id': 99}]}]), \
             patch('builtins.input', return_value='ADOPT'):
            workflow.recover_rent('qwen', 777)
        self.assertEqual(json.loads(receipt.read_text())['response']['new_contract'], 777)

    def test_parallel_operation_is_rejected(self):
        import fcntl
        self.state.mkdir(parents=True)
        lock = self.state / 'qwen.lock'
        with lock.open('w') as f:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ValueError, 'already running'):
                workflow.upload('qwen', 'gdrive_vast')

    def test_copy_waits_for_volume_and_retries_only_rejected_request(self):
        import requests
        self.register()
        model = workflow.read('qwen'); model['volume'] = {'id': 99, 'machine': 42}
        api = Mock(); api.client = Mock()
        response = Mock(status_code=400, json=Mock(return_value={'error': 'invalid_volume'}))
        with patch.object(workflow.storage, 'show_volumes', side_effect=[
                [{'id': 99, 'status': 'initialized'}], [{'id': 99, 'status': 'created'}]]), \
             patch.object(workflow.storage, 'copy', side_effect=[requests.HTTPError(response=response),
                          {'success': True, 'copy_id': 55}]) as copy, \
             patch.object(workflow.time, 'sleep'):
            workflow.submit_copy('qwen', model, self.state / 'qwen.json', api, 7)
        self.assertEqual(copy.call_count, 2)
        self.assertEqual(workflow.read('qwen')['copy']['id'], 55)

    def test_retry_requires_explicit_confirmation(self):
        self.register()
        m = workflow.read('qwen'); m['volume'] = {'id': 99, 'machine': 42}
        m['copy'] = {'state': 'pending', 'connection': 7}
        workflow.save(self.state / 'qwen.json', m)
        api = Mock(); api.client = Mock()
        with patch.object(workflow.vast, 'client', return_value=api), \
             patch.object(workflow.storage, 'copy', return_value={'success': True, 'copy_id': 10}) as copy, \
             patch('builtins.input', return_value='no'):
            with self.assertRaisesRegex(ValueError, 'Cancelled'):
                workflow.retry_copy('qwen')
            copy.assert_not_called()
        with patch.object(workflow.vast, 'client', return_value=api), \
             patch.object(workflow.storage, 'copy', return_value={'success': True, 'copy_id': 10}), \
             patch.object(workflow.storage, 'show_volumes', return_value=[{'id': 99, 'status': 'created'}]), \
             patch('builtins.input', return_value='RETRY'):
            workflow.retry_copy('qwen')
        self.assertEqual(workflow.read('qwen')['copy']['id'], 10)

    def test_new_launch_requires_both_confirmations_and_verified_files(self):
        self.register()
        m = workflow.read('qwen'); m['volume'] = {'id': 99, 'machine': 42}; m['copy'] = {'state': 'submitted'}
        workflow.save(self.state / 'qwen.json', m)
        api = Mock(); api.client = Mock()
        offer = {'id': 123, 'machine_id': 42, 'num_gpus': 1, 'gpu_name': 'RTX 5090',
                 'cpu_arch': 'amd64', 'driver_version': '595.1', 'cpu_ram': 65000,
                 'gpu_ram': 32000, 'compute_cap': 1200}
        api.client.post.return_value.json.return_value = {'offers': [offer]}
        def run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, stdout='{"loaded": true}' if argv[-1].startswith('curl ') else '')
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
        with patch.object(workflow.vast, 'client', return_value=api), \
             patch.object(workflow.vast, 'rent', return_value={'response': {'new_contract': 777}}) as rent, \
             patch.object(workflow.vast, 'instance', return_value={'id': 777, 'intended_status': 'running'}), \
             patch.object(workflow.vast, 'ssh_command', return_value=['ssh', 'root@example']), \
             patch.object(workflow.subprocess, 'run', side_effect=run) as calls, \
             patch('builtins.input', side_effect=['READY', 'yes']):
            workflow.launch('qwen', 123, .2, None, port, 60)
        self.assertEqual(rent.call_count, 1)
        self.assertEqual(rent.call_args.kwargs['volume'], 99)
        commands = [c.args[0] for c in calls.call_args_list]
        self.assertTrue(any('md5sum' in argv[-1] for argv in commands))
        self.assertTrue(any('-L' in argv for argv in commands))

    def test_failed_checksum_never_opens_tunnel(self):
        self.register()
        m = workflow.read('qwen'); m['volume'] = {'id': 99, 'machine': 42}; m['copy'] = {'state': 'submitted'}
        workflow.save(self.state / 'qwen.json', m)
        workflow.save(self.state / 'qwen.rent.json', {'outcome': 'created', 'response': {'new_contract': 777}})
        api = Mock(); api.client = Mock()
        def run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 1 if 'md5sum' in argv[-1] else 0, stdout='')
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
        with patch.object(workflow.vast, 'client', return_value=api), \
             patch.object(workflow.vast, 'instances', return_value=[{'id': 777}]), \
             patch.object(workflow.vast, 'instance', return_value={'id': 777, 'intended_status': 'running'}), \
             patch.object(workflow.vast, 'ssh_command', return_value=['ssh', 'root@example']), \
             patch.object(workflow.subprocess, 'run', side_effect=run) as calls:
            with self.assertRaisesRegex(RuntimeError, 'checksum verification failed'):
                workflow.launch('qwen', 1, .2, None, port, 60)
        self.assertFalse(any('-L' in c.args[0] for c in calls.call_args_list))

    def test_resume_checks_model_and_opens_local_only_tunnel(self):
        self.register()
        m = workflow.read('qwen'); m['volume'] = {'id': 99, 'machine': 42}; m['copy'] = {'state': 'submitted'}
        workflow.save(self.state / 'qwen.json', m)
        workflow.save(self.state / 'qwen.rent.json', {'outcome': 'created', 'response': {'new_contract': 777}})
        api = Mock(); api.client = Mock()
        api.client.post.return_value.json.return_value = {'offers': [{'machine_id': 42, 'inet_down_cost': .001}]}
        def run(argv, **kwargs):
            if argv[-1].startswith('curl '):
                return subprocess.CompletedProcess(argv, 0, stdout='{"loaded": true}')
            return subprocess.CompletedProcess(argv, 0, stdout='')
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
        with patch.object(workflow.vast, 'client', return_value=api), \
             patch.object(workflow.vast, 'instances', return_value=[{'id': 777}]), \
             patch.object(workflow.vast, 'instance', return_value={'id': 777, 'intended_status': 'running'}), \
             patch.object(workflow.vast, 'ssh_command', return_value=['ssh', 'root@example']), \
             patch.object(workflow.subprocess, 'run', side_effect=run) as calls:
            workflow.launch('qwen', 1, .2, None, port, 60)
        checksum = next(c for c in calls.call_args_list if 'md5sum' in c.args[0][-1])
        self.assertEqual(checksum.kwargs['input'], self.md5.encode())
        tunnel = calls.call_args.args[0]
        self.assertIn(f'127.0.0.1:{port}:127.0.0.1:8080', tunnel)


if __name__ == '__main__':
    unittest.main()
