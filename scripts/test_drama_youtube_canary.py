#!/usr/bin/env python3
"""No-network tests for the single internal unlisted deployment canary."""

from __future__ import annotations

import ast
import contextlib
import hashlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.drama_synthesis.core import (
    CANARY_ACCOUNT_ID, CANARY_APP_ID, CANARY_CHANNEL_ID, CANARY_CHANNEL_LOCAL_ID,
    CANARY_COMMENT, CANARY_DESCRIPTION, CANARY_OPERATION_ID, CANARY_TITLE,
    COMMENT_SCOPE, DramaSynthesisError, DramaSynthesisStore, ImmutableFilesystemPublisher,
)
from features.drama_synthesis.unified_youtube import (
    ControlledRPCExecutor, UnifiedYouTubeWriter, run_sync_outbox_once, validate_entity_payload, validate_writer_health,
)
from features.drama_synthesis.unified_youtube_rpc import UnifiedYouTubeLedger
from features.drama_synthesis.youtube import (
    YouTubeCredential, YouTubeHTTPClient, YouTubeHTTPError, YouTubePublishEngine,
)
from scripts import drama_youtube_canary as cli
from scripts.test_drama_youtube_unified_rpc import FakeConnection


JOB_ID = "c" * 32
SOURCE = "https://media.example.test/completed.mp4"
SCOPES = frozenset({COMMENT_SCOPE})
VIDEO_ID = "canary_video_1"
EXPIRES = "2099-01-01T00:00:00Z"
BLOB = b"offline-canary-fixture-mp4"


def authorized_args(action="prepare", **changes):
    args = cli.parser().parse_args([
        "--action", action, "--authorize-unlisted-canary",
        "--operation-id", CANARY_OPERATION_ID, "--confirm-app-id", CANARY_APP_ID,
        "--confirm-channel-local-id", CANARY_CHANNEL_LOCAL_ID,
        "--confirm-channel-id", CANARY_CHANNEL_ID, "--confirm-account-id", CANARY_ACCOUNT_ID,
        "--operator-user-id", "803",
    ])
    if action == "prepare":
        args.job_id = JOB_ID
        args.source_kind = "concat_video"
    for key, value in changes.items():
        setattr(args, key, value)
    return args


