import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import sync


def source():
    return dict(task_id="task1", version=1, asset_id="asset1", path="/assets/asset1.jpg", sha256="a" * 64,
                tenant="tenant1", owner="person1", owner_email="person@example.com", owner_name="Person",
                content_id="content1", title="Drama", language="en", asset_created_at="2026-10-19T05:00:00Z",
                width=1664, height=936, category="16:9")


class FakeCatalog:
    def __init__(self):
        self.intakes, self.finals = [], []
        self.people_map = {"person@example.com": [dict(id=123, username="person", email="person@example.com")]}
        self.fail = False

    def people(self, emails):
        return self.people_map

    def rows(self, where, limit=100):
        if self.fail:
            raise RuntimeError("read_failed")
        return self.intakes if where.startswith("data_source") else self.finals


class FakePublisher:
    def __init__(self):
        self.uploads, self.posts = [], []
        self.exception = False
        self.before_post = None

    def upload(self, source, request, key):
        self.uploads.append(request)

    def post(self, items):
        self.posts.append(items)
        if self.before_post:
            self.before_post()
        if self.exception:
            raise TimeoutError()
        return {"accepted": True, "http_status": 200, "code": 0}


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = sync.Store(Path(self.directory.name) / "state.sqlite3")
        self.catalog, self.publisher = FakeCatalog(), FakePublisher()
        self.env = {"COS_DOMAIN": "example.com"}

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def row(self):
        return dict(self.store.connection.execute("SELECT * FROM cover_sync").fetchone())

    def tick(self):
        sync.tick(self.store, self.catalog, self.publisher, self.env)

    def test_trial_identity_matches_previous_authorized_request(self):
        s = source()
        s.update(task_id="64737cf7b3c67eeb09d40acc3248c88e", asset_id="2a5147fb547749e9ad79e838ad98a1f9")
        self.assertEqual(sync.identity(s)[1], "362197942320522293")

    def test_add_idempotent_and_source_mutation_fails(self):
        self.assertEqual(self.store.add([source()]), 1)
        self.assertEqual(self.store.add([source()]), 0)
        altered = dict(source(), sha256="b" * 64)
        with self.assertRaisesRegex(ValueError, "frozen_source"):
            self.store.add([altered])

    def test_publication_gate_rejects_unknown_and_scheduled(self):
        ledger = dict(workflow="reviewed_thumbnail", video_state="published", video_id="12345678901",
                      public_status="succeeded", thumbnail_status="succeeded", processing_status="succeeded", unknown_outcome=0)
        self.assertTrue(sync.is_public(ledger))
        for key, value in (("unknown_outcome", 1), ("video_state", "submitted"), ("thumbnail_status", "pending"),
                           ("public_status", "pending"), ("video_id", ""), ("workflow", "legacy")):
            self.assertFalse(sync.is_public(dict(ledger, **{key: value})), key)

    def test_original_square_and_near_wide_dimensions_preserved(self):
        self.assertEqual(sync.image_category(690, 690), "1:1")
        self.assertEqual(sync.image_category(1672, 941), "16:9")
        request, _ = sync.payload(dict(source(), category="1:1", width=690, height=690), dict(id=123, username="person"), "example.com")
        self.assertEqual(request["category"], "1:1")
        self.assertIn("-1:1-YouTube-", request["name"])

    def test_outbound_uncertainty_committed_before_post(self):
        self.store.add([source()])
        self.publisher.before_post = lambda: self.assertEqual((self.row()["state"], self.row()["attempts"]), ("unknown", 1))
        self.tick()
        self.assertEqual(self.row()["state"], "accepted")
        self.assertEqual(len(self.publisher.posts), 1)

    def test_timeout_does_not_resend_on_next_turn_or_restart(self):
        self.store.add([source()])
        self.publisher.exception = True
        self.tick()
        self.store.change(self.row()["key"], "unknown", next_check=0)
        self.tick()
        self.assertEqual(len(self.publisher.posts), 1)
        self.assertEqual(self.row()["attempts"], 1)
        self.assertEqual(self.row()["state"], "unknown")

    def test_crash_after_begin_attempt_never_resends(self):
        self.store.add([source()])
        request, key = sync.payload(source(), dict(username="person", id=123), "example.com")
        self.store.change(self.row()["key"], "ready", request=sync.encoded(dict(payload=request, person_id=123, cos_key=key)))
        self.store.begin_attempt([self.row()])
        self.tick()
        self.assertEqual(self.publisher.posts, [])

    def make_existing(self, mismatch=None):
        s = source()
        request, _ = sync.payload(s, dict(username="person", id=123), "example.com")
        self.catalog.intakes = [dict(id=111, data_source_id=sync.identity(s)[1], name=request["name"],
                                     url=request["url"], category=request["category"], remark="222")]
        final = dict(id=222, product="Drama-AI素材", category="16:9", type=1, data_source=6,
                     data_source_id="content1", name=request["name"], is_delete=0, user_id=123, designer="123", initiator=123)
        final.update(mismatch or {})
        self.catalog.finals = [final]

    def test_existing_trial_adopted_without_upload_or_post(self):
        self.store.add([source()])
        self.make_existing()
        self.tick()
        self.assertEqual(self.row()["final_id"], 222)
        self.assertEqual(self.row()["state"], "confirmed")
        self.assertEqual(self.publisher.posts, [])
        self.assertEqual(self.publisher.uploads, [])

    def test_wrong_final_owner_blocks_without_overwriting(self):
        self.store.add([source()])
        self.make_existing({"designer": "999"})
        self.tick()
        self.assertEqual(self.row()["state"], "blocked")
        self.assertIn("designer", self.row()["error"])
        self.assertFalse(self.publisher.posts)

    def test_duplicate_intake_blocks(self):
        self.store.add([source()])
        self.make_existing()
        self.catalog.intakes.append(copy.deepcopy(self.catalog.intakes[0]))
        self.tick()
        self.assertEqual(self.row()["state"], "blocked")

    def test_missing_or_ambiguous_people_fail_closed(self):
        self.store.add([source()])
        self.catalog.people_map = {}
        self.tick()
        self.assertEqual(self.row()["state"], "blocked")
        self.assertFalse(self.publisher.posts)

    def test_catalog_failure_stops_before_writes(self):
        self.store.add([source()])
        self.catalog.fail = True
        with self.assertRaises(RuntimeError):
            self.tick()
        self.assertFalse(self.publisher.uploads)
        self.assertFalse(self.publisher.posts)

    def test_preupload_transient_error_retries_only_before_material_post(self):
        self.store.add([source()])
        with patch.object(self.publisher, "upload", side_effect=RuntimeError("temporary")):
            self.tick()
        self.assertEqual((self.row()["state"], self.row()["attempts"]), ("pending", 0))
        self.assertFalse(self.publisher.posts)
        self.store.change(self.row()["key"], "pending", next_check=0)
        self.tick()
        self.assertEqual(len(self.publisher.posts), 1)

    def test_mysql_wrapper_and_read_only_port(self):
        env = dict(ADMIN_MAPPING_MYSQL_HOST="101.32.56.53", ADMIN_MAPPING_MYSQL_PORT="63350",
                   ADMIN_MAPPING_MYSQL_USER="reader", ADMIN_MAPPING_MYSQL_PASSWORD="test-secret")
        catalog = sync.Catalog(env)
        fake = type("Result", (), dict(returncode=0, stdout='1\n{"id":123}\n'))()
        with patch("sync.subprocess.run", return_value=fake) as run:
            self.assertEqual(catalog.query("SELECT JSON_OBJECT('id',123)"), [{"id": 123}])
            args = run.call_args[0][0]
            self.assertEqual(args[0], "/usr/bin/mysql")
            self.assertNotIn("test-secret", str(args))
            self.assertNotIn("SQL_GATE_BYPASS", run.call_args[1]["env"])
        with self.assertRaises(ValueError):
            sync.Catalog(dict(env, ADMIN_MAPPING_MYSQL_PORT="63353"))

    def test_sql_parameters_have_explicit_collation_and_no_raw_input(self):
        value = "quote'中文"
        self.assertIn("COLLATE utf8mb4_bin", sync.literal(value))
        self.assertNotIn(value, sync.literal(value))

    def test_post_acceptance_is_not_final_confirmation(self):
        self.store.add([source()])
        self.tick()
        self.assertEqual(self.row()["state"], "accepted")
        self.assertIsNone(self.row()["final_id"])

    def test_readback_wrong_content_is_blocked(self):
        self.store.add([source()])
        self.make_existing({"data_source_id": "other"})
        self.tick()
        self.assertEqual(self.row()["state"], "blocked")


