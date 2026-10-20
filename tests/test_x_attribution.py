import base64
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from features.x_posts import attribution as attribution
from features.x_posts import attribution_migration as migration
from scripts import x_attribution_migrate as cli

STAMP = "2026-10-19T02:03:04+00:00"


class Fixture:
    def __init__(self, root, count=4):
        self.root = Path(root)
        self.x = self.root / "x.sqlite3"
        self.users = self.root / "users.sqlite3"
        self.auto = self.root / "auto.sqlite3"
        self.public = self.root / "s2l"
        self.state = self.root / "migration"
        self.public.mkdir()
        with closing(sqlite3.connect(self.x)) as db:
            db.executescript("""
            CREATE TABLE x_post_queue(
              id INTEGER PRIMARY KEY, manual_run_id INTEGER, page_id TEXT,
              page_name TEXT, material_id TEXT, material_name TEXT, content_id TEXT,
              drama_name TEXT, material_language TEXT, tag TEXT, status TEXT);
            CREATE TABLE x_post_publish_log(
              id INTEGER PRIMARY KEY, queue_id INTEGER UNIQUE, created_at TEXT,
              long_url TEXT, short_url TEXT, post_text TEXT, x_post_id TEXT,
              status TEXT, unknown_outcome INTEGER, attempt_count INTEGER);
            CREATE TABLE x_post_manual_run(
              id INTEGER PRIMARY KEY, trigger_source TEXT, actor_user_id TEXT,
              actor_tenant_key TEXT, template_ref TEXT, external_task_key TEXT);
            INSERT INTO x_post_manual_run VALUES(1,'auto_template','editor','tenant','x-auto-template-7','x-auto-task-8');
            INSERT INTO x_post_manual_run VALUES(2,'manual','editor','tenant','','');
            """)
            for index in range(1, count + 1):
                self.insert(db, index)
            db.commit()
        with closing(sqlite3.connect(self.users)) as db:
            db.executescript("""
            CREATE TABLE drama_admin_user(user_id TEXT, tenant_key TEXT, email TEXT, name TEXT);
            INSERT INTO drama_admin_user VALUES('creator','tenant','creator@example.test','模板原创建人');
            INSERT INTO drama_admin_user VALUES('editor','tenant','editor@example.test','后续编辑和运行人');
            INSERT INTO drama_admin_user VALUES('default','tenant','default@example.test','默认归属人');
            """)
        with closing(sqlite3.connect(self.auto)) as db:
            db.executescript("""
            CREATE TABLE x_auto_template(id INTEGER PRIMARY KEY, created_by_user_id TEXT,
              created_by_tenant_key TEXT, updated_by_user_id TEXT);
            CREATE TABLE x_auto_task(id INTEGER PRIMARY KEY, template_id INTEGER, execution_queue_id INTEGER);
            INSERT INTO x_auto_template VALUES(7,'creator','tenant','editor');
            INSERT INTO x_auto_task VALUES(8,7,1);
            """)
        self.ids = Mock(side_effect=lambda email: {"creator@example.test": ["42"], "editor@example.test": ["43"],
                                                 "default@example.test": ["248"]}.get(email, []))
        self.emails = Mock(return_value=["default@example.test"])
        self.resolver = attribution.Resolver(self.x, self.auto, self.users, self.ids, self.emails)

    def insert(self, db, index):
        old_url = attribution.BASE_URL + "?af_c_id=" + str(index) + "&af_channel=long&c=legacy"
        status = ["published", "failed", "needs_review", "reserved"][(index - 1) % 4]
        db.execute("INSERT INTO x_post_queue VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                   (index, index if index <= 2 else None, "100000000001", "账号 & A", str(900 + index),
                    "素材 " + str(index), str(100 + index), "剧名 " + str(index), "en", "none", status))
        db.execute("INSERT INTO x_post_publish_log VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (index, index, STAMP, old_url, "https://short.example/s2l/" + str(index) + ".html",
                    "original body " + str(index), "post-" + str(index) if status == "published" else "",
                    status, int(status == "needs_review"), 2))
        (self.public / (str(index) + ".html")).write_bytes(migration.redirect_bytes(old_url))

    def execute(self, path, statement, args=()):
        with closing(sqlite3.connect(path)) as db:
            db.execute(statement, args); db.commit()

    def rows(self, table):
        with closing(attribution.readonly(self.x)) as db:
            return [dict(row) for row in db.execute("SELECT * FROM " + table + " ORDER BY id")]

    def log(self, log_id=1):
        return next(row for row in self.rows("x_post_publish_log") if row["id"] == log_id)

    def manifest(self):
        result = migration.create_manifest(self.x, self.public, self.resolver, self.state)
        return result["manifest_sha256"]

    def facts(self):
        logs = [{key: value for key, value in row.items() if key != "long_url"} for row in self.rows("x_post_publish_log")]
        return self.rows("x_post_queue"), logs


class AttributionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fx = Fixture(self.temp.name)

    def assert_code(self, code, call):
        with self.assertRaises(attribution.AttributionError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)

    def test_template_uses_original_creator_with_trusted_tenant(self):
        # Same Feishu user ID can exist in another tenant; trusted tenant wins.
        self.fx.execute(self.fx.users, "INSERT INTO drama_admin_user VALUES('creator','other','other@example.test','other')")
        context = self.fx.resolver.resolve(1, 1)
        self.assertEqual((context["owner"], context["tenant"], context["sub_user_id"]), ("creator", "tenant", "42"))
        self.assertEqual(context["source_owner"], "creator")
        self.assertEqual(context["template_id"], 7)
        self.assertEqual(context["fallback_reason"], "")
        self.fx.emails.assert_not_called()

    def test_manual_uses_submitter_and_system_uses_default(self):
        self.assertEqual(self.fx.resolver.resolve(2, 2)["sub_user_id"], "43")
        context = self.fx.resolver.resolve(3, 3)
        self.assertEqual((context["owner"], context["sub_user_id"], context["fallback_reason"]),
                         ("default", "248", "no_publisher_relation"))

    def test_legacy_missing_tenant_only_allows_unique_identity(self):
        self.fx.execute(self.fx.auto, "UPDATE x_auto_template SET created_by_tenant_key=''")
        self.assertEqual(self.fx.resolver.resolve(1, 1)["tenant"], "tenant")
        self.fx.execute(self.fx.users, "INSERT INTO drama_admin_user VALUES('creator','other','other@example.test','other')")
        self.assert_code("x_attribution_tenant_ambiguous", lambda: self.fx.resolver.resolve(1, 1))

    def test_missing_mapping_falls_back_with_same_tenant(self):
        self.fx.ids.side_effect = lambda _email: []
        context = self.fx.resolver.resolve(1, 1)
        self.assertEqual(context["fallback_reason"], "publisher_mapping_missing")
        self.assertEqual((context["source_owner"], context["owner"], context["sub_user_id"]), ("creator", "default", "248"))

    def test_query_failure_cannot_fallback_or_cache_failure(self):
        self.fx.ids.side_effect = RuntimeError("private connection detail")
        self.assert_code("x_attribution_mapping_unavailable", lambda: self.fx.resolver.resolve(1, 1))
        self.fx.emails.assert_not_called()
        self.fx.ids.side_effect = lambda _email: ["42"]
        self.assertEqual(self.fx.resolver.resolve(1, 1)["sub_user_id"], "42")

    def test_invalid_and_ambiguous_mapping_cannot_fallback(self):
        for result, code in [(["42", "43"], "x_attribution_user_ambiguous"), (None, "x_attribution_mapping_unavailable"),
                             ("42", "x_attribution_mapping_unavailable"), (["0"], "x_attribution_identity_invalid")]:
            with self.subTest(result=result):
                self.fx.resolver._id_cache.clear()
                self.fx.ids.side_effect = None
                self.fx.ids.return_value = result
                self.assert_code(code, lambda: self.fx.resolver.resolve(1, 1))
        self.fx.emails.assert_not_called()

    def test_default_owner_must_be_unique_and_same_tenant(self):
        self.fx.execute(self.fx.users, "UPDATE drama_admin_user SET tenant_key='other' WHERE user_id='default'")
        self.fx.ids.side_effect = lambda _email: []
        self.assert_code("x_attribution_default_owner_unavailable", lambda: self.fx.resolver.resolve(1, 1))

    def test_missing_publisher_or_email_falls_back(self):
        self.fx.execute(self.fx.users, "DELETE FROM drama_admin_user WHERE user_id='creator'")
        self.assertEqual(self.fx.resolver.resolve(1, 1)["fallback_reason"], "publisher_not_found")
        self.fx.execute(self.fx.users, "UPDATE drama_admin_user SET email='' WHERE user_id='editor'")
        self.assertEqual(self.fx.resolver.resolve(2, 2)["fallback_reason"], "publisher_email_missing")

    def test_auto_task_must_match_canonical_execution_queue(self):
        self.fx.execute(self.fx.auto, "UPDATE x_auto_task SET execution_queue_id=NULL")
        self.assert_code("x_attribution_template_task_conflict", lambda: self.fx.resolver.resolve(1, 1))

    def test_url_exact_parameters_encoding_and_timestamp(self):
        context = self.fx.resolver.resolve(1, 1)
        url = attribution.build_url(context, self.fx.log())
        query = parse_qs(urlsplit(url).query)
        self.assertEqual(set(query), {"c", "af_adset", "af_adset_id", "af_ad", "af_ad_id", "af_channel", "af_c_id", "af_dp"})
        self.assertEqual(query["af_channel"], ["42"])
        self.assertEqual(query["af_c_id"], ["1"])
        self.assertEqual(query["af_adset"], ["账号 & A"])
        self.assertIn("*" + str(attribution.epoch(STAMP)) + "noneen*", query["c"][0])
        self.assertEqual(query["af_ad"], ["素材 1_contentid[101]"])
        self.assertIn("%20", url)
        self.assertNotIn("+", url)

    def test_invalid_timestamp_and_campaign_delimiter_rejected(self):
        context = self.fx.resolver.resolve(1, 1)
        for invalid in ("missing", "2026-10-19T02:03:04", None, "1900-01-01T00:00:00Z"):
            with self.subTest(invalid=invalid):
                self.assert_code("x_attribution_timestamp_invalid", lambda: attribution.epoch(invalid))
        for key, value in [("drama_name", "Drama*fake"), ("owner", ""), ("channel_name", "name\x7f")]:
            changed = dict(context, **{key: value})
            self.assert_code("x_attribution_text_invalid", lambda: attribution.build_url(changed, self.fx.log()))

    def test_freeze_retries_reuse_original_relationship(self):
        context = self.fx.resolver.resolve(1, 1)
        attribution.freeze(self.fx.x, self.fx.log(), context)
        self.fx.execute(self.fx.auto, "UPDATE x_auto_template SET created_by_user_id='editor'")
        self.fx.ids.side_effect = RuntimeError("lookup now unavailable")
        self.assertEqual(self.fx.resolver.resolve(1, 1), context)
        self.assertEqual(attribution.freeze(self.fx.x, self.fx.log(), context), context)
        self.assert_code("x_attribution_frozen_conflict",
                         lambda: attribution.freeze(self.fx.x, self.fx.log(), dict(context, owner="editor")))

    def test_frozen_version_cid_and_json_are_validated(self):
        context = self.fx.resolver.resolve(1, 1)
        attribution.freeze(self.fx.x, self.fx.log(), context)
        self.fx.execute(self.fx.x, "UPDATE x_post_link_attribution SET context_json='[]'")
        self.assert_code("x_attribution_version_conflict", lambda: attribution.load_frozen(self.fx.x, 1))
        self.fx.execute(self.fx.x, "UPDATE x_post_link_attribution SET context_json=?", (json.dumps(dict(context, af_c_id="2")),))
        self.assert_code("x_attribution_identity_conflict", lambda: attribution.load_frozen(self.fx.x, 1))
        self.fx.execute(self.fx.x, "UPDATE x_post_link_attribution SET context_json='{'")
        self.assert_code("x_attribution_frozen_invalid", lambda: attribution.load_frozen(self.fx.x, 1))

    def test_freeze_rejects_wrong_publication_identity(self):
        context = self.fx.resolver.resolve(1, 1)
        self.assert_code("x_attribution_publication_conflict", lambda: attribution.freeze(self.fx.x, dict(self.fx.log(), id=999), context))
        self.assert_code("x_attribution_identity_conflict", lambda: attribution.build_url(dict(context, af_c_id="2"), self.fx.log()))


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fx = Fixture(self.temp.name)
        self.old_facts = self.fx.facts()
        self.old_urls = {row["id"]: row["long_url"] for row in self.fx.rows("x_post_publish_log")}
        self.old_files = {path.name: path.read_bytes() for path in self.fx.public.iterdir()}

    def assert_restored(self):
        self.assertEqual(self.fx.facts(), self.old_facts)
        self.assertEqual({row["id"]: row["long_url"] for row in self.fx.rows("x_post_publish_log")}, self.old_urls)
        self.assertEqual({path.name: path.read_bytes() for path in self.fx.public.iterdir()}, self.old_files)

    def test_full_migration_repeat_rollback_preserve_all_publication_facts(self):
        digest = self.fx.manifest()
        for _ in range(2):
            self.assertEqual(migration.run(self.fx.state, digest)["count"], 4)
            self.assertEqual(self.fx.facts(), self.old_facts)
            for row in self.fx.rows("x_post_publish_log"):
                self.assertEqual(migration.target_from_bytes((self.fx.public / (str(row["id"]) + ".html")).read_bytes()), row["long_url"])
                self.assertEqual(parse_qs(urlsplit(row["long_url"]).query)["af_c_id"], [str(row["queue_id"])])
        for _ in range(2):
            migration.run(self.fx.state, digest, rollback=True)
            self.assert_restored()

    def test_file_and_database_crashes_resume_and_rollback(self):
        digest = self.fx.manifest()
        for phase in ("file_applied", "database_applied"):
            def fault(log_id, at):
                if log_id == 2 and at == phase:
                    raise RuntimeError("injected interruption")
            with self.assertRaisesRegex(RuntimeError, "interruption"):
                migration.run(self.fx.state, digest, fault=fault)
            self.assertEqual(self.fx.facts(), self.old_facts)
            migration.run(self.fx.state, digest)
            migration.run(self.fx.state, digest, rollback=True)
            self.assert_restored()

    def test_rollback_can_itself_resume_after_file_or_database_crash(self):
        digest = self.fx.manifest()
        for phase in ("rollback_file", "database_applied"):
            migration.run(self.fx.state, digest)
            def fault(log_id, at):
                if log_id == 3 and at == phase:
                    raise RuntimeError("rollback interruption")
            with self.assertRaises(RuntimeError):
                migration.run(self.fx.state, digest, rollback=True, fault=fault)
            migration.run(self.fx.state, digest, rollback=True)
            self.assert_restored()

    def test_later_publications_survive_rollback(self):
        digest = self.fx.manifest()
        migration.run(self.fx.state, digest)
        with closing(sqlite3.connect(self.fx.x)) as db:
            self.fx.insert(db, 5); db.commit()
        new_log = self.fx.log(5)
        new_bytes = (self.fx.public / "5.html").read_bytes()
        migration.run(self.fx.state, digest, rollback=True)
        self.assertEqual(self.fx.log(5), new_log)
        self.assertEqual((self.fx.public / "5.html").read_bytes(), new_bytes)

    def test_any_queue_or_post_fact_drift_blocks_before_file_mutation(self):
        digest = self.fx.manifest()
        for statement, expected in [
            ("UPDATE x_post_queue SET status='changed' WHERE id=1", "migration_queue_drift"),
            ("UPDATE x_post_publish_log SET unknown_outcome=1 WHERE id=1", "migration_publication_fact_drift"),
        ]:
            with self.subTest(statement=statement):
                self.fx.execute(self.fx.x, statement)
                with self.assertRaisesRegex(attribution.AttributionError, expected):
                    migration.run(self.fx.state, digest)
                self.assertEqual((self.fx.public / "1.html").read_bytes(), self.old_files["1.html"])
                self.fx.execute(self.fx.x, "UPDATE x_post_queue SET status='published' WHERE id=1")
                self.fx.execute(self.fx.x, "UPDATE x_post_publish_log SET unknown_outcome=0 WHERE id=1")

    def test_conflicting_frozen_queue_cannot_be_ignored(self):
        digest = self.fx.manifest()
        with closing(sqlite3.connect(self.fx.x)) as db:
            db.execute(attribution.SCHEMA)
            db.execute("INSERT INTO x_post_link_attribution VALUES(999,1,?,?,?)", (attribution.VERSION, "{}", STAMP)); db.commit()
        with self.assertRaisesRegex(attribution.AttributionError, "migration_attribution_drift"):
            migration.run(self.fx.state, digest)
        self.assertEqual((self.fx.public / "1.html").read_bytes(), self.old_files["1.html"])
        self.assertEqual(self.fx.log()["long_url"], self.old_urls[1])

    def test_database_readback_detects_silently_ignored_freeze(self):
        digest = self.fx.manifest()
        with closing(sqlite3.connect(self.fx.x)) as db:
            db.execute(attribution.SCHEMA)
            db.execute("CREATE TRIGGER prevent_freeze BEFORE INSERT ON x_post_link_attribution BEGIN SELECT RAISE(IGNORE); END"); db.commit()
        with self.assertRaisesRegex(attribution.AttributionError, "migration_readback_failed"):
            migration.run(self.fx.state, digest)
        self.assertEqual(self.fx.log()["long_url"], self.old_urls[1])
        self.fx.execute(self.fx.x, "DROP TRIGGER prevent_freeze")
        migration.run(self.fx.state, digest, rollback=True)
        self.assert_restored()

    def test_database_readback_rolls_back_trigger_modified_post_fact(self):
        digest = self.fx.manifest()
        self.fx.execute(self.fx.x, """CREATE TRIGGER change_fact AFTER UPDATE OF long_url ON x_post_publish_log
            BEGIN UPDATE x_post_publish_log SET post_text='unexpected trigger side effect' WHERE id=NEW.id; END""")
        with self.assertRaisesRegex(attribution.AttributionError, "migration_publication_fact_drift"):
            migration.run(self.fx.state, digest)
        self.assertEqual(self.fx.facts(), self.old_facts)
        self.assertEqual(self.fx.log()["long_url"], self.old_urls[1])

    def test_manifest_checksum_and_duplicate_id_fail_without_publication_writes(self):
        digest = self.fx.manifest()
        with self.assertRaisesRegex(attribution.AttributionError, "migration_manifest_hash_mismatch"):
            migration.run(self.fx.state, "0" * 64)
        data = json.loads((self.fx.state / "manifest.json").read_text(encoding="utf-8"))
        data["records"].append(data["records"][0])
        payload = migration.canonical(data).encode()
        (self.fx.state / "manifest.json").write_bytes(payload)
        digest = migration.sha(payload)
        (self.fx.state / "manifest.sha256").write_text(digest)
        with self.assertRaisesRegex(attribution.AttributionError, "migration_manifest_duplicate_identity"):
            migration.run(self.fx.state, digest)
        self.assert_restored()

    def test_resolution_failure_does_not_leave_unretryable_empty_state(self):
        self.fx.ids.side_effect = RuntimeError("unavailable")
        with self.assertRaises(attribution.AttributionError):
            self.fx.manifest()
        self.assertFalse(self.fx.state.exists())
        self.assert_restored()


