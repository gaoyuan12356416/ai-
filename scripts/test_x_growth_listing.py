"""Validate account-list counts against overlapping snapshots of the same posts."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.x_growth_profile import listing, store


class GrowthListingTest(unittest.TestCase):
    def test_post_counts_metrics_and_score_have_distinct_scopes(self):
        now = datetime(2026, 11, 19, 12, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'growth.db'
            store.initialize(path)
            with store.connect(path, write=True) as db:
                for post, account, age in [('a', 1, 80), ('b', 1, 25), ('c', 1, 26), ('d', 1, 1),
                                           ('old', 1, 24 * 29), ('hidden', 2, 100), ('zero', 3, 25)]:
                    published = (now - timedelta(hours=age)).isoformat()
                    db.execute('INSERT INTO growth_post VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                               (post, account, 'uid', 'drama', 'en', 'dramawave', published, 90, '{}', '', 'test'))
                for post, stage, impressions in [('a', '6h', 500), ('a', '24h', 1200), ('a', '72h', 1500),
                                                  ('b', '24h', None), ('c', '24h', 34), ('old', '24h', 9000),
                                                  ('hidden', '24h', 2500), ('zero', '24h', 0)]:
                    db.execute('INSERT INTO post_snapshot VALUES(?,?,?,?)',
                               (post, stage, now.isoformat(), json.dumps({'organic_impressions': impressions})))
                db.execute('INSERT INTO growth_profile VALUES(?,?,?,?)',
                           (1, 'v', now.isoformat(), json.dumps({'mature_posts': 0})))
            result = listing.account_profiles([{'id': 1}, {'id': 3}], path, now)
            self.assertEqual(set(result), {'1', '3'})
            self.assertEqual(result['1']['mature_posts'], 0)
            self.assertEqual(result['1']['observations'], {
                'tracked_posts': 4, 'at_least_72h_posts': 1, 'stages': {'6h': 1, '24h': 3, '72h': 1, '7d': 0},
                'organic_impressions_24h': 1234, 'measured_posts_24h': 2,
            })
            self.assertEqual(result['3']['observations']['organic_impressions_24h'], 0)
            self.assertEqual(result['3']['observations']['measured_posts_24h'], 1)
            self.assertNotIn('mature_posts', result['3'])


if __name__ == '__main__':
    unittest.main()
