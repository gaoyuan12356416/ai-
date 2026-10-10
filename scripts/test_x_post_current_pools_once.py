import contextlib
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from features.x_posts.service import XPostStore
from scripts.x_post_current_pools_once import BEIJING, claim, identity


class CurrentPoolOnceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "posts.sqlite3"
        XPostStore(self.db)
        self.now = datetime(2026, 10, 10, 12, 1, tzinfo=BEIJING)
        with contextlib.closing(sqlite3.connect(str(self.db))) as c:
            for source, version in [("material", 30), ("drama", 26)]:
                c.execute("UPDATE x_post_schedule_config SET enabled=1,version=?,account_ids_json='[2,1]',body_template='{{drama_name}}',schedule_mode='random',random_effective_date='2026-10-11' WHERE source_type=?", (version, source))
                c.execute("INSERT INTO x_post_schedule_random_plan(source_type,run_date,config_version,account_ids_json,body_template,publish_times_json,created_at) VALUES(?,?,?,?,?,?,?)", (source, '2026-10-10', version-1, '[1]', '{{desc}}', '["14:32"]', '2026-10-09T16:00:00Z'))
            c.commit()

    def tearDown(self):
        self.tmp.cleanup()

    def run_claim(self, **kwargs):
        return claim(self.db, "current-pools-20261010", kwargs.pop("versions", {"material": 30, "drama": 26}), actor="operator", now=kwargs.pop("now", self.now), **kwargs)

    def test_current_scope_and_daily_plans_unchanged(self):
        rows = self.run_claim()
        self.assertEqual([r["config_version"] for r in rows], [30, 26])
        self.assertTrue(all(identity(r)["account_ids"] == [2, 1] for r in rows))
        with contextlib.closing(sqlite3.connect(str(self.db))) as c:
            self.assertEqual(c.execute("SELECT count(*) FROM x_post_queue").fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT count(*) FROM x_post_operator_gap_recovery_audit").fetchone()[0], 2)
            self.assertEqual(c.execute("SELECT DISTINCT account_ids_json FROM x_post_schedule_random_plan").fetchall(), [("[1]",)])

    def test_repeated_request_does_not_create_more_runs_or_refreeze(self):
        first = self.run_claim()
        with contextlib.closing(sqlite3.connect(str(self.db))) as c:
            c.execute("UPDATE x_post_schedule_config SET version=version+1,account_ids_json='[3]'")
            c.commit()
        self.assertEqual(first, self.run_claim())

    def test_second_config_change_rolls_back_both_claims(self):
        with self.assertRaises(ValueError):
            self.run_claim(versions={"material": 30, "drama": 25})
        with contextlib.closing(sqlite3.connect(str(self.db))) as c:
            self.assertEqual(c.execute("SELECT count(*) FROM x_post_schedule_run").fetchone()[0], 0)

    def test_natural_slot_collision_has_no_claim(self):
        with self.assertRaises(ValueError):
            self.run_claim(now=datetime(2026, 10, 10, 14, 32, tzinfo=BEIJING))


if __name__ == "__main__":
    unittest.main()
