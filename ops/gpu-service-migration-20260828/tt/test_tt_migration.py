import importlib.util
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("tt_migration", HERE / "tt_migration.py")
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)
import cpu_state


class MigrationTests(unittest.TestCase):
    def test_config_closes_both_lanes_and_preserves_pull_contract(self):
        source = {
            "TT_POST_LIVE_ENABLED": "1", "TT_POST_MANUAL_CANARY_ENABLED": "1",
            "TT_POST_GPU_COS_DOMAIN": "https://socialkit-cdn.yingliang.tech",
            "TT_POST_URL_PROPERTY_VERIFIED_ORIGIN": "https://socialkit-cdn.yingliang.tech",
            "TT_POST_GPU_RANDOM_OVERLAY_MANIFEST_SHA256": "a" * 64,
            "TT_POST_DEFAULT_SOURCE_TRIM_TAIL_SECONDS": "0",
        }
        base, direct = migration.closed_environment(
            source, {"TT_POST_LIVE_ENABLED": "1", "TT_POST_DEFAULT_SOURCE_TRIM_TAIL_SECONDS": "4.333333"}
        )
        for env in (base, {**base, **direct}):
            self.assertEqual(env["TT_POST_LIVE_ENABLED"], "0")
            self.assertEqual(env["TT_POST_MANUAL_CANARY_ENABLED"], "0")
            self.assertEqual(env["TT_POST_GPU_STORAGE_BACKEND"], "cos")
            self.assertEqual(env["TT_POST_GPU_COS_DOMAIN"], source["TT_POST_GPU_COS_DOMAIN"])
        self.assertEqual(base["TT_POST_DEFAULT_SOURCE_TRIM_TAIL_SECONDS"], "0")
        self.assertEqual(direct["TT_POST_DEFAULT_SOURCE_TRIM_TAIL_SECONDS"], "4.333333")
        self.assertNotEqual(base["TT_POST_GPU_WORK_ROOT"], direct["TT_POST_GPU_WORK_ROOT"])
        self.assertEqual(source["TT_POST_LIVE_ENABLED"], "1")

    def test_environment_round_trip_does_not_execute_shell(self):
        values = {"VALUE": "space $HOME $(literal)", "EMPTY": ""}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "env"
            path.write_bytes(migration.env_bytes(values))
            self.assertEqual(migration.read_env(path), values)

    def test_secrets_cannot_reopen_gates_or_redirect_disk_paths(self):
        base, direct = migration.closed_environment({}, {})
        for overrides in ({"TT_POST_LIVE_ENABLED": "1"}, {"TMPDIR": "/tmp"}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                migration.validate_closed_environment(base, direct, overrides)
        migration.validate_closed_environment(base, direct, {"TT_POST_GPU_CREDENTIAL_SEAL_KEY": "synthetic"})
        with self.assertRaises(ValueError):
            migration.env_bytes({"VALUE": "one\nTT_POST_LIVE_ENABLED=1"})

    def test_snapshot_preserves_failed_ledgers_and_detects_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for base in [root, root / "direct-outro-work"]:
                (base / "manifests").mkdir(parents=True)
                (base / "publishes").mkdir()
            for job, state in [("first-job-0001", "published"), ("second-job-0002", "init_rejected"),
                               ("third-job-0003", "init_outcome_unknown")]:
                (root / "publishes" / (job + ".json")).write_text(
                    json.dumps({"job_id": job, "state": state})
                )
            before = migration.snapshot(root)
            self.assertEqual(before["file_count"], 3)
            self.assertEqual([x["state"] for x in before["risk"]], ["init_outcome_unknown"])
            path = root / "publishes/first-job-0001.json"
            path.write_text(json.dumps({"job_id": "first-job-0001", "state": "published", "publish_id": "new"}))
            self.assertNotEqual(before["fingerprint"], migration.snapshot(root)["fingerprint"])

    def test_snapshot_rejects_mismatched_job_and_missing_lane(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(ValueError):
                migration.snapshot(root)
            for base in [root, root / "direct-outro-work"]:
                (base / "manifests").mkdir(parents=True)
                (base / "publishes").mkdir()
            (root / "publishes/job-0000001.json").write_text(json.dumps({"job_id": "different", "state": "published"}))
            with self.assertRaises(ValueError):
                migration.snapshot(root)

    def test_run_id_cannot_escape_backup_scope(self):
        self.assertEqual(migration.run_backup_root("gpu-service-migration-20260828T1502").name, "tt")
        with self.assertRaises(ValueError):
            migration.run_backup_root("../../outside")


class CPUStateTests(unittest.TestCase):
    @staticmethod
    def create_tasks(connection):
        connection.execute("CREATE TABLE tt_auto_task (id INTEGER PRIMARY KEY,status TEXT,"
                           "claim_token TEXT DEFAULT '',lease_expires_at_utc TEXT DEFAULT '',"
                           "unknown_outcome INTEGER DEFAULT 0,publish_id TEXT DEFAULT '',"
                           "gpu_job_id TEXT DEFAULT '',scheduled_at_utc TEXT DEFAULT '')")
        connection.execute("INSERT INTO tt_auto_task(id,status,scheduled_at_utc) VALUES(1,'ready','2099-01-01T00:00:00Z')")
        connection.commit()

    def test_future_ready_allowed_but_expired_claim_and_unknown_still_block(self):
        with closing(sqlite3.connect(":memory:")) as connection:
            self.create_tasks(connection)
            before = cpu_state.table_snapshot(connection, "tt_auto_task")
            self.assertFalse(before["blocked"])
            self.assertEqual(before["future_unclaimed_ready"], 1)
            connection.execute("UPDATE tt_auto_task SET claim_token='never-return-this',lease_expires_at_utc='2020-01-01T00:00:00Z'")
            claimed = cpu_state.table_snapshot(connection, "tt_auto_task")
            self.assertTrue(claimed["blocked"])
            self.assertEqual(claimed["claims_expired"], 1)
            self.assertNotIn("never-return-this", json.dumps(claimed))
            connection.execute("UPDATE tt_auto_task SET claim_token='',lease_expires_at_utc='',unknown_outcome=1")
            unknown = cpu_state.table_snapshot(connection, "tt_auto_task")
            self.assertTrue(unknown["blocked"])
            self.assertNotEqual(before["publication_facts_sha256"], unknown["publication_facts_sha256"])

    def test_sqlite_backup_includes_committed_wal_without_uncommitted_work(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = root / "live.sqlite3", root / "snapshot.sqlite3"
            writer = sqlite3.connect(str(source))
            try:
                writer.execute("PRAGMA journal_mode=WAL")
                self.create_tasks(writer)
                writer.execute("UPDATE tt_auto_task SET status='published',publish_id='committed'")
                writer.commit()
                writer.execute("UPDATE tt_auto_task SET publish_id='uncommitted'")
                evidence = cpu_state.sqlite_backup(source, target)
                with closing(sqlite3.connect(str(target))) as copied:
                    self.assertEqual(copied.execute("SELECT publish_id FROM tt_auto_task").fetchone()[0], "committed")
                self.assertEqual(evidence["quick_check"], "ok")
                self.assertEqual(evidence["sha256"], migration.digest(target))
                with self.assertRaises(ValueError):
                    cpu_state.sqlite_backup(source, target)
            finally:
                writer.rollback()
                writer.close()

    def test_http_pairs_deduplicated_without_reading_request_contents(self):
        output = "0 0 127.0.0.1:18830 127.0.0.1:54321\n0 0 127.0.0.1:54321 127.0.0.1:18830\n0 0 127.0.0.1:22 10.0.0.1:55555\n"
        self.assertEqual(cpu_state.connection_snapshot(output), [["127.0.0.1:18830", "127.0.0.1:54321"]])


if __name__ == "__main__":
    unittest.main(verbosity=2)
