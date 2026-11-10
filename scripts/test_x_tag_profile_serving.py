import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
from test_x_hierarchy import portrait
from features.x_tag_revenue.storage import projections, SnapshotExpired
from features.x_tag_revenue.serving import publish, Store
from features.x_tag_revenue.integration import patch_fast_load


def fixture(hour=1):
    data = portrait([(1, 'Female,Sweet', i + 1) for i in range(75)])
    data['accounts']['123'] = data['accounts'].pop('channel')
    data['meta'] = dict(version=f'20261110T{hour:02d}0000000000Z', source_generated_at='2026-11-10T01:00:00+00:00')
    proof = dict(version=data['meta']['version'], rows=2, revenue=data['accounts']['123']['revenue_cents'], checksum=123)
    return data, proof


class ServingTests(unittest.TestCase):
    def test_scoped_projections_and_pages_equal_verified_source_without_sql(self):
        data, proof = fixture()
        with tempfile.TemporaryDirectory() as root:
            manifest = publish(root, data, proof)
            fallback = Mock(); fallback.load.side_effect = AssertionError('SQL must not be used')
            fallback.dramas.side_effect = AssertionError('SQL must not be used')
            store = Store(root, fallback)
            actual, meta = store.load(['123'], view='summary')
            self.assertEqual(actual['accounts']['123'], projections(data['accounts']['123'])[0])
            self.assertEqual(meta['read_model'], 'verified_disk')
            self.assertEqual(store.load(['999'])[0]['accounts'], {})
            self.assertEqual(store.load([])[0]['accounts'], {})
            path = '$.types[0].children[0].children[0].dramas'
            pages = [store.dramas('123', proof['version'], path, n) for n in (1, 2, 3)]
            self.assertEqual([len(p['items']) for p in pages], [50, 25, 0])
            rows = pages[0]['items'] + pages[1]['items']
            expected = data['accounts']['123']['types'][0]['children'][0]['children'][0]['dramas']
            self.assertEqual(rows, expected)
            self.assertEqual(sum(r['allocated_cents'] for r in rows), proof['revenue'])
            self.assertEqual(pages[0]['total'], 75)
            fallback.load.assert_not_called(); fallback.dramas.assert_not_called()

    def test_rollover_keeps_previous_and_expired_version_uses_explicit_fallback(self):
        with tempfile.TemporaryDirectory() as root:
            for hour in (1, 2):
                data, proof = fixture(hour); publish(root, data, proof)
            store = Store(root, Mock())
            self.assertEqual(store.load(['123'])[1]['version'], fixture(2)[1]['version'])
            self.assertEqual(store.load(['123'], version=fixture(1)[1]['version'])[1]['version'], fixture(1)[1]['version'])
            data, proof = fixture(3); publish(root, data, proof)
            store._fallback.load.side_effect = SnapshotExpired('expired')
            with self.assertRaises(SnapshotExpired):store.load(['123'], version=fixture(1)[1]['version'])
            self.assertEqual(store.load(['123'], version=fixture(2)[1]['version'])[1]['version'], fixture(2)[1]['version'])

    def test_invalid_source_does_not_replace_current(self):
        with tempfile.TemporaryDirectory() as root:
            data, proof = fixture(); publish(root, data, proof)
            new, bad = fixture(2); bad['revenue'] += 1
            with self.assertRaises(ValueError):publish(root, new, bad)
            self.assertEqual(Store(root).load(['123'])[1]['version'], proof['version'])

    def test_republish_is_immutable_and_rejects_changed_same_version(self):
        with tempfile.TemporaryDirectory() as root:
            data, proof = fixture(); first = publish(root, data, proof)
            path = Path(root) / 'serving' / first['file']; before = path.stat().st_mtime_ns
            publish(root, data, proof)
            self.assertEqual(path.stat().st_mtime_ns, before)
            data['accounts']['123']['status'] = 'changed'
            with self.assertRaises(ValueError):publish(root, data, proof)
            self.assertNotEqual(Store(root).load(['123'])[0]['accounts']['123']['status'], 'changed')

    def test_unavailable_disk_falls_back_to_database_not_zero(self):
        fallback = Mock(); fallback.load.return_value = ({'accounts': {'123': {'revenue_cents': 9}}}, {'version': fixture()[1]['version']})
        with tempfile.TemporaryDirectory() as root:
            result, meta = Store(root, fallback).load(['123'])
        self.assertEqual(result['accounts']['123']['revenue_cents'], 9)
        self.assertEqual(meta['read_model'], 'ads_ai_fallback')

    def test_minor_audiences_remain_visible_in_summary(self):
        data = portrait([(1, 'Female,Sweet', 80), (1, 'Male,Action', 15), (1, 'BL', 5)])
        summary, _ = projections(data['accounts']['channel'])
        self.assertEqual([a['key'] for a in summary['focus']['audiences']], ['female', 'male', 'lgbtq'])
        self.assertEqual(summary['focus']['audiences'][-1]['share_bp'], 500)

    def test_parallel_frontend_load_is_idempotent(self):
        text = '        const loadVersion = ++state.loadVersion;\n' + '          window.XTagRevenue?.refresh();'
        result = patch_fast_load(text)
        self.assertEqual(result.count('XTagRevenue?.refresh()'), 1)
        self.assertIn('XTagRevenue?.paint()', result)
        self.assertEqual(patch_fast_load(result), result)


if __name__ == '__main__': unittest.main()
