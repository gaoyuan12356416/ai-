import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch


path = Path(__file__).resolve().parents[1] / 'ops/cpu-db-egress/egress.py'
spec = importlib.util.spec_from_file_location('cpu_egress', path)
egress = importlib.util.module_from_spec(spec)
spec.loader.exec_module(egress)


def cloud(cap=20, billing=egress.BILLING):
    client = Mock()
    client.describe.return_value = dict(cap_mbps=cap, billing=billing, state='RUNNING', operation_state='SUCCESS')
    client.quote.return_value = {'Price': {'BandwidthPrice': {'UnitPrice': .1, 'ChargeUnit': 'GB'}}, 'RequestId': 'fixture'}
    return client


class EgressTests(unittest.TestCase):
    def test_plan_preserves_per_gb_billing_and_one_instance(self):
        planned = egress.plan(cloud())
        self.assertEqual(planned['target_mbps'], 100)
        self.assertTrue(planned['traffic_tariff_unchanged'])
        self.assertEqual(egress.bandwidth(100), {'InstanceIds': ['ins-lyr5109j'], 'InternetAccessible': {'InternetMaxBandwidthOut': 100}})

    def test_changed_tariff_billing_or_another_operators_cap_is_preserved(self):
        for client in [cloud(cap=50), cloud(billing='BANDWIDTH_PREPAID')]:
            with self.assertRaises(RuntimeError):
                egress.plan(client)
            client.api.assert_not_called()
        client = cloud()
        client.quote.side_effect = [client.quote.return_value, {'Price': {'BandwidthPrice': {'UnitPrice': .2, 'ChargeUnit': 'GB'}}}]
        with self.assertRaises(RuntimeError):
            egress.plan(client)

    def test_uncertain_write_is_read_back_once_without_write_retry(self):
        client = cloud()
        client.describe.side_effect = [dict(cap_mbps=20, billing=egress.BILLING, state='RUNNING', operation_state='SUCCESS'),
                                       dict(cap_mbps=100, billing=egress.BILLING, state='RUNNING', operation_state='SUCCESS')]
        client.api.side_effect = RuntimeError('cloud_transport_TimeoutError')
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary) / 'deployment'
            result = egress.change(client, folder, 'fixture')
            self.assertEqual(result['phase'], 'confirmed')
            client.api.assert_called_once()
            client.api.reset_mock()
            client.describe.side_effect = None
            client.describe.return_value = dict(cap_mbps=100, operation_state='SUCCESS')
            egress.change(client, folder, 'fixture')
            client.api.assert_not_called()

    def test_rollback_refuses_to_overwrite_another_bandwidth_change(self):
        client = cloud(cap=150)
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            egress.atomic_json(folder / 'manifest.json', dict(instance_id=egress.INSTANCE, phase='confirmed'))
            with self.assertRaises(RuntimeError):
                egress.change(client, folder, 'fixture', rollback=True)
            client.api.assert_not_called()
