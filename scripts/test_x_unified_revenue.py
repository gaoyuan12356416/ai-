import copy
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if sys.platform == 'win32':
    sys.modules.setdefault('fcntl', types.SimpleNamespace(LOCK_EX=1, LOCK_NB=2, flock=lambda *args: None))
from features.x_account_stats.service import read_snapshot, merge_account_stats
from features.x_tag_revenue import serving, refresh, routes
from features.x_tag_revenue.statistics import build_statistics
from test_x_tag_revenue import publication, metric, resource
from features.x_tag_revenue.core import build

NOW = datetime(2026, 11, 19, 3, 20, tzinfo=timezone.utc)
VERSION = '20261119T032000000000Z'


def fixture(ledger):
    data = build([publication(), publication()], [metric(101), metric(202, day='2026-11-18'),
                  metric(999, day='2026-11-19'), metric(50, campaign='unassigned')],
                 [resource()], '2026-11-18')
    data['meta'] = dict(version=VERSION, generated_at=NOW.isoformat(), start='2026-01-01',
                        end='2026-11-18', source_version='source-v1', source_generated_at=NOW.isoformat())
    with patch('features.x_tag_revenue.statistics.read_ledger_metrics', return_value=({7: {
        'published_posts_total': 3, 'published_posts_yesterday': 2, 'reposts_total': 0, 'reposts_yesterday': 0}}, {}, {})):
        data['operating_stats'] = build_statistics(data, ledger, NOW)
    return data


class UnifiedRevenueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.ledger = self.root / 'ledger.sqlite3'
        with closing(sqlite3.connect(self.ledger)) as db:
            db.execute('CREATE TABLE x_authorized_account(id INTEGER,x_user_id TEXT)')
            db.executemany('INSERT INTO x_authorized_account VALUES(?,?)', [(7, '123'), (8, '999')])
            db.commit()

    def tearDown(self):
        for path in self.root.rglob('*.sqlite3'):
            path.chmod(0o644)
        self.temp.cleanup()

    def publish(self, data):
        return serving.publish(self.root, data, {'version': data['meta']['version'],
            'rows': len(data['accounts']) + 1, 'revenue': sum(p['revenue_cents'] for p in data['accounts'].values())})

    def test_income_cutoff_attribution_and_atomic_version(self):
        data = fixture(self.ledger)
        self.publish(data)
        path = self.root / 'serving/current.json'
        snapshot, meta = read_snapshot(path, now=NOW)
        portrait, profile_meta = serving.Store(self.root).load(['123'], view='summary')
        self.assertEqual(meta['version'], profile_meta['version'])
        self.assertEqual(meta['generated_at_utc'], profile_meta['generated_at'])
        self.assertEqual('3.030000', snapshot['accounts']['7']['revenue_total_usd'])
        self.assertEqual('2.020000', snapshot['accounts']['7']['revenue_yesterday_usd'])
        self.assertEqual(303, portrait['accounts']['123']['revenue_cents'])
        self.assertEqual(50, snapshot['attribution']['unassigned_revenue_cents'])
        self.assertEqual('0.000000', snapshot['accounts']['8']['revenue_total_usd'])
        self.assertEqual('fresh', meta['status'])
        self.assertNotIn('operating_stats', profile_meta)

    def test_mismatched_income_never_replaces_shared_pointer(self):
        data = fixture(self.ledger)
        self.publish(data)
        path = self.root / 'serving/current.json'
        old = path.read_bytes()
        data['meta']['version'] = data['operating_stats']['version'] = '20261119T042000000000Z'
        data['operating_stats']['accounts']['7']['revenue_total_usd'] = '999.00'
        with self.assertRaisesRegex(ValueError, 'statistics_portrait_revenue_mismatch'):
            self.publish(data)
        self.assertEqual(old, path.read_bytes())
        self.assertEqual('3.030000', read_snapshot(path, now=NOW)[0]['accounts']['7']['revenue_total_usd'])

    def test_corrupt_statistics_is_unavailable_not_zero(self):
        self.publish(fixture(self.ledger))
        path = self.root / 'serving/current.json'
        manifest = json.loads(path.read_text())
        database = self.root / 'serving' / manifest['file']
        database.chmod(0o644)
        with closing(sqlite3.connect(database)) as db:
            db.execute('DROP TABLE account_stats')
            db.commit()
        dto = merge_account_stats({'items': [{'id': 7}]}, path, now=NOW)
        self.assertIsNone(dto['items'][0]['operating_stats']['revenue_total_usd'])
        self.assertEqual('missing', dto['operating_stats_meta']['status'])

    def test_refresh_failure_is_shared_and_old_income_survives(self):
        self.publish(fixture(self.ledger))
        (self.root / 'status.json').write_text(json.dumps({'status': 'error'}))
        snapshot, meta = read_snapshot(self.root / 'serving/current.json', now=NOW)
        _, portrait_meta = routes.read_snapshot(self.root, ['123'])
        self.assertTrue(meta['refresh_failed'])
        self.assertTrue(portrait_meta['refresh_failed'])
        self.assertEqual('3.030000', snapshot['accounts']['7']['revenue_total_usd'])

    def test_sql_timeout_splits_ranges_without_losing_rows(self):
        calls = []
        class Reader:
            def query(self, statements, timeout):
                ids = re.findall(r'0x([0-9a-f]+)', statements[0])
                calls.append(len(ids))
                if len(ids) > 2:
                    raise RuntimeError('source_query_failed_3024')
                return [bytes.fromhex(value).decode() for value in ids]
        self.assertEqual(['a', 'b', 'c', 'd'], refresh.bounded_query(Reader(), ['a','b','c','d'], 'SELECT %s'))
        self.assertEqual([4, 2, 2], calls)
        class Broken:
            def query(self, *args, **kwargs):
                raise RuntimeError('source_query_failed_1146')
        with self.assertRaisesRegex(RuntimeError, '1146'):
            refresh.bounded_query(Broken(), ['a','b'], 'SELECT %s')

    def test_summary_version_is_pinned_after_authorization(self):
        self.publish(fixture(self.ledger))
        sent = []
        class Handler:
            headers = {'Cookie': 'fixture'}
            def send_json(self, status, payload):
                sent.append((status, payload))
        routes.dispatch(Handler(), urlsplit('/api/x-channel-tag-revenue?version=' + VERSION),
                        lambda cookie: (200, [{'id': 7, 'x_user_id': '123'}]), self.root)
        self.assertEqual(200, sent[0][0])
        self.assertEqual(VERSION, sent[0][1]['meta']['version'])
        self.assertEqual(303, sent[0][1]['items']['7']['revenue_cents'])

    def test_expired_version_requests_list_refresh_not_reopen_loop(self):
        sent = []
        class Handler:
            headers = {'Cookie': 'fixture'}
            def send_json(self, status, payload):
                sent.append((status, payload))
        with patch.object(routes, 'read_snapshot', side_effect=routes.SnapshotExpired()):
            routes.dispatch(Handler(), urlsplit('/api/x-channel-tag-revenue?version=' + VERSION),
                            lambda cookie: (200, [{'id': 7, 'x_user_id': '123'}]), self.root)
        self.assertEqual(409, sent[0][0])
        self.assertIn('刷新列表', sent[0][1]['message'])


if __name__ == '__main__':
    unittest.main()
