"""Offline tests: single-attempt rentals, pagination and bounded SSH setup."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests
import vast


class VastTests(unittest.TestCase):
    def test_rent_volume_requires_matching_machine_before_paid_call(self):
        sdk = Mock()
        sdk.client.post.return_value.json.return_value = {'offers': [{'id': 12, 'machine_id': 20}]}
        with tempfile.TemporaryDirectory() as directory, patch.object(vast.storage, 'show_volumes',
                return_value=[{'id': 77, 'machine_id': 10, 'instances': []}]):
            with self.assertRaisesRegex(ValueError, 'different machine'):
                vast.rent(sdk, 12, 'image', bid=.2, volume=77, receipt=Path(directory) / 'receipt.json')
            sdk.create_instance.assert_not_called()
            self.assertFalse((Path(directory) / 'receipt.json').exists())
            sdk.client.post.return_value.json.return_value['offers'][0]['machine_id'] = 10
            sdk.create_instance.return_value = {'success': True, 'new_contract': 42}
            result = vast.rent(sdk, 12, 'image', bid=.2, volume=77, receipt=Path(directory) / 'receipt.json')
            self.assertEqual(result['response']['new_contract'], 42)
            self.assertEqual(sdk.create_instance.call_args.kwargs['link_volume'], 77)
            self.assertEqual(sdk.create_instance.call_args.kwargs['mount_path'], '/models')

    def test_upload_requires_confirmation_and_known_price(self):
        sdk = Mock()
        sdk.show_instances_v1.return_value = {'instances': [{'id': 77, 'ssh_host': 'host.example',
            'ssh_port': 22, 'inet_down_cost': .01}]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'model.bin'
            path.write_bytes(b'data')
            with patch('sys.stdin.isatty', return_value=True), patch('builtins.input', return_value='n'), \
                 patch.object(vast.subprocess, 'run') as run:
                vast.upload_file(sdk, 77, path, '/data/model.bin')
                run.assert_not_called()
            sdk.show_instances_v1.return_value['instances'][0].pop('inet_down_cost')
            with self.assertRaisesRegex(ValueError, 'transfer price'):
                vast.upload_file(sdk, 77, path, '/data/model.bin')

    def test_volume_offer_chooses_cheapest_on_requested_machine(self):
        sdk = Mock()
        sdk.search_volumes.return_value = [
            {'id': 1, 'machine_id': 10, 'disk_space': 20, 'storage_total_cost': .2},
            {'id': 2, 'machine_id': 10, 'disk_space': 50, 'storage_total_cost': .1},
            {'id': 3, 'machine_id': 11, 'disk_space': 50, 'storage_total_cost': 0},
        ]
        self.assertEqual(vast.volume_offer(sdk, 10, 20)['id'], 2)
        with self.assertRaises(LookupError):
            vast.volume_offer(sdk, 10, 100)

    def test_cmp_ranks_total_cost_and_rejects_missing_transfer_price(self):
        sdk = Mock()
        base = {'gpu_name': 'CMP 170HX', 'num_gpus': 1, 'geolocation': 'Poland, PL', 'inet_down': 1000}
        sdk.client.post.return_value.json.return_value = {'offers': [
            dict(base, id=1, dph_total=.1, inet_down_cost=.1),
            dict(base, id=2, dph_total=.2, inet_down_cost=.001),
            dict(base, id=3, dph_total=.01),
            dict(base, id=4, dph_total=.01, inet_down_cost=0, geolocation='London, GB'),
        ], 'truncated': False}
        rows = vast.ranked_offers(sdk, 'CMP 170HX', 2, 10, 20)
        self.assertEqual([row['id'] for row in rows], [2, 1])
        self.assertAlmostEqual(rows[0]['estimated_total_usd'], .41)
        self.assertEqual([row['id'] for row in vast.ranked_offers(sdk, 'CMP 170HX', 2, 10, 20, 'europe')], [4, 2, 1])
        self.assertEqual([row['id'] for row in vast.ranked_offers(sdk, 'CMP 170HX', 2, 10, 20, 'all')], [4, 2, 1])
        sdk.client.post.return_value.json.return_value['truncated'] = True
        with self.assertRaisesRegex(RuntimeError, 'incomplete'):
            vast.ranked_offers(sdk, 'CMP 170HX', 2, 10, 20)

    def transport(self, code, payload=None, error=None):
        def reply(prepared, **kwargs):
            if error:
                raise error
            response = requests.Response()
            response.status_code = code
            response._content = json.dumps(payload or {}).encode()
            response._content_consumed = True
            response.request = prepared
            response.url = prepared.url
            return response
        return patch('vastai.api.client.VastSession.send', side_effect=reply)

    def test_client_uses_one_attempt_and_environment_key(self):
        with patch.dict('os.environ', {'VAST_API_KEY': 'dummy'}), patch.object(vast, 'VastAI') as sdk:
            instance = vast.client()
        self.assertEqual(sdk.call_args.kwargs['retry'], 1)
        self.assertEqual(sdk.call_args.kwargs['api_key'], 'dummy')
        self.assertEqual(instance.client.timeout, 25)

    def test_instances_preserve_raw_boot_rows_and_paginate(self):
        sdk = Mock()
        sdk.show_instances_v1.side_effect = [
            {'instances': [{'id': 1, 'start_date': None}], 'next_token': 'next'},
            {'instances': [{'id': 2}]}]
        self.assertEqual([r['id'] for r in vast.instances(sdk)], [1, 2])
        self.assertEqual(sdk.show_instances_v1.call_args.args[0]['after_token'], 'next')
        sdk.show_instances_v1.side_effect = [{'instances': [], 'next_token': 'same'}] * 2
        with self.assertRaisesRegex(RuntimeError, 'Repeated'):
            vast.instances(sdk)

    def test_sdk_rent_uses_bid_only_when_explicit_and_masks_response_credentials(self):
        for bid in (None, .2):
            with self.subTest(bid=bid), tempfile.TemporaryDirectory() as directory:
                receipt = Path(directory) / 'rent.json'
                with patch.dict('os.environ', {'VAST_API_KEY': 'dummy'}), self.transport(
                        200, {'success': True, 'new_contract': 77, 'instance_api_key': 'hidden'}) as send:
                    result = vast.rent(vast.client(), 12, 'example/image', bid=bid, receipt=receipt, strata=True)
                send.assert_called_once()
                body = json.loads(send.call_args.args[0].body)
                self.assertEqual(body['runtype'], 'ssh_direc ssh_proxy')
                self.assertEqual(body.get('price'), bid)
                self.assertIn('/opt/instance-tools/bin/entrypoint.sh', body['onstart'])
                self.assertEqual(result['response']['new_contract'], 77)
                self.assertNotIn('hidden', receipt.read_text())
                with self.assertRaises(FileExistsError):
                    vast.rent(Mock(), 12, 'example/image', receipt=receipt)

    def test_timeout_and_gateway_error_never_repeat_paid_creation(self):
        for code, error in ((503, None), (200, requests.Timeout('unknown'))):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as directory:
                receipt = Path(directory) / 'rent.json'
                with patch.dict('os.environ', {'VAST_API_KEY': 'dummy'}), self.transport(code, error=error) as send:
                    with self.assertRaisesRegex(RuntimeError, 'outcome unknown'):
                        vast.rent(vast.client(), 12, 'example/image', receipt=receipt)
                send.assert_called_once()
                self.assertEqual(json.loads(receipt.read_text())['outcome'], 'unknown')

    def test_machine_offer_tolerates_rotating_search_results(self):
        sdk = Mock()
        response = sdk.client.post.return_value
        response.json.side_effect = [{'offers': []}, {'offers': [{'id': 123, 'machine_id': 42}]}]
        self.assertEqual(vast.find_machine_offer(sdk, 42, 123, 'bid', 20)['id'], 123)
        self.assertEqual(sdk.client.post.call_count, 2)

    def test_expired_offer_is_recorded_as_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            receipt = Path(directory) / 'rent.json'
            response = requests.Response(); response.status_code = 410
            response._content = b'{"error":"no_such_ask"}'
            sdk = Mock(); sdk.create_instance.side_effect = requests.HTTPError(response=response)
            with self.assertRaisesRegex(RuntimeError, 'no instance was created'):
                vast.rent(sdk, 12, 'example/image', receipt=receipt)
            self.assertEqual(json.loads(receipt.read_text())['outcome'], 'rejected')

    def test_sensitive_response_fields_are_not_rendered(self):
        result = vast.safe({'result_url': 'signed', 'env': ['SECRET=value'],
                            'extra_env': 'TOKEN=value', 'nested': {'api_key': 'secret'}, 'id': 1})
        self.assertEqual(result, {'nested': {}, 'id': 1})

    def test_ssh_proxy_and_direct_fallback(self):
        proxy = vast.ssh_command({'ssh_host': 'ssh.example', 'ssh_port': 1234})
        self.assertEqual(proxy[-1], 'root@ssh.example')
        self.assertIn('1234', proxy)
        direct = vast.ssh_command({'public_ipaddr': '192.0.2.1', 'ports': {'22/tcp': [{'HostPort': '4321'}]}})
        self.assertEqual(direct[-1], 'root@192.0.2.1')
        self.assertIn('4321', direct)
        with self.assertRaises(ValueError):
            vast.ssh_command({'ssh_host': 'bad host', 'ssh_port': 1})

    def test_provision_runs_script_once_and_does_not_destroy_on_failure(self):
        sdk = Mock()
        sdk.show_instances_v1.return_value = {'instances': [{'id': 77, 'ssh_host': 'ssh.example', 'ssh_port': 1234}]}
        with patch.object(vast.subprocess, 'run', side_effect=[subprocess.CompletedProcess([], 0),
                subprocess.CalledProcessError(1, 'ssh')]) as run:
            with self.assertRaises(subprocess.CalledProcessError):
                vast.provision(sdk, 77, timeout=10)
        self.assertEqual(run.call_count, 2)
        self.assertIn(b'mkdir -p /workspace', run.call_args.kwargs['input'])
        sdk.destroy_instance.assert_not_called()

    def test_provision_wait_deadline_does_not_rent_or_destroy(self):
        sdk = Mock()
        sdk.show_instances_v1.return_value = {'instances': [{'id': 77}]}
        clock = [0.]
        with patch.object(vast.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(vast.time, 'sleep', side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)):
            with self.assertRaisesRegex(TimeoutError, 'SSH did not become ready'):
                vast.provision(sdk, 77, timeout=.1)
        sdk.create_instance.assert_not_called()
        sdk.destroy_instance.assert_not_called()

    def test_provision_waits_for_instance_to_appear_after_creation(self):
        sdk = Mock()
        sdk.show_instances_v1.side_effect = [{'instances': []},
            {'instances': [{'id': 77, 'ssh_host': 'ssh.example', 'ssh_port': 1234}]}]
        with patch.object(vast.time, 'sleep'), patch.object(vast.subprocess, 'run',
                return_value=subprocess.CompletedProcess([], 0)) as run:
            self.assertTrue(vast.provision(sdk, 77, timeout=10)['provisioned'])
        self.assertEqual(run.call_count, 2)


if __name__ == '__main__':
    unittest.main()
