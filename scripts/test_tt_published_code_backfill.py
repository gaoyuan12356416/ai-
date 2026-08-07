#!/usr/bin/env python3
"""Offline safety tests for the historical TT code backfill."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qsl, urlsplit


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.tt_posts.code_routes import (  # noqa: E402
    TTCodeRouteResolver,
    ensure_code_route_storage,
)
from features.tt_posts.links import build_w2a_url, build_w2a_url_from_fields  # noqa: E402
import scripts.backfill_tt_published_codes as backfill_module  # noqa: E402
from scripts.backfill_tt_published_codes import (  # noqa: E402
    TTCodeBackfillError,
    apply_plan,
    backup_database,
    inspect_database,
)


class TTPublishedCodeBackfillTests(unittest.TestCase):
    LEDGER_QUEUE_ID = 6

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.db_path = root / "tt-post.sqlite3"
        self.backup_path = root / "backups" / "before.sqlite3"
        self.backup_path.parent.mkdir()
        self._create_database()

    @staticmethod
    def _long_url(queue_id: int, content_id: str) -> str:
        return build_w2a_url(
            {
                "username": "creator%s" % queue_id,
                "timestamp": 1785988800 + queue_id,
                "material_language": "en",
                "drama_name": "Drama%s" % queue_id,
                "tag": "none",
                "link_id": queue_id,
                "page_name": "Creator %s" % queue_id,
                "page_id": "640%s" % queue_id,
                "material_name": "Clip %s" % queue_id,
                "material_id": "500%s" % queue_id,
                "queue_id": queue_id,
                "content_id": content_id,
                "channel": "AIpost",
            }
        )

    def _create_database(self):
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute(
                """
                CREATE TABLE tt_post_queue(
                    id INTEGER PRIMARY KEY,
                    scheduled_at_utc TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    account_username TEXT NOT NULL DEFAULT '',
                    account_display_name TEXT NOT NULL DEFAULT '',
                    creator_nickname_snapshot TEXT NOT NULL DEFAULT '',
                    creator_username_snapshot TEXT NOT NULL DEFAULT '',
                    content_id TEXT NOT NULL,
                    material_id TEXT NOT NULL,
                    material_name TEXT NOT NULL DEFAULT '',
                    drama_name TEXT NOT NULL DEFAULT '',
                    material_language TEXT NOT NULL DEFAULT '',
                    material_tag TEXT NOT NULL DEFAULT '',
                    short_link_id INTEGER NOT NULL DEFAULT 0,
                    short_url TEXT NOT NULL DEFAULT '',
                    long_url TEXT NOT NULL,
                    code TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    publish_id TEXT NOT NULL,
                    publish_url TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE tt_post_event(
                    id INTEGER PRIMARY KEY,
                    queue_id INTEGER,
                    event_type TEXT NOT NULL DEFAULT '',
                    to_status TEXT NOT NULL,
                    details_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE tt_post_recurring_pool(
                    id INTEGER PRIMARY KEY,
                    queue_id INTEGER,
                    material_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    content_id TEXT NOT NULL,
                    material_name TEXT NOT NULL DEFAULT '',
                    drama_name TEXT NOT NULL DEFAULT '',
                    material_language TEXT NOT NULL DEFAULT '',
                    routing_language TEXT NOT NULL DEFAULT '',
                    material_tag TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    run_id INTEGER
                )
                """
            )
            conn.execute(
                "CREATE TABLE tt_post_direct_test("
                "id INTEGER PRIMARY KEY,code TEXT NOT NULL,status TEXT NOT NULL)"
            )
            ensure_code_route_storage(conn)
            for queue_id, status, publish_id, code in (
                (2, "published", "publish-2", ""),
                (3, "published", "publish-3", ""),
                (4, "published", "publish-4", "Z9Y8"),
                (5, "failed", "", ""),
            ):
                content_id = "CONTENT%05d" % queue_id
                conn.execute(
                    """
                    INSERT INTO tt_post_queue(
                        id,scheduled_at_utc,account_id,account_username,
                        account_display_name,creator_nickname_snapshot,
                        creator_username_snapshot,content_id,material_id,
                        material_name,drama_name,material_language,material_tag,
                        short_link_id,short_url,long_url,code,status,publish_id,
                        publish_url,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        queue_id,
                        "2026-08-0%sT08:00:00Z" % queue_id,
                        "640%s" % queue_id,
                        "creator%s" % queue_id,
                        "Creator %s current" % queue_id,
                        "Creator %s" % queue_id,
                        "creator%s" % queue_id,
                        content_id,
                        "500%s" % queue_id,
                        "Clip %s" % queue_id,
                        "Drama%s" % queue_id,
                        "en",
                        "none",
                        0,
                        "",
                        self._long_url(queue_id, content_id),
                        code,
                        status,
                        publish_id,
                        "",
                        "2026-08-0%sT07:00:00Z" % queue_id,
                        "2026-08-0%sT08:05:00Z" % queue_id,
                    ),
                )
                if status == "published":
                    conn.execute(
                        "INSERT INTO tt_post_event("
                        "id,queue_id,event_type,to_status,details_json,created_at"
                        ") VALUES(?,?,?,?,?,?)",
                        (
                            queue_id,
                            queue_id,
                            "publish_succeeded",
                            "published",
                            json.dumps({"publish_id": publish_id}),
                            "2026-08-0%sT08:04:00Z" % queue_id,
                        ),
                    )
            conn.execute(
                """
                INSERT INTO tt_post_queue(
                    id,scheduled_at_utc,account_id,account_username,
                    account_display_name,creator_nickname_snapshot,
                    creator_username_snapshot,content_id,material_id,
                    material_name,drama_name,material_language,material_tag,
                    short_link_id,short_url,long_url,code,status,publish_id,
                    publish_url,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    6,
                    "2026-08-06T08:00:00Z",
                    "6406",
                    "live.creator6",
                    "Live Creator 6",
                    "Frozen Creator 6",
                    "frozen.creator6",
                    "LEDGER00006",
                    "9006",
                    "",
                    "",
                    "",
                    "",
                    0,
                    "",
                    "",
                    "",
                    "published",
                    "publish-6",
                    "",
                    "2026-08-06T07:00:00Z",
                    "2026-08-06T08:05:00Z",
                ),
            )
            conn.execute(
                """
                INSERT INTO tt_post_recurring_pool(
                    id,queue_id,material_id,account_id,content_id,
                    material_name,drama_name,material_language,
                    routing_language,material_tag,status,run_id
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    60,
                    6,
                    "9006",
                    "6406",
                    "LEDGER00006",
                    "",
                    "",
                    "",
                    "en",
                    "",
                    "consumed",
                    106,
                ),
            )
            conn.execute(
                """
                INSERT INTO tt_post_event(
                    id,queue_id,event_type,to_status,details_json,created_at
                ) VALUES(?,?,?,?,?,?)
                """,
                (
                    60,
                    6,
                    "publish_reconciled",
                    "published",
                    json.dumps({"publish_id": "publish-6"}),
                    "2026-08-06T08:04:00Z",
                ),
            )
            conn.execute(
                "INSERT INTO tt_post_direct_test(id,code,status) VALUES(1,'','published')"
            )
            conn.commit()

    @staticmethod
    def _sha(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _inspect_ledger(self, *, queue_ids=(6,), reconstruct_ids=(6,)):
        return inspect_database(
            self.db_path,
            queue_ids=queue_ids,
            reconstruct_route_from_ledger_queue_ids=reconstruct_ids,
        )

    def _apply_ledger(
        self,
        exact,
        *,
        queue_ids=(6,),
        reconstruct_ids=(6,),
        choice_fn=lambda alphabet: alphabet[0],
    ):
        return apply_plan(
            self.db_path,
            queue_ids=queue_ids,
            expected_count=exact["candidate_count"],
            expected_hash=exact["plan_sha256"],
            reconstruct_route_from_ledger_queue_ids=reconstruct_ids,
            choice_fn=choice_fn,
        )

    def _assert_no_backfill_writes(self, *queue_ids):
        normalized = tuple(queue_ids or (self.LEDGER_QUEUE_ID,))
        placeholders = ",".join("?" for _item in normalized)
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            rows = conn.execute(
                "SELECT id,code FROM tt_post_queue WHERE id IN (%s) ORDER BY id"
                % placeholders,
                normalized,
            ).fetchall()
            self.assertEqual([row[1] for row in rows], [""] * len(rows))
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM tt_post_code_route").fetchone()[0],
                0,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT code FROM tt_post_direct_test WHERE id=1"
                ).fetchone()[0],
                "",
            )

    def test_dry_run_is_read_only_and_excludes_direct_test(self):
        before = self._sha(self.db_path)
        result = inspect_database(self.db_path, queue_ids=[2, 3])
        self.assertEqual(result["candidate_count"], 2)
        self.assertEqual(
            [item["queue_id"] for item in result["candidates"]],
            [2, 3],
        )
        self.assertEqual(before, self._sha(self.db_path))
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tt_post_code_route").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT code FROM tt_post_direct_test WHERE id=1").fetchone()[0], "")

    def test_ledger_reconstruction_requires_explicit_repeatable_queue_opt_in(self):
        before = self._sha(self.db_path)
        with self.assertRaises(TTCodeBackfillError):
            inspect_database(self.db_path, queue_ids=[self.LEDGER_QUEUE_ID])

        first = self._inspect_ledger(reconstruct_ids=[6, 6])
        second = self._inspect_ledger(reconstruct_ids=[6])
        self.assertEqual(first, second)
        self.assertEqual(first["candidate_count"], 1)
        self.assertEqual(first["candidates"][0]["queue_id"], 6)
        self.assertEqual(
            first["candidates"][0]["route_source"],
            "publish_recurring_v1",
        )
        self.assertEqual(before, self._sha(self.db_path))
        self._assert_no_backfill_writes(6)

        with self.assertRaises(TTCodeBackfillError):
            self._inspect_ledger(queue_ids=[2], reconstruct_ids=[6])

    def test_cli_ledger_queue_opt_in_is_repeatable_and_read_only(self):
        before = self._sha(self.db_path)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = backfill_module.main(
                [
                    "--db-path",
                    str(self.db_path),
                    "--queue-id",
                    "6",
                    "--reconstruct-route-from-ledger-queue-id",
                    "6",
                    "--reconstruct-route-from-ledger-queue-id",
                    "6",
                ]
            )
        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["candidate_count"], 1)
        self.assertEqual(
            payload["candidates"][0]["route_source"],
            "publish_recurring_v1",
        )
        self.assertEqual(before, self._sha(self.db_path))
        self._assert_no_backfill_writes(6)

    def test_ledger_reconstruction_requires_unique_consumed_row_with_run_id(self):
        self.assertEqual(self._inspect_ledger()["candidate_count"], 1)
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "UPDATE tt_post_recurring_pool SET run_id=NULL WHERE queue_id=6"
            )
            conn.commit()
        with self.assertRaises(TTCodeBackfillError):
            self._inspect_ledger()
        self._assert_no_backfill_writes(6)

        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "UPDATE tt_post_recurring_pool "
                "SET run_id=106,status='available' WHERE queue_id=6"
            )
            conn.commit()
        with self.assertRaises(TTCodeBackfillError):
            self._inspect_ledger()
        self._assert_no_backfill_writes(6)

        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "UPDATE tt_post_recurring_pool SET status='consumed' WHERE queue_id=6"
            )
            conn.execute(
                """
                INSERT INTO tt_post_recurring_pool(
                    id,queue_id,material_id,account_id,content_id,
                    material_name,drama_name,material_language,
                    routing_language,material_tag,status,run_id
                )
                SELECT 61,queue_id,material_id,account_id,content_id,
                       material_name,drama_name,material_language,
                       routing_language,material_tag,status,107
                FROM tt_post_recurring_pool WHERE id=60
                """
            )
            conn.commit()
        with self.assertRaises(TTCodeBackfillError):
            self._inspect_ledger()
        self._assert_no_backfill_writes(6)

    def test_ledger_reconstruction_requires_one_matching_publish_event(self):
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "UPDATE tt_post_event SET details_json=? WHERE id=60",
                (json.dumps({"publish_id": "other-publish"}),),
            )
            conn.commit()
        with self.assertRaises(TTCodeBackfillError):
            self._inspect_ledger()
        self._assert_no_backfill_writes(6)

        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "UPDATE tt_post_event SET details_json=? WHERE id=60",
                (json.dumps({"publish_id": "publish-6"}),),
            )
            conn.execute(
                """
                INSERT INTO tt_post_event(
                    id,queue_id,event_type,to_status,details_json,created_at
                ) VALUES(?,?,?,?,?,?)
                """,
                (
                    61,
                    6,
                    "publish_reconciled",
                    "published",
                    json.dumps({"publish_id": "publish-6"}),
                    "2026-08-06T08:04:01Z",
                ),
            )
            conn.commit()
        with self.assertRaises(TTCodeBackfillError):
            self._inspect_ledger()
        self._assert_no_backfill_writes(6)

    def test_ledger_identity_and_missing_event_fail_closed(self):
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "UPDATE tt_post_recurring_pool SET content_id='OTHER' WHERE queue_id=6"
            )
            conn.commit()
        with self.assertRaises(TTCodeBackfillError):
            self._inspect_ledger()
        self._assert_no_backfill_writes(6)

        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "UPDATE tt_post_recurring_pool "
                "SET content_id='LEDGER00006' WHERE queue_id=6"
            )
            conn.execute("DELETE FROM tt_post_event WHERE id=60")
            conn.commit()
        with self.assertRaises(TTCodeBackfillError):
            self._inspect_ledger()
        self._assert_no_backfill_writes(6)

    def test_ledger_fallbacks_use_frozen_account_snapshot(self):
        exact = self._inspect_ledger()
        written = self._apply_ledger(exact)
        self.assertEqual(len(written), 1)
        self.assertEqual(written[0]["route_source"], "publish_recurring_v1")

        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            route = conn.execute(
                """
                SELECT af_adset,af_adset_id,af_ad,af_ad_id,c,published_at,long_url
                FROM tt_post_code_route WHERE queue_id=6
                """
            ).fetchone()
        self.assertEqual(route["af_adset"], "Frozen Creator 6")
        self.assertEqual(route["af_adset_id"], "6406")
        self.assertEqual(route["af_ad"], "9006_contentid[LEDGER00006]")
        self.assertEqual(route["af_ad_id"], "9006")
        self.assertIn("frozen.creator6*", route["c"])
        self.assertIn(
            "noneen*LEDGER00006*none*8000000000000000006",
            route["c"],
        )
        self.assertNotIn("live.creator6", route["c"])
        self.assertEqual(route["published_at"], "2026-08-06T08:04:00Z")
        fields = dict(parse_qsl(urlsplit(route["long_url"]).query))
        self.assertEqual(fields["af_dp"], "LEDGER00006")
        self.assertEqual(fields["af_channel"], "TT")
        evidence = exact["candidates"][0]["route_evidence"]
        self.assertEqual(
            evidence["fallback_fields"]["link_id"],
            {
                "source": (
                    "legacy_short_link_namespace_plus_queue_id_surrogate"
                ),
                "value": "8000000000000000006",
            },
        )

    def test_page_name_uses_display_snapshot_but_never_live_username(self):
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "UPDATE tt_post_queue SET creator_nickname_snapshot='' WHERE id=6"
            )
            conn.commit()
        exact = self._inspect_ledger()
        self._apply_ledger(exact)
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT af_adset FROM tt_post_code_route WHERE queue_id=6"
                ).fetchone()[0],
                "Live Creator 6",
            )

    def test_live_username_cannot_replace_missing_frozen_account_snapshot(self):
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "UPDATE tt_post_queue SET creator_username_snapshot='' WHERE id=6"
            )
            conn.commit()
        with self.assertRaises(TTCodeBackfillError):
            self._inspect_ledger()
        self._assert_no_backfill_writes(6)

    def test_live_username_cannot_replace_missing_page_name_snapshot(self):
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "UPDATE tt_post_queue "
                "SET creator_nickname_snapshot='',account_display_name='' WHERE id=6"
            )
            conn.commit()
        with self.assertRaises(TTCodeBackfillError):
            self._inspect_ledger()
        self._assert_no_backfill_writes(6)

    def test_conflicting_frozen_metadata_and_noncanonical_language_fail_closed(self):
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "UPDATE tt_post_queue SET material_name='queue-name.mp4' WHERE id=6"
            )
            conn.execute(
                "UPDATE tt_post_recurring_pool "
                "SET material_name='recurring-name.mp4' WHERE queue_id=6"
            )
            conn.commit()
        with self.assertRaises(TTCodeBackfillError):
            self._inspect_ledger()
        self._assert_no_backfill_writes(6)

        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "UPDATE tt_post_queue "
                "SET material_name='',material_language='fr' WHERE id=6"
            )
            conn.execute(
                "UPDATE tt_post_recurring_pool "
                "SET material_name='',material_language='fr',routing_language='en' "
                "WHERE queue_id=6"
            )
            conn.commit()
        with self.assertRaises(TTCodeBackfillError):
            self._inspect_ledger()
        self._assert_no_backfill_writes(6)

    def test_mixed_frozen_and_ledger_reconstruction_commit_together(self):
        exact = self._inspect_ledger(queue_ids=[2, 6], reconstruct_ids=[6])
        written = self._apply_ledger(
            exact,
            queue_ids=[2, 6],
            reconstruct_ids=[6],
        )
        self.assertEqual(
            [item["route_source"] for item in written],
            ["frozen_long_url", "publish_recurring_v1"],
        )
        self.assertEqual([item["code"] for item in written], ["AAAA", "AAAB"])
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM tt_post_code_route "
                    "WHERE queue_id IN (2,6)"
                ).fetchone()[0],
                2,
            )

    def test_hash_drift_and_mid_batch_failure_leave_zero_writes(self):
        exact = self._inspect_ledger(queue_ids=[2, 6], reconstruct_ids=[6])
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "UPDATE tt_post_recurring_pool "
                "SET material_name='Ledger Clip 6' WHERE queue_id=6"
            )
            conn.commit()
        with self.assertRaises(TTCodeBackfillError) as raised:
            self._apply_ledger(
                exact,
                queue_ids=[2, 6],
                reconstruct_ids=[6],
            )
        self.assertEqual(raised.exception.code, "tt_code_backfill_plan_changed")
        self._assert_no_backfill_writes(2, 6)

        refreshed = self._inspect_ledger(queue_ids=[2, 6], reconstruct_ids=[6])
        original_allocate = backfill_module.allocate_code_route
        calls = {"count": 0}

        def fail_second_allocation(conn, queue_id, route, **_kwargs):
            calls["count"] += 1
            if calls["count"] == 2:
                raise TTCodeBackfillError("forced_test_failure", "forced")
            return original_allocate(
                conn,
                queue_id,
                route,
                choice_fn=lambda alphabet: alphabet[0],
            )

        with patch.object(
            backfill_module,
            "allocate_code_route",
            side_effect=fail_second_allocation,
        ):
            with self.assertRaises(TTCodeBackfillError) as raised:
                self._apply_ledger(
                    refreshed,
                    queue_ids=[2, 6],
                    reconstruct_ids=[6],
                )
        self.assertEqual(raised.exception.code, "forced_test_failure")
        self.assertEqual(calls["count"], 2)
        self._assert_no_backfill_writes(2, 6)

    def test_guarded_apply_backs_up_and_resolves_codes(self):
        exact = inspect_database(self.db_path, queue_ids=[2, 3])
        backup = backup_database(self.db_path, self.backup_path)
        written = apply_plan(
            self.db_path,
            queue_ids=[2, 3],
            expected_count=exact["candidate_count"],
            expected_hash=exact["plan_sha256"],
            choice_fn=lambda alphabet: alphabet[0],
        )
        self.assertEqual([item["code"] for item in written], ["AAAA", "AAAB"])
        self.assertEqual(backup["sha256"], self._sha(self.backup_path))
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            queues = conn.execute(
                "SELECT id,code,long_url FROM tt_post_queue WHERE id IN (2,3) ORDER BY id"
            ).fetchall()
            self.assertEqual([row["code"] for row in queues], ["AAAA", "AAAB"])
            self.assertTrue(all("af_channel=AIpost" in row["long_url"] for row in queues))
            routes = conn.execute(
                "SELECT code,state,af_channel,published_at,long_url "
                "FROM tt_post_code_route ORDER BY queue_id"
            ).fetchall()
            self.assertEqual([row["state"] for row in routes], ["published", "published"])
            self.assertTrue(all(row["af_channel"] == "TT" for row in routes))
            self.assertTrue(all("af_channel=TT" in row["long_url"] for row in routes))
            self.assertEqual(conn.execute("SELECT code FROM tt_post_direct_test WHERE id=1").fetchone()[0], "")
        resolved = TTCodeRouteResolver(self.db_path).resolve("AAAA", "Search")
        self.assertEqual(resolved["item"]["content_id"], "CONTENT00002")
        self.assertEqual(resolved["item"]["route_mode"], "code_exact")
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM tt_post_queue "
                    "WHERE id IN (2,3) AND code=''"
                ).fetchone()[0],
                0,
            )

    def test_changed_plan_and_incomplete_history_fail_closed(self):
        exact = inspect_database(self.db_path, queue_ids=[2, 3])
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("UPDATE tt_post_queue SET publish_id='changed' WHERE id=3")
            conn.commit()
        with self.assertRaises(TTCodeBackfillError) as raised:
            apply_plan(
                self.db_path,
                queue_ids=[2, 3],
                expected_count=exact["candidate_count"],
                expected_hash=exact["plan_sha256"],
            )
        self.assertEqual(raised.exception.code, "tt_code_backfill_plan_changed")
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("UPDATE tt_post_queue SET long_url='' WHERE id=2")
            conn.commit()
        with self.assertRaises(TTCodeBackfillError) as raised:
            inspect_database(self.db_path, queue_ids=[2])
        self.assertEqual(raised.exception.code, "tt_code_backfill_long_url_missing")

    def test_non_legacy_attribution_channel_is_rejected(self):
        original = self._long_url(2, "CONTENT00002")
        fields = dict(parse_qsl(urlsplit(original).query, keep_blank_values=True))
        rebuilt = build_w2a_url_from_fields(
            {
                key: fields[key]
                for key in (
                    "af_dp",
                    "c",
                    "af_adset",
                    "af_adset_id",
                    "af_ad",
                    "af_ad_id",
                    "af_c_id",
                )
            },
            channel="Search",
        )
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("UPDATE tt_post_queue SET long_url=? WHERE id=2", (rebuilt,))
            conn.commit()
        with self.assertRaises(TTCodeBackfillError) as raised:
            inspect_database(self.db_path, queue_ids=[2])
        self.assertEqual(raised.exception.code, "tt_code_backfill_channel_invalid")

    def test_capacity_guard_never_recycles_an_existing_route(self):
        exact = inspect_database(self.db_path, queue_ids=[2, 3])
        with patch.object(backfill_module, "CODE_ALPHABET", "A"), patch.object(
            backfill_module, "CODE_LENGTH", 1
        ):
            with self.assertRaises(TTCodeBackfillError) as raised:
                apply_plan(
                    self.db_path,
                    queue_ids=[2, 3],
                    expected_count=exact["candidate_count"],
                    expected_hash=exact["plan_sha256"],
                )
        self.assertEqual(
            raised.exception.code,
            "tt_code_backfill_capacity_exhausted",
        )
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM tt_post_code_route").fetchone()[0],
                0,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM tt_post_queue WHERE code<>''"
                ).fetchone()[0],
                1,
            )

    def test_backup_path_is_exclusive_and_preserves_existing_file(self):
        self.backup_path.write_bytes(b"operator-owned")
        with self.assertRaises(TTCodeBackfillError) as raised:
            backup_database(self.db_path, self.backup_path)
        self.assertEqual(raised.exception.code, "tt_code_backfill_backup_exists")
        self.assertEqual(self.backup_path.read_bytes(), b"operator-owned")


if __name__ == "__main__":
    unittest.main()