class FakeCredentialRepository:
    def __init__(self):
        self.calls = []
        self.value = YouTubeCredential(
            account_id=CANARY_ACCOUNT_ID, channel_local_id=CANARY_CHANNEL_LOCAL_ID,
            channel_id=CANARY_CHANNEL_ID, channel_name="Shahrul Ikmal", channel_status=1,
            scopes=SCOPES, refresh_token="PRIVATE_REFRESH_VALUE",
            client_id="PRIVATE_CLIENT_ID", client_secret="PRIVATE_CLIENT_SECRET",
        )

    def credential(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs != dict(app_id=CANARY_APP_ID, channel_local_id=CANARY_CHANNEL_LOCAL_ID,
                          account_id=CANARY_ACCOUNT_ID, expected_channel_id=CANARY_CHANNEL_ID):
            raise AssertionError("credential scope widened")
        return self.value


class FakeCanaryClient:
    def __init__(self, store):
        self.store = store
        self.begins = self.uploads = self.queries = self.comments = self.refreshes = self.downloads = 0
        self.begin_error = self.upload_error = self.comment_error = None
        self.video_states = []
        self.query_result = {"state": "submitted", "video_id": VIDEO_ID}
        self.upload_result = {"state": "submitted", "video_id": VIDEO_ID}
        self.last_begin = None

    def refresh_access_token(self, credential):
        assert credential.account_id == CANARY_ACCOUNT_ID
        self.refreshes += 1
        return "PRIVATE_ACCESS_VALUE"

    def verify_channel_identity(self, _value, expected):
        assert expected == CANARY_CHANNEL_ID

    def download(self, source_url, target, *, allowed_hosts, heartbeat):
        assert source_url == SOURCE and allowed_hosts == ("media.example.test",)
        heartbeat()
        self.downloads += 1
        target.write_bytes(BLOB)

    def begin_resumable(self, _value, **metadata):
        self.begins += 1
        self.last_begin = metadata
        row = self.store.youtube_canary_task()
        assert row["video_attempt_count"] == 1 and row["resumable_session_uri"] == ""
        assert metadata["privacy_status"] == "unlisted"
        if self.begin_error:
            raise self.begin_error
        return "https://upload.example.test/PRIVATE_SESSION_VALUE"

    def upload(self, _session_uri, _source, _offset):
        self.uploads += 1
        if self.upload_error:
            raise self.upload_error
        return dict(self.upload_result)

    def query_upload(self, _session_uri, _size):
        self.queries += 1
        if isinstance(self.query_result, Exception):
            raise self.query_result
        return dict(self.query_result)

    def video_status(self, _value, video_id, *, expected_privacy_status):
        assert video_id == VIDEO_ID and expected_privacy_status == "unlisted"
        if self.video_states:
            value = self.video_states.pop(0)
            if isinstance(value, Exception):
                raise value
            return value
        return {"state": "published", "visibility": "unlisted", "processing_status": "succeeded"}

    def publish_comment(self, _value, *, video_id, comment_text, channel_id):
        assert video_id == VIDEO_ID and comment_text == CANARY_COMMENT and channel_id == CANARY_CHANNEL_ID
        self.comments += 1
        if self.comment_error:
            raise self.comment_error
        return "canary_comment_1"


class FakeHTTPSession:
    def __init__(self, payload, *, status=200, headers=None):
        self.response = SimpleNamespace(status_code=status, headers=headers or {}, json=lambda: payload)
        self.calls = []
        self.trust_env = True

    def get(self, url, **kwargs):
        self.calls.append(("get", url, kwargs))
        return self.response

    def post(self, url, **kwargs):
        self.calls.append(("post", url, kwargs))
        return self.response

    def close(self):
        pass


class FakeCheckedExecutor:
    def __init__(self, ledger):
        self.ledger = ledger
        self.fail_writes = False
        self.health_calls = 0

    def health(self):
        self.health_calls += 1
        return self.ledger.health()

    def __call__(self, *args):
        if self.fail_writes:
            raise RuntimeError("offline writer unavailable")
        return self.ledger.execute(*args)


class CanaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "jobs.sqlite3"
        self.store = DramaSynthesisStore(self.path)
        self.store.ensure_storage()
        self.credentials = FakeCredentialRepository()
        self.job = dict(job_id=JOB_ID, app_id=CANARY_APP_ID, content_id="2284", status="done", output_video_url=SOURCE)
        self.app = SimpleNamespace(
            JOB_DB_PATH=str(self.path), DRAMA_SYNTHESIS_STORE=self.store,
            DRAMA_SHORT_LINK_PUBLISHER=ImmutableFilesystemPublisher(self.root / "short-links"),
            drama_youtube_repository=lambda: self.credentials,
            require_completed_drama_job=mock.Mock(side_effect=lambda _job_id: dict(self.job)),
            drama_youtube_source=mock.Mock(side_effect=lambda job, kind: (kind, job["output_video_url"])),
        )
        self.env = {
            "DRAMA_JOB_DB_PATH": str(self.path), "YOUTUBE_LIVE_ENABLED": "0",
            "DRAMA_YOUTUBE_UNIFIED_SYNC_ENABLED": "0", "DRAMA_YOUTUBE_WORK_ROOT": str(self.root / "uploads"),
            "DRAMA_YOUTUBE_SOURCE_HOSTS": "media.example.test",
        }
        self.client = FakeCanaryClient(self.store)
        self.engine = YouTubePublishEngine(self.store, self.credentials, self.client,
            work_root=self.root / "uploads", allowed_source_hosts=("media.example.test",))
        self.connection = FakeConnection()
        self.ledger = UnifiedYouTubeLedger(lambda: self.connection)
        self.executor = FakeCheckedExecutor(self.ledger)
        self.writer = UnifiedYouTubeWriter(self.executor)
        for target in ("requests.sessions.Session.request", "socket.create_connection"):
            patch = mock.patch(target, side_effect=AssertionError("network forbidden by canary test"))
            patch.start()
            self.addCleanup(patch.stop)
        probe = mock.patch("features.drama_synthesis.youtube.subprocess.run", return_value=SimpleNamespace(stdout='{"format":{"duration":"1.25"}}'))
        probe.start()
        self.addCleanup(probe.stop)

    def prepare(self, **changes):
        return cli.prepare_canary(self.app, authorized_args(**changes), hosts=("media.example.test",))

    def tick(self, task_id):
        return self.engine.run_once("offline-canary-worker", canary_task_id=task_id)

    def sql(self, statement, params=()):
        with contextlib.closing(sqlite3.connect(self.path)) as connection, connection:
            connection.row_factory = sqlite3.Row
            cursor = connection.execute(statement, params)
            return [dict(row) for row in cursor.fetchall()]

    def public(self):
        return self.store.enqueue_youtube(
            operation_id="public:operation-0001", job_id="d" * 32, content_id="2284", app_id=CANARY_APP_ID,
            channel_local_id=CANARY_CHANNEL_LOCAL_ID, channel_id=CANARY_CHANNEL_ID, youtube_account_id=CANARY_ACCOUNT_ID,
            source_kind="concat_video", source_url=SOURCE, title="Public title", description_template="Public description",
            description_rendered="Public description", comment_text="", duplicate_confirmed=False, scopes=SCOPES,
        )

    def test_prepare_freezes_real_job_source_and_renders_real_short_link(self):
        before = dict(self.job)
        row = self.prepare()
        self.assertEqual((row["operation_id"], row["privacy_status"], row["source_url"]), (CANARY_OPERATION_ID, "unlisted", SOURCE))
        self.assertEqual(row["title"], CANARY_TITLE)
        self.assertEqual(row["description_template"], CANARY_DESCRIPTION)
        self.assertEqual(row["comment_text"], CANARY_COMMENT)
        self.assertNotIn("{{url}}", row["description_rendered"])
        self.assertIn("https://gy.g2flow.com/s2l/youtube/1.html", row["description_rendered"])
        wrapper = (self.root / "short-links" / "1.html").read_text(encoding="utf-8")
        self.assertIn(JOB_ID, wrapper)
        self.assertEqual(self.job, before)
        self.assertEqual(self.client.refreshes, 0)

    def test_two_operators_reuse_one_stable_operation(self):
        with ThreadPoolExecutor(max_workers=2) as workers:
            records = list(workers.map(lambda who: self.prepare(operator_user_id=who), ("803", "804")))
        self.assertEqual(records[0]["id"], records[1]["id"])
        self.assertEqual(len(self.sql("SELECT * FROM drama_youtube_publish")), 1)
        self.assertEqual(len(self.sql("SELECT * FROM drama_material_short_link")), 1)
        self.assertEqual(len(list((self.root / "short-links").glob("*.html"))), 1)

    def test_second_source_cannot_replace_frozen_canary(self):
        self.prepare()
        self.job["output_video_url"] = "https://media.example.test/other.mp4"
        with self.assertRaisesRegex(cli.CanaryCLIError, "canary_frozen_source_conflict"):
            self.prepare(operator_user_id="804")
        self.assertEqual(len(self.sql("SELECT * FROM drama_youtube_publish")), 1)

    def test_target_confirmation_or_authorization_missing_stops_before_job_read(self):
        for changes in ({"authorize_unlisted_canary": False}, {"confirm_account_id": "256"},
                        {"confirm_app_id": "1"}, {"confirm_channel_local_id": "1"},
                        {"confirm_channel_id": "UC" + "A" * 22}, {"operation_id": "another-operation"}):
            with self.subTest(changes=changes), self.assertRaises(cli.CanaryCLIError):
                self.prepare(**changes)
        self.app.require_completed_drama_job.assert_not_called()
        self.assertFalse(self.sql("SELECT * FROM drama_material_short_link"))

    def test_job_must_be_same_product_and_requested_job(self):
        for key, value in (("app_id", "1"), ("job_id", "d" * 32)):
            with self.subTest(key=key), mock.patch.dict(self.job, {key: value}), self.assertRaises(cli.CanaryCLIError):
                self.prepare()
        self.assertFalse(self.sql("SELECT * FROM drama_youtube_publish"))

    def test_incomplete_job_and_unknown_source_fail_before_enqueue(self):
        self.app.require_completed_drama_job.side_effect = DramaSynthesisError("drama_job_not_completed", "not done", 409)
        with self.assertRaises(DramaSynthesisError):
            self.prepare()
        self.assertFalse(self.sql("SELECT * FROM drama_youtube_publish"))

    def test_source_must_remain_on_exact_allowlist(self):
        self.job["output_video_url"] = "https://media.example.test.attacker.invalid/video.mp4"
        with self.assertRaisesRegex(cli.CanaryCLIError, "canary_source_not_allowed"):
            self.prepare()

    def test_public_enqueue_rejects_internal_privacy_and_reserved_operation(self):
        for request in ({"privacy_status": "unlisted"}, {"_privacy_status": "unlisted"},
                        {"canary_operation_id": CANARY_OPERATION_ID}, {"operation_id": CANARY_OPERATION_ID}):
            with self.subTest(request=request), self.assertRaisesRegex(DramaSynthesisError, "内部测试"):
                self.store.enqueue_youtube(**request)

    def test_real_http_helper_cannot_select_canary_or_bypass_formal_gate(self):
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        snippet = source[source.index("def enqueue_drama_youtube_publish("):source.index("def get_job_db_connection(")]
        namespace = dict(os=os, DramaSynthesisError=DramaSynthesisError,
            require_completed_drama_job=self.app.require_completed_drama_job,
            drama_youtube_source=self.app.drama_youtube_source,
            drama_youtube_repository=self.app.drama_youtube_repository,
            DRAMA_SYNTHESIS_STORE=self.store, DRAMA_SHORT_LINK_PUBLISHER=self.app.DRAMA_SHORT_LINK_PUBLISHER)
        exec(compile(ast.parse(snippet), "formal-http-helper", "exec"), namespace)
        payload = dict(operation_id="http:operation-0001", material_kind="concat_video", app_id=CANARY_APP_ID,
            channel_local_id=CANARY_CHANNEL_LOCAL_ID, channel_id=CANARY_CHANNEL_ID, youtube_account_id=CANARY_ACCOUNT_ID,
            title="Formal title", description_template="Formal description", comment_text="", privacy_status="unlisted",
            internal_canary=True, authorize_unlisted_canary=True)
        with mock.patch.dict(os.environ, {"YOUTUBE_LIVE_ENABLED": "0"}), self.assertRaisesRegex(DramaSynthesisError, "真实发布"):
            namespace["enqueue_drama_youtube_publish"](JOB_ID, payload)
        self.app.require_completed_drama_job.assert_not_called()
        with mock.patch.dict(os.environ, {"YOUTUBE_LIVE_ENABLED": "1"}):
            created = namespace["enqueue_drama_youtube_publish"](JOB_ID, payload)
            self.assertEqual(self.store.youtube_task(created["id"])["privacy_status"], "public")
            with self.assertRaisesRegex(DramaSynthesisError, "内部测试"):
                namespace["enqueue_drama_youtube_publish"](JOB_ID, dict(payload, operation_id=CANARY_OPERATION_ID))

    def test_normal_worker_and_browser_task_list_skip_canary(self):
        self.prepare()
        self.assertIsNone(self.store.claim_youtube("formal", EXPIRES))
        self.assertEqual(self.store.youtube_tasks_for_job(JOB_ID), [])
        public = self.public()
        self.assertEqual(self.store.claim_youtube("formal", EXPIRES)["id"], public["id"])

    def test_canary_claim_requires_exact_task_and_valid_frozen_identity(self):
        row = self.prepare()
        public = self.public()
        with self.assertRaises(DramaSynthesisError):
            self.store.claim_youtube_canary("canary", EXPIRES, public["id"])
        self.sql("UPDATE drama_youtube_publish SET youtube_account_id='999' WHERE id=?", (row["id"],))
        with self.assertRaises(DramaSynthesisError):
            self.tick(row["id"])
        self.assertEqual(self.client.refreshes, 0)

    def test_claim_is_fenced_across_two_cli_workers(self):
        row = self.prepare()
        first = self.store.claim_youtube_canary("worker1", EXPIRES, row["id"])
        self.assertIsNone(self.store.claim_youtube_canary("worker2", EXPIRES, row["id"]))
        with self.assertRaises(DramaSynthesisError):
            self.store.mark_canary_upload_intent(row["id"], worker_id="worker2", lease_generation=first["lease_generation"])

    def test_happy_path_one_upload_one_comment_three_exact_unlisted_records(self):
        row = self.prepare()
        args = authorized_args("run", canary_task_id=row["id"])
        submitted = cli.run_canary(self.app, args, env=self.env, engine=self.engine, writer=self.writer)
        self.assertEqual(submitted["status"], "submitted")
        finished = cli.run_canary(self.app, args, env=self.env, engine=self.engine, writer=self.writer)
        self.assertTrue(cli.safe_result(finished, "run")["complete"])
        for operator in ("803", "804"):
            cli.run_canary(self.app, authorized_args("run", canary_task_id=row["id"], operator_user_id=operator),
                           env=self.env, engine=self.engine, writer=self.writer)
        self.assertEqual((self.client.begins, self.client.uploads, self.client.comments), (1, 1, 1))
        self.assertEqual((finished["video_attempt_count"], finished["comment_attempt_count"]), (1, 1))
        self.assertEqual([len(rows) for rows in self.connection.rows.values()], [1, 1, 1])
        video = self.connection.rows["ads_youtube_videos"][0]
        self.assertEqual(video["privacy_status"], "unlisted")
        self.assertEqual(video["channel_id"], 263)
        log = json.loads(self.connection.rows["ads_youtube_publish_log"][0]["log"])
        self.assertEqual(log["canary_operation_id"], CANARY_OPERATION_ID)
        self.assertEqual(log["privacy_status"], "unlisted")

    def test_prepare_does_not_open_live_gate(self):
        with mock.patch.dict(os.environ, self.env, clear=True):
            self.prepare()
            self.assertEqual(os.environ["YOUTUBE_LIVE_ENABLED"], "0")

    def test_run_requires_writer_before_upload(self):
        row = self.prepare()
        with self.assertRaisesRegex(cli.CanaryCLIError, "canary_unified_writer_required"):
            cli.run_canary(self.app, authorized_args("run", canary_task_id=row["id"]), env=self.env, engine=self.engine)
        self.assertEqual((self.client.begins, self.client.refreshes), (0, 0))

    def test_run_rejects_configured_executor_without_fresh_readonly_health(self):
        row = self.prepare()
        fake = mock.Mock(side_effect=RuntimeError("unavailable"))
        # A mere callable configuration is not proof the deployed writer works.
        writer = UnifiedYouTubeWriter(lambda *args: fake(*args))
        with self.assertRaises(DramaSynthesisError):
            cli.run_canary(self.app, authorized_args("run", canary_task_id=row["id"]), env=self.env, engine=self.engine, writer=writer)
        self.assertEqual((self.client.refreshes, self.client.begins, self.client.uploads), (0, 0, 0))
        self.assertEqual(self.store.youtube_canary_task()["lease_generation"], 0)
        fake.assert_not_called()

    def test_writer_health_schema_or_grant_drift_stops_before_any_youtube_call(self):
        row = self.prepare()
        for attribute in ("schema_drift", "grant_extra", "proxy_grant_extra"):
            with self.subTest(attribute=attribute), mock.patch.object(self.connection, attribute, True), self.assertRaises(Exception):
                cli.run_canary(self.app, authorized_args("run", canary_task_id=row["id"]), env=self.env, engine=self.engine, writer=self.writer)
        self.assertEqual((self.client.refreshes, self.client.begins, self.client.uploads), (0, 0, 0))
        self.assertEqual(self.store.youtube_canary_task()["video_attempt_count"], 0)
        self.assertEqual(self.store.youtube_canary_task()["status"], "queued")

    def test_run_requires_explicit_disabled_formal_gates(self):
        row = self.prepare()
        for key in ("YOUTUBE_LIVE_ENABLED", "DRAMA_YOUTUBE_UNIFIED_SYNC_ENABLED"):
            for value in ("1", ""):
                with self.subTest(key=key, value=value), self.assertRaises(cli.CanaryCLIError):
                    cli.run_canary(self.app, authorized_args("run", canary_task_id=row["id"]), env=dict(self.env, **{key: value}), engine=self.engine, writer=self.writer)
        self.assertEqual(self.client.refreshes, 0)

    def test_persisted_upload_intent_precedes_create_and_survives_runtime_crash(self):
        row = self.prepare()
        self.client.begin_error = RuntimeError("PRIVATE_SESSION_VALUE PRIVATE_REFRESH_VALUE")
        self.assertEqual(self.tick(row["id"])["status"], "unknown")
        persisted = self.store.youtube_canary_task()
        self.assertEqual(persisted["video_attempt_count"], 1)
        self.assertEqual(persisted["resumable_session_uri"], "")
        self.assertFalse(self.tick(row["id"])["claimed"])
        self.assertEqual(self.client.begins, 1)
        self.assertEqual(self.client.uploads, 0)
        self.assertEqual(len(self.sql("SELECT * FROM drama_youtube_publish_event WHERE phase='canary_upload_intent'")), 1)

    def test_crash_after_intent_before_network_stays_blocked_on_expired_lease(self):
        row = self.prepare()
        task = self.store.claim_youtube_canary("dead", "2000-01-01T00:00:00Z", row["id"])
        kwargs = dict(worker_id="dead", lease_generation=task["lease_generation"])
        self.store.advance_youtube(row["id"], "uploading", **kwargs)
        self.store.mark_canary_upload_intent(row["id"], **kwargs)
        self.assertFalse(self.tick(row["id"])["claimed"])
        self.assertEqual(self.store.youtube_canary_task()["error_code"], "youtube_canary_session_intent_unknown")
        self.assertEqual((self.client.begins, self.client.refreshes), (0, 0))

    def test_upload_session_cannot_be_set_without_intent_or_replaced(self):
        row = self.prepare()
        task = self.store.claim_youtube_canary("one", EXPIRES, row["id"])
        kwargs = dict(worker_id="one", lease_generation=task["lease_generation"])
        self.store.advance_youtube(row["id"], "uploading", **kwargs)
        with self.assertRaises(DramaSynthesisError):
            self.store.set_upload_session(row["id"], "https://upload.example.test/one", 10, **kwargs)
        self.store.mark_canary_upload_intent(row["id"], **kwargs)
        self.store.set_upload_session(row["id"], "https://upload.example.test/one", 10, **kwargs)
        with self.assertRaises(DramaSynthesisError):
            self.store.set_upload_session(row["id"], "https://upload.example.test/two", 10, **kwargs)
        self.assertTrue(self.store.youtube_canary_task()["resumable_session_uri"].endswith("/one"))

    def test_lost_upload_reply_reconciles_original_identity_without_reupload(self):
        row = self.prepare()
        self.client.upload_error = YouTubeHTTPError("offline_unknown", "unknown", unknown=True)
        self.assertEqual(self.tick(row["id"])["status"], "submitted")
        self.assertEqual(self.store.youtube_canary_task()["video_id"], VIDEO_ID)
        self.tick(row["id"])
        self.tick(row["id"])
        self.assertEqual((self.client.begins, self.client.uploads, self.client.queries, self.client.comments), (1, 1, 1, 1))

    def test_unknown_session_only_queries_even_if_resume_possible(self):
        row = self.prepare()
        self.client.upload_error = YouTubeHTTPError("offline_unknown", "unknown", unknown=True)
        self.client.query_result = {"state": "resume", "next_byte": 1}
        self.assertEqual(self.tick(row["id"])["status"], "unknown")
        for _ in range(2):
            self.assertEqual(self.tick(row["id"])["status"], "unknown")
        self.assertEqual((self.client.begins, self.client.uploads, self.client.downloads), (1, 1, 1))
        self.client.query_result = {"state": "submitted", "video_id": VIDEO_ID}
        self.assertEqual(self.tick(row["id"])["status"], "submitted")
        self.assertEqual(self.client.uploads, 1)

    def test_expired_unknown_session_never_replaced(self):
        row = self.prepare()
        self.client.upload_error = YouTubeHTTPError("offline_unknown", "unknown", unknown=True)
        self.client.query_result = {"state": "expired"}
        self.tick(row["id"])
        self.tick(row["id"])
        self.assertEqual((self.client.begins, self.client.uploads), (1, 1))
        self.assertEqual(self.store.youtube_canary_task()["status"], "unknown")

    def test_known_incomplete_upload_can_resume_only_same_session(self):
        row = self.prepare()
        self.client.upload_result = {"state": "resume", "next_byte": 1}
        self.tick(row["id"])
        self.client.query_result = {"state": "resume", "next_byte": 1}
        self.client.upload_result = {"state": "submitted", "video_id": VIDEO_ID}
        self.assertEqual(self.tick(row["id"])["status"], "submitted")
        self.assertEqual((self.client.begins, self.client.uploads), (1, 2))
        self.assertEqual(self.store.youtube_canary_task()["video_attempt_count"], 1)

    def test_source_drift_blocks_resuming_existing_session(self):
        row = self.prepare()
        self.client.upload_result = {"state": "resume", "next_byte": 1}
        self.tick(row["id"])
        (self.root / "uploads" / ("task-%s" % row["id"]) / "source.mp4").write_bytes(b"changed")
        self.assertEqual(self.tick(row["id"])["status"], "unknown")
        self.assertEqual((self.client.begins, self.client.uploads), (1, 1))

    def test_processing_waits_without_comment_or_outbox(self):
        row = self.prepare()
        self.tick(row["id"])
        self.client.video_states = [{"state": "processing", "visibility": "unlisted", "processing_status": "processing"}]
        self.assertEqual(self.tick(row["id"])["status"], "processing")
        self.assertEqual(self.client.comments, 0)
        self.assertFalse(self.sql("SELECT * FROM drama_youtube_sync_outbox"))

    def test_private_or_public_readback_stops_comment_and_sync(self):
        row = self.prepare()
        self.tick(row["id"])
        for visibility in ("private", "public"):
            self.client.video_states = [{"state": "published", "visibility": visibility, "processing_status": "succeeded"}]
            self.assertEqual(self.tick(row["id"])["status"], "unknown")
        self.assertEqual(self.client.comments, 0)
        self.assertFalse(self.sql("SELECT * FROM drama_youtube_sync_outbox"))
        self.assertEqual(self.store.youtube_canary_task()["privacy_status"], "unlisted")

    def test_privacy_changes_before_comment_leave_existing_outbox_unclaimed(self):
        row = self.prepare()
        self.tick(row["id"])
        self.client.video_states = [
            {"state": "published", "visibility": "unlisted", "processing_status": "succeeded"},
            {"state": "published", "visibility": "public", "processing_status": "succeeded"},
        ]
        failed = cli.run_canary(self.app, authorized_args("run", canary_task_id=row["id"]), env=self.env, engine=self.engine, writer=self.writer)
        self.assertEqual(failed["status"], "unknown")
        self.assertEqual(self.client.comments, 0)
        self.assertEqual([len(rows) for rows in self.connection.rows.values()], [0, 0, 0])
        self.assertEqual([item["attempt_count"] for item in self.sql("SELECT * FROM drama_youtube_sync_outbox")], [0, 0])

    def test_completed_pending_outbox_requires_fresh_privacy_and_processing_readback(self):
        row = self.prepare()
        self.tick(row["id"])
        self.tick(row["id"])
        for state in (
            {"state": "published", "visibility": "public", "processing_status": "succeeded"},
            {"state": "published", "visibility": "private", "processing_status": "succeeded"},
            {"state": "processing", "visibility": "unlisted", "processing_status": "processing"},
            YouTubeHTTPError("offline_unknown", "unknown", unknown=True),
        ):
            # Independent retry scenarios start from confirmed local video and
            # comment; none has remote visibility proof for its pending outbox.
            self.sql("UPDATE drama_youtube_publish SET status='published',video_state='published',unknown_outcome=0 WHERE id=?", (row["id"],))
            self.client.video_states = [state]
            failed = cli.run_canary(self.app, authorized_args("run", canary_task_id=row["id"]), env=self.env, engine=self.engine, writer=self.writer)
            self.assertEqual(failed["status"], "unknown")
            self.assertFalse(cli.safe_result(failed, "run")["complete"])
            self.assertEqual([len(rows) for rows in self.connection.rows.values()], [0, 0, 0])
            self.assertTrue(all(item["status"] == "pending" and item["attempt_count"] == 0 for item in self.sql("SELECT * FROM drama_youtube_sync_outbox")))

    def test_sync_hold_reconciles_same_video_without_reposting_comment(self):
        row = self.prepare()
        self.tick(row["id"])
        self.tick(row["id"])
        self.client.video_states = [{"state": "published", "visibility": "private", "processing_status": "succeeded"}]
        args = authorized_args("run", canary_task_id=row["id"])
        cli.run_canary(self.app, args, env=self.env, engine=self.engine, writer=self.writer)
        recovered = cli.run_canary(self.app, args, env=self.env, engine=self.engine, writer=self.writer)
        self.assertTrue(cli.safe_result(recovered, "run")["complete"])
        self.assertEqual((self.client.begins, self.client.uploads, self.client.comments), (1, 1, 1))

    def test_unknown_video_readback_can_reconcile_same_video_id(self):
        row = self.prepare()
        self.tick(row["id"])
        self.client.video_states = [YouTubeHTTPError("offline_unknown", "unknown", unknown=True)]
        self.assertEqual(self.tick(row["id"])["status"], "unknown")
        self.assertEqual(self.tick(row["id"])["status"], "published")
        self.assertEqual((self.client.begins, self.client.uploads, self.client.comments), (1, 1, 1))

    def test_unknown_comment_never_retries_and_formal_retry_rejected(self):
        row = self.prepare()
        self.tick(row["id"])
        self.client.comment_error = YouTubeHTTPError("offline_comment_unknown", "unknown", unknown=True)
        self.assertEqual(self.tick(row["id"])["status"], "unknown")
        self.assertFalse(self.tick(row["id"])["claimed"])
        with self.assertRaises(DramaSynthesisError):
            self.store.retry_youtube_comment(row["id"])
        self.assertEqual(self.client.comments, 1)
        self.assertEqual(self.store.youtube_canary_task()["comment_attempt_count"], 1)
        self.assertEqual(len(self.sql("SELECT * FROM drama_youtube_sync_outbox WHERE entity_kind='comment'")), 0)

    def test_unknown_comment_runtime_failure_uses_persisted_attempt(self):
        row = self.prepare()
        self.tick(row["id"])
        self.client.comment_error = RuntimeError("PRIVATE_ACCESS_VALUE")
        self.assertEqual(self.tick(row["id"])["status"], "unknown")
        self.assertFalse(self.tick(row["id"])["claimed"])
        self.assertEqual(self.client.comments, 1)

    def test_expired_comment_attempt_is_marked_unknown_without_second_post(self):
        row = self.prepare()
        self.tick(row["id"])
        task = self.store.claim_youtube_canary("dead", "2000-01-01T00:00:00Z", row["id"])
        kwargs = dict(worker_id="dead", lease_generation=task["lease_generation"])
        self.store.video_published(row["id"], VIDEO_ID, **kwargs)
        self.store.mark_comment_attempt(row["id"], **kwargs)
        self.assertFalse(self.tick(row["id"])["claimed"])
        self.assertEqual(self.store.youtube_canary_task()["comment_status"], "unknown")
        self.assertEqual(self.client.comments, 0)

    def test_public_2xx_unknown_comment_is_not_blindly_retried(self):
        row = self.public()
        self.sql("UPDATE drama_youtube_publish SET status='submitted',video_state='submitted',video_id=?,comment_text='Public comment',comment_status='queued' WHERE id=?", (VIDEO_ID, row["id"]))
        session = FakeHTTPSession({"id": "thread_only_not_comment"})
        http = YouTubeHTTPClient(session_factory=lambda: session)
        client = SimpleNamespace(
            refresh_access_token=lambda _credential: "PRIVATE_VALUE", verify_channel_identity=lambda *_args: None,
            video_status=lambda *_args: {"state": "published", "visibility": "public"},
            publish_comment=http.publish_comment,
        )
        engine = YouTubePublishEngine(self.store, self.credentials, client, work_root=self.root / "public", allowed_source_hosts=("media.example.test",))
        self.assertEqual(engine.run_once("formal")["status"], "unknown")
        self.assertFalse(engine.run_once("formal")["claimed"])
        persisted = self.store.youtube_task(row["id"])
        self.assertEqual((persisted["comment_status"], persisted["comment_attempt_count"]), ("unknown", 1))
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(session.calls[0][2]["json"]["snippet"]["channelId"], CANARY_CHANNEL_ID)

    def test_outbox_public_worker_cannot_claim_canary_and_exact_id_enforced(self):
        row = self.prepare()
        self.tick(row["id"])
        self.tick(row["id"])
        self.assertFalse(run_sync_outbox_once(self.store, self.writer, "formal-sync")["claimed"])
        with self.assertRaises(DramaSynthesisError):
            run_sync_outbox_once(self.store, self.writer, "canary-sync", canary_task_id=row["id"] + 1)
        for _ in range(3):
            self.assertEqual(run_sync_outbox_once(self.store, self.writer, "canary-sync", canary_task_id=row["id"])["status"], "synced")
        self.assertEqual(self.store.youtube_canary_task()["sync_status"], "synced")

    def test_unified_canary_marker_and_target_are_required(self):
        row = self.prepare()
        self.tick(row["id"])
        self.tick(row["id"])
        for item in self.sql("SELECT * FROM drama_youtube_sync_outbox"):
            payload = json.loads(item["payload_json"])
            self.assertEqual(validate_entity_payload(item["entity_kind"], item["external_id"], payload), payload)
            changes = ({"canary_operation_id": "another-operation"}, {"channel_local_id": 264})
            if item["entity_kind"] != "comment":
                changes += ({"app_id": 1}, {"privacy_status": "public"}, {"privacy_status": "private"})
            for change in changes:
                with self.subTest(kind=item["entity_kind"], change=change), self.assertRaises(DramaSynthesisError):
                    self.writer.sync(item["entity_kind"], item["external_id"], dict(payload, **change))
            if item["entity_kind"] != "comment":
                payload.pop("canary_operation_id")
                with self.assertRaises(DramaSynthesisError):
                    self.writer.sync(item["entity_kind"], item["external_id"], payload)

    def test_writer_validates_before_opening_connection_for_unknown_privacy(self):
        row = self.prepare()
        self.tick(row["id"])
        self.tick(row["id"])
        item = self.sql("SELECT * FROM drama_youtube_sync_outbox WHERE entity_kind='video'")[0]
        payload = dict(json.loads(item["payload_json"]), privacy_status="unexpected")
        connect = mock.Mock(side_effect=AssertionError("must validate before connecting"))
        with self.assertRaises(DramaSynthesisError):
            UnifiedYouTubeLedger(connect).execute("insert", "ads_youtube_videos", VIDEO_ID, payload)
        connect.assert_not_called()

    def test_safe_status_excludes_all_secret_and_source_fields(self):
        row = self.prepare()
        row.update(resumable_session_uri="PRIVATE_SESSION_VALUE", access_token="PRIVATE_ACCESS_VALUE",
                   refresh_token="PRIVATE_REFRESH_VALUE", error_message="PRIVATE_CLIENT_SECRET")
        output = json.dumps(cli.safe_result(row, "status"))
        for forbidden in ("PRIVATE_", "resumable", "source_url", "refresh", "access_token", "error_message"):
            self.assertNotIn(forbidden, output)

    def test_cli_status_is_readonly_and_does_not_import_app(self):
        row = self.prepare()
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        output = io.StringIO()
        with mock.patch.dict(os.environ, {"DRAMA_JOB_DB_PATH": str(self.path)}, clear=True), mock.patch.object(cli.importlib, "import_module") as loader, contextlib.redirect_stdout(output):
            code = cli.main(["--action", "status", "--canary-task-id", str(row["id"])])
        self.assertEqual(code, 0)
        loader.assert_not_called()
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), before)
        self.assertEqual(json.loads(output.getvalue())["task_id"], row["id"])

    def test_cli_no_arbitrary_url_or_secret_arguments_and_errors_redacted(self):
        for flag in ("--source-url", "--refresh-token", "--access-token", "--session-uri"):
            output = io.StringIO()
            with contextlib.redirect_stdout(output), mock.patch.object(cli, "ledger_path") as reader:
                code = cli.main(["--action", "prepare", flag, "PRIVATE_VALUE"])
            self.assertEqual(code, 2)
            self.assertNotIn("PRIVATE_VALUE", output.getvalue())
            reader.assert_not_called()

    def test_cli_requires_authorization_before_config_or_db_access(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), mock.patch.object(cli, "load_environment") as loader:
            code = cli.main(["--action", "run", "--canary-task-id", "1"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output.getvalue())["code"], "canary_explicit_authorization_required")
        loader.assert_not_called()

    def test_cli_main_prepare_suppresses_import_output_and_restores_environment(self):
        arguments = ["--action", "prepare", "--authorize-unlisted-canary", "--operation-id", CANARY_OPERATION_ID,
            "--confirm-app-id", CANARY_APP_ID, "--confirm-channel-local-id", CANARY_CHANNEL_LOCAL_ID,
            "--confirm-channel-id", CANARY_CHANNEL_ID, "--confirm-account-id", CANARY_ACCOUNT_ID,
            "--operator-user-id", "803", "--job-id", JOB_ID, "--source-kind", "concat_video"]
        output = io.StringIO()
        def load_app(_name):
            print("PRIVATE_IMPORT_VALUE")
            print("PRIVATE_STDERR_VALUE", file=sys.stderr)
            return self.app
        with mock.patch.dict(os.environ, self.env, clear=True), mock.patch.object(cli.importlib, "import_module", side_effect=load_app), contextlib.redirect_stdout(output):
            before = dict(os.environ)
            self.assertEqual(cli.main(arguments), 0)
            self.assertEqual(dict(os.environ), before)
        self.assertNotIn("PRIVATE_", output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["status"], "queued")

    def test_cli_main_import_exception_is_redacted(self):
        arguments = ["--action", "prepare", "--authorize-unlisted-canary", "--operation-id", CANARY_OPERATION_ID,
            "--confirm-app-id", CANARY_APP_ID, "--confirm-channel-local-id", CANARY_CHANNEL_LOCAL_ID,
            "--confirm-channel-id", CANARY_CHANNEL_ID, "--confirm-account-id", CANARY_ACCOUNT_ID,
            "--operator-user-id", "803", "--job-id", JOB_ID, "--source-kind", "concat_video"]
        output = io.StringIO()
        with mock.patch.dict(os.environ, self.env, clear=True), mock.patch.object(cli.importlib, "import_module", side_effect=RuntimeError("PRIVATE_EXCEPTION_VALUE")), contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(arguments), 2)
        self.assertEqual(json.loads(output.getvalue()), {"ok": False, "code": "canary_execution_failed"})
        self.assertNotIn("PRIVATE_", output.getvalue())

    def test_failed_unified_sync_is_not_reported_as_complete_or_ok(self):
        row = self.prepare()
        args = authorized_args("run", canary_task_id=row["id"])
        cli.run_canary(self.app, args, env=self.env, engine=self.engine, writer=self.writer)
        self.executor.fail_writes = True
        failed = cli.run_canary(self.app, args, env=self.env, engine=self.engine, writer=self.writer)
        result = cli.safe_result(failed, "run")
        self.assertFalse(result["ok"])
        self.assertFalse(result["complete"])
        self.assertEqual(result["blocked_reason"], "unified_sync_retry_required")
        self.executor.fail_writes = False
        recovered = cli.run_canary(self.app, args, env=self.env, engine=self.engine, writer=self.writer)
        self.assertTrue(cli.safe_result(recovered, "run")["complete"])
        self.assertEqual((self.client.begins, self.client.comments), (1, 1))

    def test_private_bootstrap_config_rejects_non_allowlisted_keys(self):
        path = self.root / "private-config.json"
        path.write_text(json.dumps({"DRAMA_JOB_DB_PATH": str(self.path)}), encoding="utf-8")
        path.chmod(0o600)
        self.assertEqual(cli.load_environment(str(path), {})["DRAMA_JOB_DB_PATH"], str(self.path))
        path.write_text(json.dumps({"UNRELATED_SECRET": "PRIVATE_VALUE"}), encoding="utf-8")
        with self.assertRaisesRegex(cli.CanaryCLIError, "canary_config_invalid"):
            cli.load_environment(str(path), {})

    def test_missing_ledger_never_created(self):
        missing = self.root / "missing.sqlite3"
        with self.assertRaises(cli.CanaryCLIError):
            cli.ledger_path({"DRAMA_JOB_DB_PATH": str(missing)})
        self.assertFalse(missing.exists())

    def test_rpc_readonly_health_uses_auth_no_proxy_redirect_and_exact_contract(self):
        credential = self.root / "rpc-credential"
        credential.write_text("f" * 48, encoding="utf-8")
        credential.chmod(0o600)
        payload = self.ledger.health()
        session = FakeHTTPSession(payload)
        executor = ControlledRPCExecutor("http://127.0.0.1:18837/v1/youtube-sync", str(credential), session_factory=lambda: session)
        self.assertEqual(executor.health(), payload)
        method, url, kwargs = session.calls[0]
        self.assertEqual((method, url), ("get", "http://127.0.0.1:18837/health"))
        self.assertFalse(kwargs["allow_redirects"])
        self.assertFalse(session.trust_env)
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer " + "f" * 48)
        for status in (301, 401, 403, 503):
            session = FakeHTTPSession(payload, status=status)
            with self.subTest(status=status), self.assertRaises(DramaSynthesisError):
                executor.health()
        for change in ({"schema": "other"}, {"writer_identity": "root@localhost"}, {"indexes_verified": False},
                       {"writable": False}, {"grant_fingerprint": ""}, {"contract": "legacy-health"}):
            with self.subTest(change=change), self.assertRaises(DramaSynthesisError):
                validate_writer_health(dict(payload, **change))
        with mock.patch.object(session, "get", side_effect=__import__("requests").Timeout("PRIVATE_VALUE")), self.assertRaises(DramaSynthesisError):
            executor.health()


