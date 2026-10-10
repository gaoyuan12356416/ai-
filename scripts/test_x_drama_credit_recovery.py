#!/usr/bin/env python3
"""Offline regression checks for explicitly authorized credit-failure recovery."""
import copy
import json
from pathlib import Path
import sqlite3
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.recover_x_drama_credit_failures import (
    AUDIT, STALE_AUDIT, ERROR, RecoveryConflict, arm, snapshot, validate, verify_route,
    release_stale_reviews,
)


def fixture(relay=False):
    q = dict(id=1, source_type="drama", status="failed", schedule_run_id=8,
             account_id=4, account_username="target", drama_pool_item_id=9,
             content_id="content", drama_replay_generation=0, episode_number=3,
             media_validation_mode="preflight", preflight_size=12345,
             preflight_sha256="a" * 64, preflight_duration=170 if relay else 100,
             account_drama_language_frozen=1, account_drama_language="en",
             delivery_mode="premium_relay_repost" if relay else "direct",
             relay_account_id=5 if relay else 0, relay_account_username="relay" if relay else "",
             updated_at="before", material_url="https://media.example.test/frozen.mp4")
    l = dict(id=11, queue_id=1, account_id=4, status="failed", attempt_count=1,
             unknown_outcome=0, error_code="x_upstream_error", error_message=ERROR,
             x_media_id="", x_post_id="", x_post_url="", published_at="",
             long_url="https://example.test/?af_c_id=1", short_url="https://gy.g2flow.com/s2l/11.html",
             post_text="frozen text", started_at="before", updated_at="before")
    p = dict(id=9, status="active", content_id="content", assigned_account_id=4,
             replay_generation=0, next_sub_number=3, published_episode_count=2,
             free_episode_count=10, last_error_code="x_upstream_error", last_error_message=ERROR)
    r = dict(id=2, queue_id=1, target_account_id=4, relay_account_id=5,
             status="failed", source_attempt_count=1, repost_attempt_count=0,
             unknown_outcome=0, error_code="x_upstream_error", error_message=ERROR,
             source_post_id="", source_post_url="", repost_id="", source_published_at="",
             reposted_at="", updated_at="before") if relay else None
    d = dict(id=3, queue_id=1, route_state="resolved", resolved_delivery_mode=q["delivery_mode"])
    return dict(queue=q, log=l, pool=p, relay=r, route=d)


def database(item):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE x_post_schedule_run (id INTEGER PRIMARY KEY, error_code TEXT, lease_heartbeat_at TEXT)")
    conn.execute("INSERT INTO x_post_schedule_run VALUES (8,'','old')")
    tables = {"queue": "x_post_queue", "log": "x_post_publish_log", "pool": "x_post_drama_pool",
              "relay": "x_post_repost_ledger", "route": "x_post_drama_delivery_route"}
    for key, table in tables.items():
        row = item[key]
        columns = row or fixture(True)[key]
        conn.execute("CREATE TABLE " + table + " (" + ",".join(
            name + (" INTEGER" if isinstance(value, int) else " TEXT")
            for name, value in columns.items()) + ")")
        if row:
            conn.execute("INSERT INTO " + table + " VALUES (" + ",".join("?" for _ in row) + ")", list(row.values()))
    conn.commit()
    return conn


