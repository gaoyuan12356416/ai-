import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from features.tt_auto_posts.legacy_reader import LEGACY_MATERIAL_TABLES
from features.tt_promotion import operations
from features.tt_promotion.channels import ChannelError
from features.tt_promotion.pub_store import connection, dump
from features.tt_promotion.publishing import initialize


class PromotionOperationsTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "promotion.sqlite3"
        initialize(self.path)
        self.actor = {"tenant_key": "tenant-a", "user_id": "operator"}
        self.scope = {"12", "13"}
        # Use the production template constructor and defaults, including a real version snapshot.
        from features.tt_auto_posts.core import TTPostAutoStore
        from features.tt_promotion.publishing import audit, config_for

        name, config = config_for(
            {
                "name": "Example",
                "account_ids": ["12", "13"],
                "schedule": {"mode": "fixed", "times": ["12:00"]},
            },
            self.scope,
            self.actor,
        )
        self.t = TTPostAutoStore(self.path).create_template(
            name=name, config=config, actor=audit(self.actor), confirmation={"accepted": True}
        )

    def job(self, db, key, date, channels=("12",), state="planned", tenant="tenant-a"):
        job = db.execute(
            """INSERT INTO ttp_job(op_key,request_hash,template_id,version,template_name,
                  tenant,actor,trigger_type,opts,created_at) VALUES(?, 'hash',?,1,'Example',?,'{}','manual','{}',?)""",
            (key, self.t.id, tenant, date),
        ).lastrowid
        for channel in channels:
            db.execute(
                """INSERT INTO ttp_slot(slot_key,job_id,account_id,scheduled_at,state,changed_at)
                VALUES(?,?,?,?,?,?)""",
                (f"{job}:{channel}", job, channel, date, state, date),
            )
        return job

    def test_history_filters_before_pagination_and_scopes_whole_runs(self):
        at = "2026-11-25T16:05:00+00:00"
        with connection(self.path, write=True) as db:
            first = self.job(db, "first", at, ("12", "13"))
            db.execute("UPDATE ttp_slot SET state='skipped' WHERE job_id=? AND account_id='13'", (first,))
            for i in range(202):
                self.job(db, "next-" + str(i), at)
            self.job(db, "outside", at, ("12", "14"), "skipped")
            self.job(db, "foreign", at, ("12",), "skipped", tenant="tenant-b")

        def read(**args):
            return operations.read("/history", args, self.actor, self.scope, self.path)

        self.assertEqual(read(page=2, page_size=100)["total"], 203)
        self.assertEqual(read()["items"][0]["status"], "planned")
        result = read(status="skipped", account_id="13", start="2026-11-26")
        self.assertEqual(result["items"][0]["id"], first)
        self.assertEqual(result["items"][0]["counts"]["expected"], 2)
        self.assertEqual(result["items"][0]["counts"]["pending"], 1)
        self.assertEqual(read(status="skipped", account_id="12")["total"], 0)
        self.assertEqual(read(end="2026-11-25")["total"], 0)
        with self.assertRaises(ChannelError):
            read(status="invalid")

    def test_stats_dates_frozen_and_projection(self):
        stamp = "2026-11-25T05:00:00+00:00"  # after noon in Beijing
        with connection(self.path, write=True) as db:
            db.execute("UPDATE tt_auto_template SET enabled=1 WHERE id=?", (self.t.id,))
            db.execute("UPDATE tt_auto_template SET updated_by_name='Last operator' WHERE id=?", (self.t.id,))
            self.job(db, "older running", stamp, state="reserved")
            self.job(db, "last", stamp, state="skipped")
            db.execute(
                "INSERT INTO ttp_day VALUES(?,?,1,?)",
                (self.t.id, "2026-11-25", dump([["12", "2026-11-25T06:00:00+00:00"]])),
            )
        with patch.object(operations, "now", return_value=datetime.fromisoformat(stamp)):
            res = operations.read("/stats/templates", {}, self.actor, self.scope, self.path)["items"][0]
        self.assertEqual(res["operator_name"], "Last operator")
        self.assertTrue(res["running"])
        self.assertEqual(res["latest"]["created_at"], stamp)
        self.assertEqual(res["latest"]["skipped"], 1)
        self.assertEqual(res["next_at"], "2026-11-25T06:00:00+00:00")

    def test_inventory_import_and_cross_ledger_occupancy(self):
        ids = ["11", "12", "13", "14"]
        args = {"material_ids": ids, "expected_version": self.t.version}
        self.assertEqual(
            operations.add_inventory(self.path, self.t.id, args, self.actor, self.scope),
            {"added": 4, "total": 4},
        )
        self.assertEqual(
            operations.add_inventory(self.path, self.t.id, args, self.actor, self.scope)["added"], 0
        )
        with self.assertRaises(ChannelError):
            operations.add_inventory(self.path, self.t.id, args, self.actor, {"12"})
        old, pool = self.path.parent / "old.sqlite3", self.path.parent / "pool.sqlite3"
        initialize(old)
        with closing(sqlite3.connect(pool)) as db, db:
            for table in LEGACY_MATERIAL_TABLES:
                db.execute(
                    f"CREATE TABLE {table}(material_id TEXT, status TEXT, publish_id TEXT, unknown_outcome INTEGER)"
                )
            db.execute("INSERT INTO tt_post_recurring_pool(material_id) VALUES('12')")
            db.execute("INSERT INTO tt_post_queue VALUES('13','published','receipt',0)")
            db.execute("INSERT INTO tt_post_direct_test VALUES('14','reconciling','r',1)")
        with connection(self.path) as db:
            result = operations.inventory(
                db, self.actor, self.scope, self.t.id, {"status": "review"}, (old, pool)
            )
        self.assertEqual(result["counts"], {"available": 1, "occupied": 1, "published": 1, "review": 1})
        self.assertEqual(result["items"][0]["material_id"], "14")
        self.assertIsNone(result["items"][0]["job_id"])
