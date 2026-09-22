import unittest
from migrate_legacy_snapshot_refs import rewrite


class RewriteTests(unittest.TestCase):
    def test_only_snapshot_path_changes(self):
        original = 'DB = Path("/mnt/data-disk/tt-minis-storage/analysis-snapshots/tt_minis_cache_old.sqlite3")\nSTART = "2026-09-01"\n'
        text, count = rewrite(original, 'test')
        self.assertEqual(count, 1)
        self.assertIn('snapshot(owner=', text)
        self.assertIn('START = "2026-09-01"', text)
        self.assertNotIn('tt_minis_cache_old.sqlite3', text)

    def test_other_paths_untouched(self):
        text = 'DB = Path("/data/main.sqlite3")'
        self.assertEqual((text, 0), rewrite(text, 'test'))

    def test_unrecognized_extra_reference_refuses_migration(self):
        text = 'DB = Path("/mnt/data-disk/tt-minis-storage/analysis-snapshots/tt_minis_cache_old.sqlite3")\nEXTRA = "tt_minis_cache_other.sqlite3"'
        with self.assertRaises(RuntimeError):
            rewrite(text, 'test')
