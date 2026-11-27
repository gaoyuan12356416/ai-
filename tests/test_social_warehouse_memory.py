import gc
import json
import sqlite3
import tempfile
import tracemalloc
import unittest
from contextlib import closing
from pathlib import Path

from features.social_analytics import warehouse_refresh as refresh
from features.social_analytics.warehouse_schema import digest, encoded, fact_key


class ServingSnapshotMemoryTests(unittest.TestCase):
    def test_large_payloads_keep_bounded_extra_memory_and_exact_rows(self):
        # These represent large publication catalogs and effect payloads. The
        # input is intentionally allocated before tracking snapshot overhead.
        payload = 'example publication metadata ' * 160
        catalog = [dict(id=str(i), text=payload) for i in range(2500)]
        metrics = [dict(platform='facebook', date='2026-11-27', site_id='2310',
                        campaign_id=str(i), text=payload) for i in range(2500)]
        titles = {str(i): dict(name=payload) for i in range(2500)}
        data = dict(catalog=catalog + catalog[:1], metrics=metrics, titles=titles)
        manifest = dict(version='test', end='2026-11-27')
        gc.collect()
        with tempfile.TemporaryDirectory() as folder:
            tracemalloc.start()
            try:
                path = refresh.snapshot(Path(folder), 'test', data, manifest)
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            self.assertLess(peak, 4 * 1024 * 1024, 'snapshot retained a full serialized batch')
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute('PRAGMA quick_check').fetchone()[0], 'ok')
                self.assertEqual(db.execute('SELECT count(*) FROM catalog').fetchone()[0], len(catalog))
                self.assertEqual(dict(db.execute('SELECT row_key,payload FROM catalog')),
                                 {digest(row): encoded(row) for row in catalog})
                self.assertEqual(dict(db.execute('SELECT row_key,payload FROM effects')),
                                 {fact_key(row): encoded(row) for row in metrics})
                self.assertEqual(dict(db.execute('SELECT content_id,payload FROM titles')),
                                 {key: encoded(value) for key, value in titles.items()})
                self.assertEqual(json.loads(db.execute('SELECT payload FROM metadata').fetchone()[0]), manifest)


if __name__ == '__main__':
    unittest.main()