class CanaryHTTPContractTests(unittest.TestCase):
    def client(self, payload, **kwargs):
        session = FakeHTTPSession(payload, **kwargs)
        return YouTubeHTTPClient(session_factory=lambda: session), session

    def test_create_unlisted_sets_privacy_without_promoting_or_notifying(self):
        client, session = self.client({}, headers={"Location": "https://www.googleapis.com/upload/session"})
        client.begin_resumable("PRIVATE_VALUE", title=CANARY_TITLE, description="test", size=100, privacy_status="unlisted")
        method, url, request = session.calls[0]
        self.assertEqual(method, "post")
        self.assertEqual(request["json"]["status"]["privacyStatus"], "unlisted")
        self.assertEqual(parse_qs(urlsplit(url).query)["notifySubscribers"], ["false"])
        self.assertEqual(len(session.calls), 1)
        self.assertFalse(session.trust_env)

    def test_default_create_and_status_remain_public(self):
        client, session = self.client({}, headers={"Location": "https://www.googleapis.com/upload/session"})
        client.begin_resumable("PRIVATE_VALUE", title="public", description="test", size=100)
        self.assertEqual(session.calls[0][2]["json"]["status"]["privacyStatus"], "public")
        client, _ = self.client({"items": [{"status": {"uploadStatus": "processed", "privacyStatus": "public"}}]})
        self.assertEqual(client.video_status("PRIVATE_VALUE", VIDEO_ID)["state"], "published")

    def test_canary_comment_passes_required_channel_and_returns_comment_not_thread_id(self):
        payload = {"id": "thread_identity", "snippet": {"channelId": CANARY_CHANNEL_ID, "videoId": VIDEO_ID,
            "topLevelComment": {"id": "comment_identity", "snippet": {"textOriginal": CANARY_COMMENT}}}}
        client, session = self.client(payload)
        self.assertEqual(client.publish_comment("PRIVATE_VALUE", video_id=VIDEO_ID, comment_text=CANARY_COMMENT, channel_id=CANARY_CHANNEL_ID), "comment_identity")
        self.assertEqual(session.calls[0][2]["json"]["snippet"], {
            "channelId": CANARY_CHANNEL_ID, "videoId": VIDEO_ID,
            "topLevelComment": {"snippet": {"textOriginal": CANARY_COMMENT}},
        })
        for invalid in ({"id": "thread_identity"}, dict(payload, snippet=dict(payload["snippet"], videoId="another_video"))):
            client, _ = self.client(invalid)
            with self.assertRaises(YouTubeHTTPError) as failure:
                client.publish_comment("PRIVATE_VALUE", video_id=VIDEO_ID, comment_text=CANARY_COMMENT, channel_id=CANARY_CHANNEL_ID)
            self.assertTrue(failure.exception.unknown)

    def test_public_comment_contract_also_requires_channel_and_real_top_level_id(self):
        channel = "UC" + "P" * 22
        payload = {"id": "thread_public", "snippet": {"channelId": channel, "videoId": VIDEO_ID,
            "topLevelComment": {"id": "comment_public"}}}
        client, session = self.client(payload)
        self.assertEqual(client.publish_comment("PRIVATE_VALUE", video_id=VIDEO_ID, comment_text="Public comment", channel_id=channel), "comment_public")
        self.assertEqual(session.calls[0][2]["json"]["snippet"]["channelId"], channel)
        for invalid in ({"id": "thread_public"}, dict(payload, snippet=dict(payload["snippet"], channelId=CANARY_CHANNEL_ID)),
                        dict(payload, snippet=dict(payload["snippet"], topLevelComment={}))):
            client, _ = self.client(invalid)
            with self.assertRaises(YouTubeHTTPError) as failure:
                client.publish_comment("PRIVATE_VALUE", video_id=VIDEO_ID, comment_text="Public comment", channel_id=channel)
            self.assertTrue(failure.exception.unknown)
        client, _ = self.client({}, status=204)
        with self.assertRaises(YouTubeHTTPError) as failure:
            client.publish_comment("PRIVATE_VALUE", video_id=VIDEO_ID, comment_text="Public comment", channel_id=channel)
        self.assertTrue(failure.exception.unknown)

    def test_unlisted_requires_id_privacy_and_succeeded_processing_details(self):
        item = {"id": VIDEO_ID, "status": {"uploadStatus": "processed", "privacyStatus": "unlisted"}, "processingDetails": {"processingStatus": "succeeded"}}
        client, session = self.client({"items": [item]})
        self.assertEqual(client.video_status("PRIVATE_VALUE", VIDEO_ID, expected_privacy_status="unlisted")["state"], "published")
        self.assertEqual(parse_qs(urlsplit(session.calls[0][1]).query)["part"], ["status,processingDetails"])
        for change in ({"id": "other_video"}, {"status": {"uploadStatus": "processed", "privacyStatus": "private"}},
                       {"status": {"uploadStatus": "processed", "privacyStatus": "public"}}):
            client, _ = self.client({"items": [dict(item, **change)]})
            with self.subTest(change=change), self.assertRaises(YouTubeHTTPError):
                client.video_status("PRIVATE_VALUE", VIDEO_ID, expected_privacy_status="unlisted")
        client, _ = self.client({"items": [dict(item, processingDetails={})]})
        self.assertEqual(client.video_status("PRIVATE_VALUE", VIDEO_ID, expected_privacy_status="unlisted")["state"], "unknown")

    def test_unlisted_processing_failure_does_not_confirm_publication(self):
        for processing, expected in (("processing", "processing"), ("failed", "failed"), ("terminated", "failed")):
            client, _ = self.client({"items": [{"id": VIDEO_ID, "status": {"uploadStatus": "processed", "privacyStatus": "unlisted"},
                                               "processingDetails": {"processingStatus": processing}}]})
            self.assertEqual(client.video_status("PRIVATE_VALUE", VIDEO_ID, expected_privacy_status="unlisted")["state"], expected)

    def test_unknown_privacy_rejected_before_network(self):
        client, session = self.client({})
        with self.assertRaises(YouTubeHTTPError):
            client.begin_resumable("PRIVATE_VALUE", title="test", description="test", size=1, privacy_status="private")
        with self.assertRaises(YouTubeHTTPError):
            client.video_status("PRIVATE_VALUE", VIDEO_ID, expected_privacy_status="private")
        self.assertFalse(session.calls)


if __name__ == "__main__":
    unittest.main()