class CreditRecoveryTests(unittest.TestCase):
    def apply(self, conn, item, sync=lambda *args: None):
        arm(conn, item, 8, actor="explicit-operator", commit="b" * 40,
            manifest_sha="c" * 64, sync_run=sync)

    def test_direct_and_relay_preserve_history_counts_frozen_content_and_progress(self):
        for relay in (False, True):
            with self.subTest(relay=relay):
                original = fixture(relay)
                conn = database(original)
                self.apply(conn, original)
                after = snapshot(conn, 1)
                self.assertEqual(after["log"]["attempt_count"], 1)
                self.assertEqual(after["log"]["status"], "reserved")
                self.assertEqual(after["pool"], original["pool"])
                self.assertEqual(after["route"], original["route"])
                self.assertNotEqual(conn.execute("SELECT lease_heartbeat_at FROM x_post_schedule_run").fetchone()[0], "old")
                for key in ("queue", "log", "relay"):
                    if after[key]:
                        for name, value in original[key].items():
                            if name not in ("status", "updated_at"):
                                self.assertEqual(after[key][name], value)
                audit = conn.execute(f"SELECT before_json FROM {AUDIT}").fetchone()[0]
                self.assertEqual(json.loads(audit), original)

    def test_unknown_and_remote_ids_never_rearm(self):
        mutations = [("log", "unknown_outcome", 1), ("log", "x_media_id", "123"),
                     ("log", "x_post_id", "123"), ("log", "status", "post_creating"),
                     ("log", "status", "published"), ("log", "attempt_count", 2),
                     ("relay", "source_post_id", "123"), ("relay", "unknown_outcome", 1),
                     ("relay", "repost_attempt_count", 1)]
        for section, key, value in mutations:
            with self.subTest(section=section, key=key):
                item = fixture(True)
                item[section][key] = value
                conn = database(item)
                with self.assertRaises(RecoveryConflict):
                    self.apply(conn, item)
                self.assertEqual(snapshot(conn, 1), item)

    def test_identity_progress_and_noncredit_failures_are_fenced(self):
        for section, key, value in [("pool", "next_sub_number", 4),
                                    ("pool", "assigned_account_id", 99),
                                    ("queue", "content_id", "other"),
                                    ("log", "error_message", "HTTP 402 at create Post"),
                                    ("route", "route_state", "duration_pending")]:
            with self.subTest(key=key):
                item = fixture()
                item[section][key] = value
                with self.assertRaises(RecoveryConflict):
                    validate(item, 8)

    def test_cas_detects_even_frozen_text_drift(self):
        item = fixture()
        conn = database(item)
        conn.execute("UPDATE x_post_publish_log SET post_text='changed'")
        conn.commit()
        with self.assertRaises(RecoveryConflict):
            self.apply(conn, item)
        self.assertEqual(snapshot(conn, 1)["queue"]["status"], "failed")

    def test_repeated_apply_and_audit_edits_fail(self):
        item = fixture()
        conn = database(item)
        self.apply(conn, item)
        with self.assertRaises(RecoveryConflict):
            self.apply(conn, item)
        for sql in (f"UPDATE {AUDIT} SET actor='changed'", f"DELETE FROM {AUDIT}"):
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(sql)
            conn.rollback()
        self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {AUDIT}").fetchone()[0], 1)

    def test_transaction_rolls_back_on_sync_failure(self):
        item = fixture(True)
        conn = database(item)
        def fail(*_):
            raise RuntimeError("failed to sync")
        with self.assertRaises(RuntimeError):
            self.apply(conn, item, fail)
        self.assertEqual(snapshot(conn, 1), item)

    def test_lost_membership_blocks_frozen_relay_without_rerouting(self):
        item = fixture(True)
        class Client:
            def verify_account(self, aid, **_):
                return dict(id=aid, username="target" if aid == 4 else "relay", drama_language="en",
                            long_video_publish_eligible=False, protected=False, subscription_type="none")
        with self.assertRaises(RecoveryConflict):
            verify_route(Client(), item)
        self.assertEqual(item, fixture(True))

    def test_stale_hold_reconciliation_preserves_failure_and_renews_lease(self):
        item = fixture()
        item["pool"].update(status="needs_review", last_checked_at="old", updated_at="old")
        conn = database(item)
        conn.execute("UPDATE x_post_schedule_run SET error_code='x_post_schedule_stale_claim'")
        conn.commit()
        restored = release_stale_reviews(conn, [item], 8, actor="operator", commit="b" * 40,
                                         manifest_sha="c" * 64, fence=lambda *args: None)
        self.assertEqual(restored[0]["pool"]["status"], "active")
        self.assertEqual(restored[0]["pool"]["last_error_message"], ERROR)
        self.assertEqual(restored[0]["log"], item["log"])
        self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {STALE_AUDIT}").fetchone()[0], 1)
        self.apply(conn, restored[0])

    def test_review_hold_without_stale_evidence_cannot_be_cleared(self):
        item = fixture()
        item["pool"]["status"] = "needs_review"
        conn = database(item)
        with self.assertRaises(RecoveryConflict):
            release_stale_reviews(conn, [item], 8, actor="operator", commit="b" * 40,
                                  manifest_sha="c" * 64, fence=lambda *args: None)
        self.assertEqual(snapshot(conn, 1), item)


if __name__ == "__main__":
    unittest.main()