class MigrationCliLockTests(unittest.TestCase):
    def test_held_publish_or_scheduler_lock_prevents_any_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            publish, scheduler = [str(Path(directory) / name) for name in ("publish.lock", "scheduler.lock")]
            for held in (publish, scheduler):
                with self.subTest(lock=held), cli.exclusive_lock(held) as acquired:
                    self.assertTrue(acquired)
                    with patch.object(cli, "run") as run:
                        with self.assertRaisesRegex(attribution.AttributionError, "migration_publisher_busy"):
                            cli.main(["--state", str(Path(directory) / "state"), "--apply", "--manifest-sha256", "0"*64,
                                      "--publish-lock", publish, "--scheduler-lock", scheduler])
                        run.assert_not_called()
            with cli.exclusive_lock(publish) as acquired:
                self.assertTrue(acquired, "failed scheduler acquisition must release the publisher lock")

    def test_both_locks_held_during_migration_and_released_after(self):
        with tempfile.TemporaryDirectory() as directory:
            publish, scheduler = [str(Path(directory) / name) for name in ("publish.lock", "scheduler.lock")]
            def run(*_args, **_kwargs):
                for name in (publish, scheduler):
                    with cli.exclusive_lock(name) as acquired:
                        self.assertFalse(acquired)
                return {"status": "verified"}
            with patch.object(cli, "run", side_effect=run), patch("sys.stdout", new=io.StringIO()):
                cli.main(["--state", str(Path(directory) / "state"), "--apply", "--manifest-sha256", "0"*64,
                          "--publish-lock", publish, "--scheduler-lock", scheduler])
            for name in (publish, scheduler):
                with cli.exclusive_lock(name) as acquired:
                    self.assertTrue(acquired)


if __name__ == "__main__":
    unittest.main()
