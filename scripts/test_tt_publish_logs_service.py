#!/usr/bin/env python3
"""Offline tests for the unified TT publish-log read model."""

from __future__ import annotations

import json
import contextlib
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.tt_auto_posts.core import AuditActor, TTPostAutoStore  # noqa: E402
from features.tt_auto_posts.legacy_reader import (  # noqa: E402
    LegacyTTPostReader,
    LegacyTTPostReaderError,
)
from features.tt_auto_posts.service import (  # noqa: E402
    AutoPostServiceError,
    TTAutoPostService,
)


QUEUE_COLUMNS = """
    id INTEGER PRIMARY KEY, scheduled_at_utc TEXT, account_id TEXT,
    account_username TEXT, account_display_name TEXT,
    creator_nickname_snapshot TEXT, creator_username_snapshot TEXT,
    content_id TEXT, material_id TEXT, material_name TEXT, drama_name TEXT,
    material_language TEXT, caption TEXT, code TEXT, privacy_level TEXT,
    allow_comment INTEGER, allow_duet INTEGER, allow_stitch INTEGER,
    brand_content_toggle INTEGER, brand_organic_toggle INTEGER,
    is_aigc INTEGER, status TEXT, publish_id TEXT, publish_url TEXT,
    error_code TEXT, error_message TEXT, unknown_outcome INTEGER,
    created_at TEXT, updated_at TEXT
"""
DIRECT_COLUMNS = """
    id INTEGER PRIMARY KEY, account_id TEXT, account_username TEXT,
    account_display_name TEXT, creator_nickname_snapshot TEXT,
    creator_username_snapshot TEXT, content_id TEXT, material_id TEXT,
    material_name TEXT, drama_name TEXT, material_language TEXT,
    caption TEXT, privacy_level TEXT, allow_comment INTEGER,
    allow_duet INTEGER, allow_stitch INTEGER, brand_content_toggle INTEGER,
    brand_organic_toggle INTEGER, is_aigc INTEGER, status TEXT,
    publish_id TEXT, publish_url TEXT, error_code TEXT, error_message TEXT,
    unknown_outcome INTEGER, created_at TEXT, updated_at TEXT,
    prepared_at_utc TEXT, publish_started_at_utc TEXT,
    published_at_utc TEXT, failed_at_utc TEXT, canceled_at_utc TEXT
"""


class DummyAccounts:
    @staticmethod
    def as_account_source():
        return object()


class UnifiedPublishLogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.legacy_path = root / "legacy.sqlite3"
        self.auto_store = TTPostAutoStore(
            root / "auto.sqlite3",
            now_fn=lambda: datetime(2026, 8, 6, 4, 0, tzinfo=timezone.utc),
        )
        self._create_legacy()
        self.legacy_reader = LegacyTTPostReader(self.legacy_path)
        self.service = TTAutoPostService(
            self.auto_store,
            self.legacy_reader,
            DummyAccounts(),
            object(),
            object(),
            runner_kick_path=root / "manual-kick",
        )
        self._create_auto()

    def _create_legacy(self):
        with contextlib.closing(sqlite3.connect(self.legacy_path)) as conn:
            conn.execute("CREATE TABLE tt_post_queue(%s)" % QUEUE_COLUMNS)
            conn.execute("CREATE TABLE tt_post_direct_test(%s)" % DIRECT_COLUMNS)
            conn.execute(
                """
                INSERT INTO tt_post_queue VALUES (
                    57,'2026-08-06T08:00:00Z','640','pool640','Pool 640',
                    'Pool Creator','poolcreator','DRAMA-POOL','5001','Pool clip',
                    'Pool Drama','en','Pool caption','A1B2','PUBLIC_TO_EVERYONE',
                    1,0,0,0,0,0,'published','pool-publish',
                    'https://www.tiktok.com/@pool/video/1','','',0,
                    '2026-08-06T07:00:00Z','2026-08-06T08:05:00Z'
                )
                """
            )
            conn.execute(
                """
                INSERT INTO tt_post_queue VALUES (
                    58,'2026-08-06T04:00:00Z','644','pool644','Pool 644',
                    'Pool Creator 2','poolcreator2','DRAMA-POOL-2','5003','Pool clip 2',
                    'Pool Drama 2','en','Scheduled caption','C3D4','PUBLIC_TO_EVERYONE',
                    1,0,0,0,0,0,'scheduled','','','','',0,
                    '2026-08-06T03:00:00Z','2026-08-06T03:00:00Z'
                )
                """
            )
            conn.execute(
                """
                INSERT INTO tt_post_direct_test VALUES (
                    9,'641','direct641','Direct 641','Direct Creator','directcreator',
                    'DRAMA-DIRECT','5002','Direct clip','Direct Drama','en',
                    'Direct caption','SELF_ONLY',1,0,0,0,0,0,'failed','','',
                    'direct_failed','safe failure',0,
                    '2026-08-06T06:00:00+00:00','2026-08-06T06:05:00+00:00',
                    '','','','2026-08-06T06:05:00+00:00',''
                )
                """
            )
            conn.commit()

    def _template(self):
        return self.auto_store.create_template(
            name="Auto Template",
            description="",
            config={"source_account_ids": ["642", "643"]},
            actor=AuditActor("803", "operator"),
            confirmation={"accepted": True, "version": "tt-auto-v1"},
        )

    def _create_auto(self):
        template = self._template()
        manual_run = self.auto_store.create_run(
            run_key="manual-log",
            template_id=template.id,
            template_version=template.version,
            trigger_type="manual",
            scheduled_at_utc="2026-08-06T07:00:00+00:00",
            shanghai_date="2026-08-06",
            publish_time="15:00",
            blacklist_snapshot={},
            actor=AuditActor("803", "operator"),
        )
        manual_task = self.auto_store.create_task(
            run_id=manual_run.id,
            account_id="642",
            account_username="auto642",
            account_display_name="Auto 642",
            drama_language="en",
            account_setting_version=3,
            account_settings={
                "privacy_level": "PUBLIC_TO_EVERYONE",
                "allow_comment": True,
                "is_aigc": True,
            },
        )
        self.auto_store.reserve_material(
            manual_task.id,
            material_id="6001",
            content_id="DRAMA-AUTO-MANUAL",
            series_code="SERIES-MANUAL",
            selection={
                "drama": {"name": "Manual Drama", "language": "en"},
                "material": {
                    "material_name": "Manual clip",
                    "media_url": "https://private.example/manual.mp4",
                },
            },
        )
        with contextlib.closing(sqlite3.connect(self.auto_store.db_path)) as conn:
            conn.execute(
                """
                UPDATE tt_auto_task
                   SET status='published',publish_id='auto-publish',
                       publish_url='https://evil.example/not-tiktok',
                       caption='Automatic caption',published_at_utc=updated_at
                 WHERE id=?
                """,
                (manual_task.id,),
            )
            conn.commit()

        enabled = self.auto_store.set_template_enabled(
            template.id,
            True,
            expected_version=template.version,
            actor=AuditActor("803", "operator"),
        )
        auto_run = self.auto_store.create_run(
            run_key="scheduled-log",
            template_id=enabled.id,
            template_version=enabled.version,
            trigger_type="auto",
            scheduled_at_utc="2026-08-06T05:00:00+00:00",
            shanghai_date="2026-08-06",
            publish_time="13:00",
            blacklist_snapshot={},
            actor=AuditActor("scheduler", "scheduler"),
        )
        self.auto_store.create_task(
            run_id=auto_run.id,
            account_id="643",
            account_username="auto643",
            account_display_name="Auto 643",
            drama_language="es",
            account_setting_version=1,
            account_settings={"privacy_level": "SELF_ONLY"},
        )
        with contextlib.closing(sqlite3.connect(self.auto_store.db_path)) as conn:
            conn.execute(
                "UPDATE tt_auto_task SET status='no_candidate' WHERE run_id=?",
                (auto_run.id,),
            )
            conn.commit()

    @staticmethod
    def query(**values):
        return {key: [str(value)] for key, value in values.items()}

    def test_global_order_source_and_trigger_mapping(self):
        payload = self.service.publish_logs(self.query(limit=20, offset=0))
        self.assertEqual(
            [item["task_at_utc"] for item in payload["items"]],
            [
                "2026-08-06T08:00:00Z",
                "2026-08-06T07:00:00+00:00",
                "2026-08-06T06:00:00+00:00",
                "2026-08-06T05:00:00+00:00",
                "2026-08-06T04:00:00Z",
            ],
        )
        self.assertEqual(
            [(item["publish_source"], item["trigger_type"]) for item in payload["items"]],
            [
                ("material_pool", "scheduled"),
                ("auto_template", "manual"),
                ("material_pool", "direct_test"),
                ("auto_template", "auto"),
                ("material_pool", "scheduled"),
            ],
        )
        self.assertEqual(payload["sources"], {"material_pool": 3, "auto_template": 2})
        self.assertEqual(payload["summary"]["total"], 5)
        self.assertEqual(payload["summary"]["no_candidate"], 1)

    def test_cross_source_pagination_has_no_gap_or_duplicate(self):
        first = self.service.publish_logs(self.query(limit=2, offset=0))
        second = self.service.publish_logs(self.query(limit=2, offset=2))
        third = self.service.publish_logs(self.query(limit=2, offset=4))
        keys = [
            item["task_key"]
            for item in first["items"] + second["items"] + third["items"]
        ]
        self.assertEqual(len(keys), 5)
        self.assertEqual(len(set(keys)), 5)

    def test_source_trigger_identity_and_status_filters(self):
        automatic = self.service.publish_logs(
            self.query(publish_source="auto_template", trigger_type="manual", limit=20, offset=0)
        )
        self.assertEqual(automatic["pagination"]["total"], 1)
        self.assertEqual(automatic["items"][0]["material_id"], "6001")
        material = self.service.publish_logs(
            self.query(
                publish_source="material_pool",
                content_id="DRAMA-DIRECT",
                status="failed",
                limit=20,
                offset=0,
            )
        )
        self.assertEqual(material["pagination"]["total"], 1)
        self.assertEqual(material["items"][0]["trigger_type"], "direct_test")
        impossible = self.service.publish_logs(
            self.query(publish_source="material_pool", trigger_type="manual", limit=20, offset=0)
        )
        self.assertEqual(impossible["pagination"]["total"], 0)

        no_candidate = self.service.publish_logs(
            self.query(status="no_candidate", limit=20, offset=0)
        )
        self.assertEqual(no_candidate["pagination"]["total"], 1)
        self.assertEqual(no_candidate["items"][0]["publish_source"], "auto_template")

    def test_date_and_template_filters_use_the_unified_contract(self):
        dated = self.service.publish_logs(
            self.query(
                **{
                    "from": "2026-08-06",
                    "to": "2026-08-06",
                    "limit": 20,
                    "offset": 0,
                }
            )
        )
        self.assertEqual(dated["pagination"]["total"], 5)
        template_id = self.auto_store.list_templates()[0].id
        templated = self.service.publish_logs(
            self.query(template_id=template_id, limit=20, offset=0)
        )
        self.assertEqual(templated["pagination"]["total"], 2)
        self.assertTrue(
            all(item["publish_source"] == "auto_template" for item in templated["items"])
        )

    def test_invalid_source_status_and_deep_offset_are_rejected(self):
        for query in (
            self.query(publish_source="unknown", limit=20, offset=0),
            self.query(status="not-a-status", limit=20, offset=0),
            self.query(limit=20, offset=10001),
        ):
            with self.subTest(query=query):
                with self.assertRaises(AutoPostServiceError) as raised:
                    self.service.publish_logs(query)
                self.assertEqual(getattr(raised.exception, "status", 0), 400)

    def test_response_removes_media_urls_and_untrusted_publish_url(self):
        payload = self.service.publish_logs(
            self.query(publish_source="auto_template", trigger_type="manual", limit=20, offset=0)
        )
        serialized = json.dumps(payload, sort_keys=True)
        self.assertNotIn("private.example", serialized)
        self.assertNotIn("source_media_url", serialized)
        self.assertNotIn("prepared_media_url", serialized)
        self.assertNotIn("claim_token", serialized)
        self.assertEqual(payload["items"][0]["publish_url"], "")

    def test_missing_unrequested_legacy_source_does_not_block_auto_only(self):
        missing = LegacyTTPostReader(Path(self.temp.name) / "missing.sqlite3")
        service = TTAutoPostService(
            self.auto_store,
            missing,
            DummyAccounts(),
            object(),
            object(),
            runner_kick_path=Path(self.temp.name) / "manual-kick-2",
        )
        auto_only = service.publish_logs(
            self.query(publish_source="auto_template", limit=20, offset=0)
        )
        self.assertEqual(auto_only["pagination"]["total"], 2)
        with self.assertRaises(LegacyTTPostReaderError):
            service.publish_logs(self.query(limit=20, offset=0))


if __name__ == "__main__":
    unittest.main()
