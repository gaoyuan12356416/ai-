import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_memory_safety_contract import load_generator, sample_row


class PartitionReuseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.m = load_generator()
        self.start, self.end = '2026-08-06', '2026-08-07'
        self.revisions = {(level, day): {'refreshed_at': 'r1', 'row_count': 1}
                          for level in self.m.METRIC_LEVELS
                          for day in (self.start, self.end)}
        self.m.cache_partition_revisions = lambda *_: dict(self.revisions)
        self.calls = []

        def fetch(day, end, level):
            self.calls.append((level, day))
            row = sample_row(self.m, day, '1001', '2001', 10)
            row['metric_level'] = level
            return [row] if self.revisions[(level, day)]['row_count'] else []
        self.m.fetch_rows_from_cache = fetch
        self.payload = self.m.build_payload([], self.start, self.end,
            {'type': 'skipped', 'checks': [], 'warnings': []}, include_rows=False)

    def publish(self):
        self.m.publish_from_cache(self.payload, self.root, self.start, self.end)
        return json.loads((self.root / 'latest.json').read_text(encoding='utf-8'))

    def test_unchanged_partitions_are_not_read_or_rewritten(self):
        first = self.publish()
        self.calls.clear()
        second = self.publish()
        self.assertEqual([], self.calls)
        self.assertEqual(first['data_files'], second['data_files'])
        self.assertEqual(0, second['publication']['written_bytes'])
        self.assertEqual(4, second['publication']['reused_partitions'])

    def test_historical_backfill_regenerates_exactly_one_partition(self):
        first = self.publish()
        self.revisions[('ad', self.start)] = {'refreshed_at': 'r2', 'row_count': 1}
        self.calls.clear()
        second = self.publish()
        self.assertEqual([('ad', self.start)], self.calls)
        self.assertNotEqual(first['data_files']['ad'][self.start]['path'],
                            second['data_files']['ad'][self.start]['path'])
        self.assertEqual(3, second['publication']['reused_partitions'])

    def test_missing_or_truncated_file_is_rebuilt(self):
        first = self.publish()
        (self.root / first['data_files']['ad'][self.start]['path']).unlink()
        (self.root / first['data_files']['campaign'][self.start]['path']).write_text('{}')
        self.calls.clear()
        self.publish()
        self.assertEqual({('ad', self.start), ('campaign', self.start)}, set(self.calls))

    def test_schema_version_change_rebuilds_all(self):
        self.publish()
        self.calls.clear()
        self.m.PARTITION_FORMAT_VERSION += 1
        self.publish()
        self.assertEqual(4, len(self.calls))

    def test_changed_to_empty_day_drops_old_reference(self):
        first = self.publish()
        self.revisions[('ad', self.start)] = {'refreshed_at': 'r2', 'row_count': 0}
        second = self.publish()
        self.assertNotIn(self.start, second['data_files']['ad'])
        self.assertTrue((self.root / first['data_files']['ad'][self.start]['path']).exists())

    def test_retired_ancient_partition_lives_for_full_grace(self):
        first = self.publish()
        old_path = self.root / first['data_files']['ad'][self.start]['path']
        now = int(self.m.time.time())
        os.utime(str(old_path), (now - 1000000, now - 1000000))
        self.revisions[('ad', self.start)] = {'refreshed_at': 'r2', 'row_count': 1}
        with patch.object(self.m.time, 'time', return_value=now):
            second = self.publish()
        for archive in (self.root / 'manifest-history').glob('*.json'):
            os.utime(str(archive), (now, now))
        keep = {i['path'] for fs in second['data_files'].values() for i in fs.values()}
        self.m.prune_stale_published_files(self.root / 'data', keep, now=now + 86399)
        self.assertTrue(old_path.exists())
        self.m.prune_stale_published_files(self.root / 'data', keep, now=now + 86401)
        self.assertFalse(old_path.exists())
        self.assertTrue(all((self.root / p).exists() for p in keep))

    def test_corrupt_history_fails_closed(self):
        self.publish()
        archive = self.root / 'manifest-history' / 'bad.json'
        archive.parent.mkdir(exist_ok=True)
        archive.write_text('bad')
        stale = self.root / 'data' / 'orphan.json'
        stale.write_text('{}')
        os.utime(str(stale), (1, 1))
        self.assertEqual(0, self.m.prune_stale_published_files(self.root / 'data', set()))
        self.assertTrue(stale.exists())

    def test_revision_change_during_publication_keeps_latest(self):
        self.publish()
        old = (self.root / 'latest.json').read_bytes()
        changed = dict(self.revisions)
        changed[('ad', self.start)] = {'refreshed_at': 'r2', 'row_count': 1}
        with patch.object(self.m, 'cache_partition_revisions', side_effect=[self.revisions, changed]):
            with self.assertRaisesRegex(RuntimeError, 'changed during'):
                self.publish()
        self.assertEqual(old, (self.root / 'latest.json').read_bytes())

    def test_bad_count_or_missing_revision_preserves_manifest(self):
        self.publish()
        old = (self.root / 'latest.json').read_bytes()
        self.revisions[('ad', self.start)] = {'refreshed_at': 'r2', 'row_count': 2}
        with self.assertRaisesRegex(RuntimeError, 'row count'):
            self.publish()
        del self.revisions[('ad', self.start)]
        with self.assertRaisesRegex(RuntimeError, 'Missing cache revision'):
            self.publish()
        self.assertEqual(old, (self.root / 'latest.json').read_bytes())

    def test_partition_contract_and_metrics_stay_identical(self):
        first = self.publish()
        for level, files in first['data_files'].items():
            for day, info in files.items():
                detail = json.loads((self.root / info['path']).read_text(encoding='utf-8'))
                self.assertEqual(level, detail['meta']['metric_level'])
                self.assertEqual(day, detail['meta']['start_date'])
                self.assertEqual(info['row_count'], len(detail['rows']))
                self.assertEqual(10, detail['totals']['spend'])
                self.assertEqual(5, detail['totals']['revenue'])
                self.assertEqual(0.5, detail['totals']['roas'])

    def test_escaping_manifest_path_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'Invalid published'):
            self.m.detail_path(self.root, 'data/../../outside.json')


if __name__ == '__main__':
    unittest.main()