class SourceSnapshotTests(unittest.TestCase):
    def setUp(self):
        from PIL import Image
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.assets = self.root / "assets"
        self.assets.mkdir()
        self.path = self.assets / "cover.jpg"
        Image.new("RGB", (160, 90), "red").save(self.path)
        self.digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.db = self.root / "source.sqlite3"
        self.connection = sqlite3.connect(self.db)
        self.connection.executescript('''
          CREATE TABLE drama_youtube_publish (preparation_id,workflow,video_state,unknown_outcome,
            public_status,thumbnail_status,processing_status,video_id,approved_cover_path,approved_cover_sha256);
          CREATE TABLE youtube_auto_asset (id,tenant,owner,path,sha256,created_at);
          CREATE TABLE drama_admin_user (tenant_key,user_id,name,email);
          CREATE TABLE youtube_auto_preparation (id,tenant,owner,body,created_at);
        ''')
        self.connection.execute("INSERT INTO drama_admin_user VALUES('t','u','User','person@example.com')")
        self.connection.execute("INSERT INTO youtube_auto_asset VALUES('a','t','u',?,?,'2026-10-19T00:00:00Z')", (str(self.path), self.digest))
        body = dict(versions=[dict(approved=True,asset_id="a",number=1,source="ai")],
                    material=dict(app_id="1479", content_id="c", macro_name="Drama", language="en"))
        for task, state, unknown in (("published", "published", 0), ("scheduled", "submitted", 0), ("unknown", "published", 1)):
            self.connection.execute("INSERT INTO youtube_auto_preparation VALUES(?,'t','u',?,'2026-10-19')", (task, json.dumps(body)))
            self.connection.execute("INSERT INTO drama_youtube_publish VALUES(?,'reviewed_thumbnail',?,?,'succeeded','succeeded','succeeded','12345678901',?,?)", (task,state,unknown,str(self.path),self.digest))
        self.connection.commit()

    def tearDown(self):
        self.connection.close()
        self.temp.cleanup()

    def test_explicit_backfill_includes_approved_unknown_and_scheduled_but_live_does_not(self):
        all_items, errors = sync.candidates(self.db, self.assets, "approved")
        self.assertEqual(len(all_items), 3)
        self.assertFalse(errors)
        live, errors = sync.candidates(self.db, self.assets, "published")
        self.assertEqual([r["task_id"] for r in live], ["published"])
        self.assertFalse(errors)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM youtube_auto_preparation").fetchone()[0], 3)

    def test_published_asset_must_match_frozen_platform_cover(self):
        self.connection.execute("UPDATE drama_youtube_publish SET approved_cover_sha256='wrong' WHERE preparation_id='published'")
        self.connection.commit()
        live, errors = sync.candidates(self.db, self.assets, "published")
        self.assertFalse(live)
        self.assertEqual(errors[0]["reason"], "published_cover_mismatch")

    def test_wrong_owner_asset_is_excluded(self):
        self.connection.execute("UPDATE youtube_auto_asset SET owner='someone-else'")
        self.connection.commit()
        items, errors = sync.candidates(self.db, self.assets, "approved")
        self.assertFalse(items)
        self.assertEqual(len(errors), 3)


if __name__ == "__main__":
    unittest.main()
