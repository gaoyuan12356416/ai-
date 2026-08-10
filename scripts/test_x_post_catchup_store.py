#!/usr/bin/env python3
"""Offline storage-contract tests for one explicit X Post catch-up batch."""

import contextlib
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.x_posts import service


RUN_DATE = "2026-07-27"
SOURCE_DATE = "2026-07-26"
CONFIGURED_ACCOUNT_IDS = tuple(range(2, 11))
REASON = "scope_expansion_v1"


def formal_candidate(account_id, material_id, username=None):
    username = username or "CatchupAccount%s" % account_id
    material_id = str(material_id)
    return {
        "account_id": account_id,
        "account_username": username,
        "source_date": SOURCE_DATE,
        "material_id": material_id,
        "content_id": "content-" + material_id,
        "material_url": (
            "https://media.example.com/source/%s.mp4" % material_id
        ),
        "material_name": "catchup-" + material_id,
        "material_language": "en",
        "drama_name": "Catch-up Drama " + material_id,
        "tag": "romance",
        "description": "A safe catch-up candidate.",
        "page_name": username,
        "page_id": "page-" + str(account_id),
        "preflight_sha256": ("%064x" % int(material_id))[-64:],
        "preflight_size": 5,
        "compliance_counts": {
            "facebook_violation_count": 0,
            "tiktok_violation_count": 0,
            "twitter_violation_count": 0,
            "resource_audit_count": 0,
            "dangerous_tag_count": 0,
        },
    }


class XPostCatchupStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db_path = self.root / "accounts.sqlite3"
        self.store = service.XPostStore(self.db_path)

    def tearDown(self):
        self.temp.cleanup()

    def _publish_queue_in_ledger(self, queue, suffix):
        log = self.store.reserve_log(queue["id"])
        self.store.prepare_log(
            log["id"],
            "https://www.dramawavew2a.com/ads/101/2116/view"
            "?af_ad_id=%s" % queue["material_id"],
            "https://ai.yingliangads.com/s2l/%s.html" % log["id"],
            "https://ai.yingliangads.com/s2l/%s.html\nbody"
            % log["id"],
        )
        self.store.mark_publishing(log["id"])
        self.store.mark_media_uploaded(log["id"], "media-%s" % suffix)
        self.store.mark_published(
            log["id"],
            "media-%s" % suffix,
            "1900%s" % suffix,
            "https://x.com/%s/status/1900%s"
            % (queue["account_username"], suffix),
        )

    def _completed_parent(self):
        candidates = [
            formal_candidate(account_id, 88000 + rank)
            for rank, account_id in enumerate((2, 3, 4), 1)
        ]
        parent = self.store.create_daily_plan(
            RUN_DATE,
            SOURCE_DATE,
            candidates,
        )
        for rank, queue in enumerate(parent["queues"], 1):
            self._publish_queue_in_ledger(queue, "10%s" % rank)
        parent = self.store.get_run(parent["id"])
        self.assertEqual(parent["status"], "completed")
        self.assertEqual(parent["published_count"], 3)
        return parent

    def _catchup_candidates(self, with_pool=False):
        candidates = [
            formal_candidate(account_id, 99000 + rank)
            for rank, account_id in enumerate(range(5, 11), 1)
        ]
        if with_pool:
            result = self.store.add_pool_materials(
                [item["material_id"] for item in candidates],
                validation_checks=[
                    {
                        "material_id": item["material_id"],
                        "error_code": "",
                        "error_message": "",
                    }
                    for item in candidates
                ],
            )
            candidates = [
                formal_candidate(
                    account_id,
                    int(pool_item["material_id"]),
                )
                for account_id, pool_item in zip(
                    range(5, 11),
                    reversed(result["items"]),
                )
            ]
            pool_by_material = {
                str(pool_item["material_id"]): pool_item
                for pool_item in result["items"]
            }
            for candidate in candidates:
                pool_item = pool_by_material[candidate["material_id"]]
                candidate["pool_item_id"] = pool_item["id"]
                candidate["pool_created_at"] = pool_item["created_at"]
        return candidates

    def _raw_parent_snapshot(self, parent_run_id):
        with contextlib.closing(
            sqlite3.connect(self.db_path)
        ) as conn:
            conn.row_factory = sqlite3.Row
            return dict(
                conn.execute(
                    "SELECT * FROM x_post_daily_run WHERE id=?",
                    (parent_run_id,),
                ).fetchone()
            )

    def test_migration_is_idempotent_and_batch_parent_triggers_fail_closed(self):
        service.ensure_storage(self.db_path)
        service.ensure_storage(self.db_path)
        with contextlib.closing(
            sqlite3.connect(self.db_path)
        ) as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            columns = {
                row[1]
                for row in conn.execute(
                    "PRAGMA table_info(x_post_queue)"
                )
            }
            triggers = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger'"
                )
            }
        self.assertIn("x_post_catchup_run", tables)
        self.assertIn("catchup_run_id", columns)
        self.assertIn("trg_x_post_queue_catchup_insert", triggers)
        self.assertIn("trg_x_post_queue_batch_parent_update", triggers)

        parent = self._completed_parent()
        failed = self.store.record_catchup_failure(
            RUN_DATE,
            SOURCE_DATE,
            parent["id"],
            REASON,
            6,
            CONFIGURED_ACCOUNT_IDS,
            "test_preflight_failure",
            "test failure",
        )
        with contextlib.closing(service._connect(self.db_path)) as conn:
            daily_queue_id = conn.execute(
                "SELECT id FROM x_post_queue WHERE run_id=? LIMIT 1",
                (parent["id"],),
            ).fetchone()[0]
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE x_post_queue SET catchup_run_id=? WHERE id=?",
                    (failed["id"], daily_queue_id),
                )
            conn.rollback()

        canary = self.store.enqueue(
            formal_candidate(
                50,
                777001,
                username="CanaryTrigger",
            )
            | {"run_date": "2026-07-28"}
        )
        with contextlib.closing(service._connect(self.db_path)) as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE x_post_queue SET catchup_run_id=? WHERE id=?",
                    (999999, canary["id"]),
                )
            conn.rollback()

    def test_child_plan_is_atomic_idempotent_and_never_mutates_parent(self):
        parent = self._completed_parent()
        parent_before = self._raw_parent_snapshot(parent["id"])
        candidates = self._catchup_candidates(with_pool=True)

        child = self.store.create_catchup_plan(
            RUN_DATE,
            SOURCE_DATE,
            parent["id"],
            REASON,
            candidates,
            CONFIGURED_ACCOUNT_IDS,
        )

        self.assertTrue(child["created"])
        self.assertEqual(child["account_ids"], list(range(5, 11)))
        self.assertEqual(child["expected_count"], 6)
        self.assertEqual(child["queued_count"], 6)
        self.assertEqual(
            [queue["account_id"] for queue in child["queues"]],
            list(range(5, 11)),
        )
        self.assertTrue(
            all(queue["run_id"] is None for queue in child["queues"])
        )
        self.assertTrue(
            all(
                queue["catchup_run_id"] == child["id"]
                for queue in child["queues"]
            )
        )
        self.assertEqual(
            self._raw_parent_snapshot(parent["id"]),
            parent_before,
        )

        repeated = self.store.create_catchup_plan(
            RUN_DATE,
            SOURCE_DATE,
            parent["id"],
            REASON,
            candidates,
            CONFIGURED_ACCOUNT_IDS,
        )
        self.assertFalse(repeated["created"])
        self.assertEqual(repeated["id"], child["id"])
        self.assertEqual(
            [queue["id"] for queue in repeated["queues"]],
            [queue["id"] for queue in child["queues"]],
        )

        queried = self.store.query_catchup_plan(
            RUN_DATE,
            parent["id"],
        )
        self.assertTrue(queried["found"])
        self.assertEqual(queried["run"]["batch_kind"], "catchup")
        self.assertEqual(queried["run"]["account_ids"], list(range(5, 11)))
        self.assertEqual(len(queried["queues"]), 6)

        for rank, queue in enumerate(child["queues"], 1):
            self._publish_queue_in_ledger(queue, "20%s" % rank)
        completed = self.store.get_catchup_run(child["id"])
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["published_count"], 6)
        self.assertEqual(completed["failed_count"], 0)
        self.assertEqual(completed["unknown_count"], 0)
        self.assertEqual(
            self._raw_parent_snapshot(parent["id"]),
            parent_before,
        )
        logs = self.store.query_logs(
            {
                "run_date": RUN_DATE,
                "account_id": 5,
                "page": 1,
                "page_size": 10,
            }
        )
        self.assertEqual(logs["pagination"]["total"], 1)
        self.assertEqual(logs["items"][0]["batch_kind"], "catchup")
        self.assertEqual(
            logs["items"][0]["catchup_run_id"],
            child["id"],
        )

    def test_scope_is_exact_and_a_collision_rolls_back_the_whole_child(self):
        parent = self._completed_parent()
        candidates = self._catchup_candidates()
        wrong_order = list(candidates)
        wrong_order[0], wrong_order[1] = (
            wrong_order[1],
            wrong_order[0],
        )
        with self.assertRaises(service.XPostError) as wrong:
            self.store.create_catchup_plan(
                RUN_DATE,
                SOURCE_DATE,
                parent["id"],
                REASON,
                wrong_order,
                CONFIGURED_ACCOUNT_IDS,
                require_pool=False,
            )
        self.assertEqual(
            wrong.exception.code,
            "x_post_catchup_scope_mismatch",
        )

        occupied = formal_candidate(
            50,
            candidates[0]["material_id"],
            username="PriorMaterialOwner",
        )
        occupied["run_date"] = "2026-07-26"
        self.store.enqueue(occupied)
        with self.assertRaises(service.XPostError) as collision:
            self.store.create_catchup_plan(
                RUN_DATE,
                SOURCE_DATE,
                parent["id"],
                REASON,
                candidates,
                CONFIGURED_ACCOUNT_IDS,
                require_pool=False,
            )
        self.assertEqual(
            collision.exception.code,
            "x_post_material_already_used",
        )
        with contextlib.closing(
            sqlite3.connect(self.db_path)
        ) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM x_post_catchup_run"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM x_post_queue "
                    "WHERE catchup_run_id IS NOT NULL"
                ).fetchone()[0],
                0,
            )

    def test_catchup_unknown_aggregation_does_not_touch_daily_parent(self):
        parent = self._completed_parent()
        parent_before = self._raw_parent_snapshot(parent["id"])
        child = self.store.create_catchup_plan(
            RUN_DATE,
            SOURCE_DATE,
            parent["id"],
            REASON,
            self._catchup_candidates(),
            CONFIGURED_ACCOUNT_IDS,
            require_pool=False,
        )
        queue = child["queues"][0]
        log = self.store.reserve_log(queue["id"])
        self.store.prepare_log(
            log["id"],
            "https://www.dramawavew2a.com/ads/101/2116/view"
            "?af_ad_id=%s" % queue["material_id"],
            "https://ai.yingliangads.com/s2l/%s.html" % log["id"],
            "https://ai.yingliangads.com/s2l/%s.html\nbody"
            % log["id"],
        )
        self.store.mark_publishing(log["id"])
        self.store.mark_media_uploaded(log["id"], "unknown-media")

        child_state = self.store.get_catchup_run(child["id"])
        self.assertEqual(child_state["status"], "needs_review")
        self.assertEqual(child_state["unknown_count"], 1)
        self.assertEqual(child_state["published_count"], 0)
        self.assertEqual(
            self._raw_parent_snapshot(parent["id"]),
            parent_before,
        )

    def test_failure_record_is_idempotent_and_can_become_one_frozen_plan(self):
        parent = self._completed_parent()
        parent_before = self._raw_parent_snapshot(parent["id"])
        first = self.store.record_catchup_failure(
            RUN_DATE,
            SOURCE_DATE,
            parent["id"],
            REASON,
            6,
            CONFIGURED_ACCOUNT_IDS,
            "candidate_shortage",
            "only five candidates",
        )
        second = self.store.record_catchup_failure(
            RUN_DATE,
            SOURCE_DATE,
            parent["id"],
            REASON,
            6,
            CONFIGURED_ACCOUNT_IDS,
            "candidate_shortage",
            "only five candidates",
        )
        self.assertTrue(first["recorded"])
        self.assertFalse(second["recorded"])
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["status"], "failed_preflight")
        self.assertEqual(first["account_ids"], list(range(5, 11)))

        with self.assertRaises(service.XPostError) as mismatch:
            self.store.record_catchup_failure(
                RUN_DATE,
                SOURCE_DATE,
                parent["id"],
                REASON,
                5,
                CONFIGURED_ACCOUNT_IDS,
                "candidate_shortage",
                "wrong expected count",
            )
        self.assertEqual(
            mismatch.exception.code,
            "x_post_catchup_scope_mismatch",
        )

        child = self.store.create_catchup_plan(
            RUN_DATE,
            SOURCE_DATE,
            parent["id"],
            REASON,
            self._catchup_candidates(),
            CONFIGURED_ACCOUNT_IDS,
            require_pool=False,
        )
        self.assertEqual(child["id"], first["id"])
        self.assertEqual(child["status"], "queued")
        self.assertEqual(len(child["queues"]), 6)
        self.assertEqual(
            self._raw_parent_snapshot(parent["id"]),
            parent_before,
        )

    def test_parent_must_be_exactly_completed_three_of_three(self):
        parent = self.store.create_daily_plan(
            RUN_DATE,
            SOURCE_DATE,
            [
                formal_candidate(account_id, 87000 + rank)
                for rank, account_id in enumerate((2, 3, 4), 1)
            ],
        )
        with self.assertRaises(service.XPostError) as rejected:
            self.store.create_catchup_plan(
                RUN_DATE,
                SOURCE_DATE,
                parent["id"],
                REASON,
                self._catchup_candidates(),
                CONFIGURED_ACCOUNT_IDS,
                require_pool=False,
            )
        self.assertEqual(
            rejected.exception.code,
            "x_post_catchup_parent_not_ready",
        )
        self.assertFalse(
            self.store.query_catchup_plan(
                RUN_DATE,
                parent["id"],
            )["found"]
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
