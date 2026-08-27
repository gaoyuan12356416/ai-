#!/usr/bin/env python3
"""Focused fake-only coverage of the new CREATE-only ads_ai bootstrap."""
from __future__ import annotations

import copy
import json
import stat
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(ROOT))

from features.drama_synthesis.unified_youtube import TABLE_BY_KIND
from features.drama_synthesis.unified_youtube_rpc import LedgerRPCError, SCHEMA, _video_record
from scripts import bootstrap_drama_youtube_ads_ai as bootstrap
from scripts import migrate_drama_youtube_unified_schema as retired_migration
from scripts import drama_youtube_three_table_rehearsal as retired_rehearsal
from scripts.test_drama_synthesis_upgrade import unified_video_payload
from scripts.test_drama_youtube_unified_rpc import FakeConnection

CANDIDATE = "a" * 40
CONTEXT = "2026082715040001"
DATA_DIR = "/mnt/data-disk/drama-youtube-ads-ai-rehearsal-" + CONTEXT + "/data"


def virtual_data_filesystem(*, overrides=None, resolved=None, mounted=True):
    overrides = overrides or {}
    resolved = resolved or {}

    class VirtualDirectoryPath(PurePosixPath):
        def lstat(self):
            key = str(self)
            value = overrides.get(key)
            if isinstance(value, Exception):
                raise value
            return SimpleNamespace(st_mode=stat.S_IFDIR | 0o700,
                                   st_dev=1 if key in {"/", "/mnt"} else 2) if value is None else value

        def stat(self):
            return self.lstat()

        def resolve(self, strict=False):
            return VirtualDirectoryPath(resolved.get(str(self), str(self)))

        def is_mount(self):
            return mounted and str(self) == "/mnt/data-disk"

    return VirtualDirectoryPath


def config(*, apply=False, rehearsal=False):
    return {"host": "127.0.0.1" if rehearsal else bootstrap.HOST,
            "port": bootstrap.REHEARSAL_PORT if rehearsal else (bootstrap.WRITER_PORT if apply else bootstrap.READER_PORT),
            "user": bootstrap.REHEARSAL_USER if rehearsal else bootstrap.ADMIN_USER,
            "password": "fixture-only-password", "database": SCHEMA}


def admin_connection(*, existing=False, rehearsal=False):
    connection = FakeConnection(existing=existing)
    user = bootstrap.REHEARSAL_USER if rehearsal else bootstrap.ADMIN_USER
    host = "%" if rehearsal else "43.166.187.96"
    connection.account = user + "@" + host
    account = "'%s'@'%s'" % (user, host)
    connection.show_grants = [{"grant": "GRANT USAGE ON *.* TO " + account},
                              {"grant": "GRANT ALL PRIVILEGES ON ads_ai.* TO " + account + ("" if rehearsal else " WITH GRANT OPTION")}]
    return connection


def evidence_fixtures():
    now = bootstrap._now()
    discovery = {
        "ok": True, "contract": bootstrap.INSPECTION_CONTRACT, "mode": "dry-run",
        "schema": SCHEMA, "host": bootstrap.HOST, "port": bootstrap.READER_PORT,
        "candidate_git_sha": CANDIDATE, "ddl_sha256": bootstrap.DDL_SHA256,
        "admin_identity": "ads_aius@43.166.187.96", "admin_trigger_check": True,
        "observed_at_utc": now, "table_states": {table: "missing" for table in TABLE_BY_KIND.values()},
        "created_tables": [],
    }
    rehearsal = {
        "ok": True, "contract": bootstrap.REHEARSAL_CONTRACT, "schema": SCHEMA,
        "host": "127.0.0.1", "port": bootstrap.REHEARSAL_PORT, "engine_version": "5.7.44",
        "context": CONTEXT, "candidate_git_sha": CANDIDATE, "ddl_sha256": bootstrap.DDL_SHA256,
        "observed_at_utc": now, "runtime_identity_simulated": True,
        "checks": {key: True for key in bootstrap.REHEARSAL_CHECKS},
    }
    evidence = {
        "contract": bootstrap.EVIDENCE_CONTRACT, "candidate_git_sha": CANDIDATE,
        "ddl_sha256": bootstrap.DDL_SHA256, "discovery_file": "/mnt/data-disk/proof/schema-discovery.json",
        "discovery_sha256": "d" * 64, "rehearsal_file": "/mnt/data-disk/proof/fresh-rehearsal.json",
        "rehearsal_sha256": "e" * 64,
    }
    return evidence, discovery, rehearsal


def fixture_loader(evidence, discovery, rehearsal):
    def load(path):
        if path.endswith("bootstrap-evidence.json"):
            return evidence, "f" * 64
        if path == evidence["discovery_file"]:
            return discovery, "d" * 64
        if path == evidence["rehearsal_file"]:
            return rehearsal, "e" * 64
        raise AssertionError("unexpected evidence path")
    return load


class AdsAiBootstrapTests(unittest.TestCase):
    def test_reviewed_sql_is_exact_three_create_only_statements(self):
        statements = bootstrap.load_reviewed_sql()
        self.assertEqual(set(statements), set(TABLE_BY_KIND.values()))
        for table, sql in statements.items():
            self.assertTrue(sql.startswith("CREATE TABLE ads_ai." + table + " ("))
            for denied in ("IF NOT EXISTS", "ALTER TABLE", "DROP TABLE", "DELETE ", "REPLACE ", "INSERT ", "kunlunads_dev", "FOREIGN KEY", "TRIGGER"):
                self.assertNotIn(denied, sql)
        with mock.patch.object(bootstrap, "DDL_SHA256", "0" * 64), self.assertRaises(RuntimeError):
            bootstrap.load_reviewed_sql()

    def test_dry_run_inspects_63350_and_never_writes(self):
        connection = admin_connection()
        with mock.patch.object(bootstrap, "load_admin_credential_file", return_value=config()) as load, mock.patch.object(bootstrap, "_connect", return_value=connection) as connect:
            result = bootstrap.bootstrap("fixture")
        self.assertEqual(result["port"], 63350)
        self.assertEqual(result["created_tables"], [])
        self.assertEqual(len(result["missing_tables"]), 3)
        self.assertTrue(result["admin_trigger_check"])
        load.assert_called_once_with("fixture", apply=False)
        connect.assert_called_once_with(config(), apply=False)
        self.assertTrue(all(sql.startswith(("SELECT", "SHOW")) for sql in connection.sql))
        self.assertEqual(connection.inserts, 0)

    def test_new_tables_create_once_and_compatible_rerun_preserves_records(self):
        connection = admin_connection()
        first = bootstrap._run_bootstrap(connection.cursor(), apply=True)
        self.assertEqual(len(first["created_tables"]), 3)
        connection.rows["ads_youtube_videos"].append(dict(_video_record(unified_video_payload()), id=1))
        before = copy.deepcopy(connection.rows)
        second = bootstrap._run_bootstrap(connection.cursor(), apply=True)
        self.assertEqual(second["created_tables"], [])
        self.assertEqual(connection.rows, before)
        self.assertEqual(len(connection.ddl), 3)
        self.assertTrue(second["complete"])
        self.assertFalse(any("FROM ads_ai." in sql for sql in connection.sql))

    def test_mixed_missing_and_compatible_creates_only_missing(self):
        connection = admin_connection()
        connection.existing.add("ads_youtube_videos")
        result = bootstrap._run_bootstrap(connection.cursor(), apply=True)
        self.assertEqual(set(result["created_tables"]), {"ads_youtube_comments", "ads_youtube_publish_log"})

    def test_any_incompatible_table_stops_before_all_ddl(self):
        for override in ({"TABLE_COMMENT": "old"}, {"TABLE_TYPE": "VIEW"}, {"ENGINE": "MyISAM"}):
            connection = admin_connection()
            connection.existing.add("ads_youtube_comments")
            connection.table_override = override
            with self.subTest(override=override), self.assertRaises(LedgerRPCError):
                bootstrap._run_bootstrap(connection.cursor(), apply=True)
            self.assertEqual(connection.ddl, [])
            self.assertEqual(connection.existing, {"ads_youtube_comments"})

    def test_trigger_foreign_key_and_extra_column_block_before_ddl(self):
        for field, value in (
            ("triggers", [{"TRIGGER_NAME": "writes_old_schema"}]),
            ("foreign_keys", [{"REFERENCED_TABLE_SCHEMA": "kunlunads_dev"}]),
            ("extra_column", True), ("missing_index", True),
        ):
            connection = admin_connection()
            connection.existing.add("ads_youtube_comments")
            setattr(connection, field, value)
            with self.subTest(field=field), self.assertRaises(LedgerRPCError):
                bootstrap._run_bootstrap(connection.cursor(), apply=True)
            self.assertEqual(connection.ddl, [])

    def test_partial_ddl_failure_preserves_created_and_existing_objects(self):
        connection = admin_connection()
        connection.fail_create = "ads_youtube_comments"
        with self.assertRaises(RuntimeError):
            bootstrap._run_bootstrap(connection.cursor(), apply=True)
        self.assertEqual(connection.existing, {"ads_youtube_videos"})
        self.assertFalse(any(sql.startswith(("DROP", "ALTER", "DELETE", "REPLACE")) for sql in connection.sql))

    def test_bootstrap_loaders_have_three_separate_fixed_targets(self):
        for apply, rehearsal in ((False, False), (True, False), (False, True)):
            valid = config(apply=apply, rehearsal=rehearsal)
            with mock.patch.object(bootstrap, "read_secure_owned_file", return_value=json.dumps(valid).encode()):
                self.assertEqual(bootstrap.load_admin_credential_file("fixture", apply=apply, rehearsal=rehearsal), valid)
            for change in ({"database": "kunlunads_dev"}, {"user": "drama_youtube_writer"}, {"port": "63353"}, {"unexpected": 1}):
                with self.subTest(apply=apply, rehearsal=rehearsal, change=change), mock.patch.object(bootstrap, "read_secure_owned_file", return_value=json.dumps(dict(valid, **change)).encode()), self.assertRaises(RuntimeError):
                    bootstrap.load_admin_credential_file("fixture", apply=apply, rehearsal=rehearsal)

    def test_admin_identity_readonly_and_trigger_visibility_are_required(self):
        for field, value in (("schema", "kunlunads_dev"), ("account", "ads_aius@%"), ("read_only", 1), ("read_only", None), ("show_grants", [])):
            connection = admin_connection()
            setattr(connection, field, value)
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                bootstrap._validate_admin(connection.cursor(), writable=True)
            self.assertEqual(connection.ddl, [])
        connection = admin_connection(rehearsal=True)
        self.assertEqual(bootstrap._validate_admin(connection.cursor(), writable=True, rehearsal=True), "drama_ads_ai_rehearsal@%")

    def test_connection_refuses_cross_environment_or_extra_driver_options(self):
        for invalid in (config(apply=True), config(rehearsal=True), dict(config(), database="kunlunads_dev"),
                        dict(config(), init_command="SELECT 1"), dict(config(), port="63350")):
            with self.subTest(invalid={key: value for key, value in invalid.items() if key != "password"}), mock.patch.object(bootstrap.pymysql, "connect") as connect, self.assertRaises(RuntimeError):
                bootstrap._connect(invalid)
            connect.assert_not_called()

    def test_production_apply_requires_explicit_new_evidence_and_clean_candidate(self):
        with mock.patch.object(bootstrap, "_connect") as connect:
            for kwargs in ({"apply": True}, {"apply": True, "candidate_git_sha": CANDIDATE}, {"evidence_file": "old.json"}):
                with self.subTest(kwargs=kwargs), self.assertRaises(RuntimeError):
                    bootstrap.bootstrap("fixture", **kwargs)
            connect.assert_not_called()
        connection = admin_connection()
        with mock.patch.object(bootstrap, "verify_candidate") as candidate, mock.patch.object(bootstrap, "load_apply_evidence", return_value={"evidence_sha256": "f" * 64}) as evidence, mock.patch.object(bootstrap, "load_admin_credential_file", return_value=config(apply=True)), mock.patch.object(bootstrap, "_connect", return_value=connection):
            result = bootstrap.bootstrap("fixture", apply=True, candidate_git_sha=CANDIDATE, evidence_file="new-evidence.json")
        candidate.assert_called_once_with(CANDIDATE)
        evidence.assert_called_once_with("new-evidence.json", candidate_git_sha=CANDIDATE)
        self.assertEqual(result["port"], 63353)
        self.assertEqual(len(result["created_tables"]), 3)

    def test_candidate_sha_and_clean_git_state_gate(self):
        with self.assertRaises(RuntimeError):
            bootstrap.verify_candidate("2c440c3")
        for head, status in (("b" * 40, ""), (CANDIDATE, "?? unexpected.py"), (CANDIDATE, " M app.py")):
            with self.subTest(head=head, status=status), mock.patch.object(bootstrap.subprocess, "run", side_effect=[SimpleNamespace(stdout=head), SimpleNamespace(stdout=status)]), self.assertRaises(RuntimeError):
                bootstrap.verify_candidate(CANDIDATE)
        with mock.patch.object(bootstrap.subprocess, "run", side_effect=[SimpleNamespace(stdout=CANDIDATE), SimpleNamespace(stdout="")]):
            bootstrap.verify_candidate(CANDIDATE)

    def test_evidence_accepts_only_fresh_candidate_bound_new_artifacts(self):
        evidence, discovery, rehearsal = evidence_fixtures()
        with mock.patch.object(bootstrap, "_load_private_json", side_effect=fixture_loader(evidence, discovery, rehearsal)):
            value = bootstrap.load_apply_evidence("/mnt/data-disk/proof/bootstrap-evidence.json", candidate_git_sha=CANDIDATE)
        self.assertEqual(value["rehearsal_sha256"], "e" * 64)
        for target, field, value in (
            ("evidence", "contract", "table_snapshot_rehearsal"), ("evidence", "ddl_sha256", "0" * 64),
            ("evidence", "candidate_git_sha", "c" * 40), ("evidence", "rehearsal_sha256", "0" * 64),
            ("discovery", "schema", "kunlunads_dev"), ("discovery", "port", 63353),
            ("discovery", "admin_trigger_check", False), ("discovery", "created_tables", ["x"]),
            ("rehearsal", "contract", "table_snapshot_rehearsal"), ("rehearsal", "port", 23357),
            ("rehearsal", "candidate_git_sha", "b" * 40), ("rehearsal", "checks", {}),
            ("rehearsal", "runtime_identity_simulated", False),
        ):
            e, d, r = evidence_fixtures()
            {"evidence": e, "discovery": d, "rehearsal": r}[target][field] = value
            with self.subTest(target=target, field=field), mock.patch.object(bootstrap, "_load_private_json", side_effect=fixture_loader(e, d, r)), self.assertRaises(RuntimeError):
                bootstrap.load_apply_evidence("/mnt/data-disk/proof/bootstrap-evidence.json", candidate_git_sha=CANDIDATE)

    def test_stale_rehearsal_cannot_be_rebound_as_current_proof(self):
        e, d, r = evidence_fixtures()
        r["observed_at_utc"] = (datetime.now(timezone.utc) - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        with mock.patch.object(bootstrap, "_load_private_json", side_effect=fixture_loader(e, d, r)), self.assertRaises(RuntimeError):
            bootstrap.load_apply_evidence("/mnt/data-disk/proof/bootstrap-evidence.json", candidate_git_sha=CANDIDATE)

    def test_evidence_outside_data_disk_is_refused(self):
        for path in ("relative.json", "/tmp/bootstrap-evidence.json", "C:/Temp/bootstrap-evidence.json"):
            with self.subTest(path=path), self.assertRaises(RuntimeError):
                bootstrap._private_data_path(path)

    def test_new_rehearsal_container_is_exact_and_loopback_only(self):
        name = "drama-youtube-ads-ai-rehearsal-" + CONTEXT
        valid = {"Name": "/" + name, "Id": "f" * 64, "State": {"Running": True}, "Image": bootstrap.REHEARSAL_IMAGE_ID,
                 "Config": {"Image": bootstrap.REHEARSAL_IMAGE_DIGEST, "Labels": {"drama_youtube_ads_ai_rehearsal": CONTEXT}},
                 "HostConfig": {"Privileged": False, "NetworkMode": "bridge"},
                 "NetworkSettings": {"Ports": {"3306/tcp": [{"HostIp": "127.0.0.1", "HostPort": "23358"}]}},
                 "Mounts": [{"Type": "bind", "Destination": "/var/lib/mysql", "Source": "/mnt/data-disk/" + name + "/data", "RW": True}]}
        for reference in (bootstrap.REHEARSAL_IMAGE_DIGEST, "mysql:5.7.44"):
            accepted = copy.deepcopy(valid)
            accepted["Config"]["Image"] = reference
            with self.subTest(reference=reference), mock.patch.object(bootstrap.subprocess, "run", return_value=SimpleNamespace(stdout=json.dumps([accepted]))), mock.patch.object(bootstrap, "_validate_rehearsal_data_dir") as validate_data:
                result = bootstrap._inspect_rehearsal_container(CONTEXT)
                validate_data.assert_called_once_with(DATA_DIR)
                self.assertEqual(result["container_name"], name)
                self.assertEqual(result["container_image_reference"], reference)
                self.assertEqual(result["container_image_id"], bootstrap.REHEARSAL_IMAGE_ID)
        for field, value in (("HostConfig", {"Privileged": True}), ("Config", {"Image": "mysql:8"}),
                             ("NetworkSettings", {"Ports": {"3306/tcp": [{"HostIp": "0.0.0.0", "HostPort": "23358"}]}}),
                             ("Mounts", []), ("Name", "/old-rehearsal"), ("Image", "sha256:" + "0" * 64), ("Image", None)):
            invalid = dict(valid, **{field: value})
            with self.subTest(field=field), mock.patch.object(bootstrap.subprocess, "run", return_value=SimpleNamespace(stdout=json.dumps([invalid]))), self.assertRaises(RuntimeError):
                bootstrap._inspect_rehearsal_container(CONTEXT)

        for reference, image_id in (
            ("mysql@sha256:" + "0" * 64, bootstrap.REHEARSAL_IMAGE_ID),
            (bootstrap.REHEARSAL_IMAGE_DIGEST, "sha256:" + "0" * 64),
            ("mysql:5.7.44", "sha256:" + "0" * 64),
        ):
            invalid = copy.deepcopy(valid)
            invalid["Config"]["Image"] = reference
            invalid["Image"] = image_id
            with self.subTest(reference=reference, image_id=image_id), mock.patch.object(bootstrap.subprocess, "run", return_value=SimpleNamespace(stdout=json.dumps([invalid]))), self.assertRaises(RuntimeError):
                bootstrap._inspect_rehearsal_container(CONTEXT)

        for mount_type in ("volume", "tmpfs", None):
            invalid = copy.deepcopy(valid)
            invalid["Mounts"][0]["Type"] = mount_type
            with self.subTest(mount_type=mount_type), mock.patch.object(bootstrap.subprocess, "run", return_value=SimpleNamespace(stdout=json.dumps([invalid]))), mock.patch.object(bootstrap, "_validate_rehearsal_data_dir") as validate_data, self.assertRaises(RuntimeError):
                bootstrap._inspect_rehearsal_container(CONTEXT)
            validate_data.assert_not_called()

        with mock.patch.object(bootstrap.subprocess, "run", return_value=SimpleNamespace(stdout=json.dumps([valid]))), mock.patch.object(bootstrap, "verify_candidate"), mock.patch.object(bootstrap, "_validate_rehearsal_data_dir", side_effect=RuntimeError("unsafe datadir")), mock.patch.object(bootstrap, "_connect") as connect, self.assertRaisesRegex(RuntimeError, "unsafe datadir"):
            bootstrap.rehearse(DATA_DIR.rsplit("/", 1)[0] + "/admin-db.json", candidate_git_sha=CANDIDATE, context=CONTEXT)
        connect.assert_not_called()

    def test_datadir_must_be_real_directory_on_verified_independent_mount(self):
        result = SimpleNamespace(stdout=json.dumps({"filesystems": [{"target": "/mnt/data-disk", "source": "/dev/fixture"}]}))
        with mock.patch.object(bootstrap, "Path", virtual_data_filesystem()), mock.patch.object(bootstrap.os, "name", "posix"), mock.patch.object(bootstrap.subprocess, "run", return_value=result) as run:
            bootstrap._validate_rehearsal_data_dir(DATA_DIR)
        self.assertEqual(run.call_args.args[0], ["findmnt", "--json", "--target", DATA_DIR, "--output", "TARGET,SOURCE"])

    def test_datadir_rejects_symlink_component_non_directory_and_other_device(self):
        for component in ("/mnt", "/mnt/data-disk", DATA_DIR.rsplit("/", 1)[0], DATA_DIR):
            metadata = SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_dev=2)
            with self.subTest(symlink=component), mock.patch.object(bootstrap, "Path", virtual_data_filesystem(overrides={component: metadata})), mock.patch.object(bootstrap.os, "name", "posix"), mock.patch.object(bootstrap.subprocess, "run") as run, self.assertRaises(RuntimeError):
                bootstrap._validate_rehearsal_data_dir(DATA_DIR)
            run.assert_not_called()
        for overrides in (
            {DATA_DIR: SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_dev=2)},
            {DATA_DIR: SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_dev=1)},
            {DATA_DIR.rsplit("/", 1)[0]: SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_dev=3)},
            {"/mnt/data-disk": SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_dev=1)},
            {DATA_DIR: FileNotFoundError("fixture")},
        ):
            with self.subTest(overrides=overrides), mock.patch.object(bootstrap, "Path", virtual_data_filesystem(overrides=overrides)), mock.patch.object(bootstrap.os, "name", "posix"), mock.patch.object(bootstrap.subprocess, "run") as run, self.assertRaises(RuntimeError):
                bootstrap._validate_rehearsal_data_dir(DATA_DIR)
            run.assert_not_called()
        for filesystem in (virtual_data_filesystem(resolved={DATA_DIR: "/old/data"}), virtual_data_filesystem(mounted=False)):
            with mock.patch.object(bootstrap, "Path", filesystem), mock.patch.object(bootstrap.os, "name", "posix"), mock.patch.object(bootstrap.subprocess, "run") as run, self.assertRaises(RuntimeError):
                bootstrap._validate_rehearsal_data_dir(DATA_DIR)
            run.assert_not_called()

    def test_same_device_nested_bind_mount_to_old_data_is_refused(self):
        for target in ("/", DATA_DIR, DATA_DIR.rsplit("/", 1)[0]):
            result = SimpleNamespace(stdout=json.dumps({"filesystems": [{"target": target, "source": "/dev/fixture[/old-data]"}]}))
            with self.subTest(target=target), mock.patch.object(bootstrap, "Path", virtual_data_filesystem()), mock.patch.object(bootstrap.os, "name", "posix"), mock.patch.object(bootstrap.subprocess, "run", return_value=result), self.assertRaises(RuntimeError):
                bootstrap._validate_rehearsal_data_dir(DATA_DIR)

    def test_fresh_rehearsal_uses_full_payload_and_preserves_nonempty_rerun(self):
        connection = admin_connection(rehearsal=True)
        path = "/mnt/data-disk/drama-youtube-ads-ai-rehearsal-" + CONTEXT + "/admin-db.json"
        with mock.patch.object(bootstrap, "verify_candidate"), mock.patch.object(bootstrap, "_inspect_rehearsal_container", return_value={}), mock.patch.object(bootstrap, "_private_data_path"), mock.patch.object(bootstrap, "load_admin_credential_file", return_value=config(rehearsal=True)), mock.patch.object(bootstrap, "_connect", return_value=connection):
            report = bootstrap.rehearse(path, candidate_git_sha=CANDIDATE, context=CONTEXT)
            before = copy.deepcopy(connection.rows)
            with self.assertRaises(RuntimeError):
                bootstrap.rehearse(path, candidate_git_sha=CANDIDATE, context=CONTEXT)
        self.assertTrue(all(report["checks"].values()))
        self.assertTrue(report["runtime_identity_simulated"])
        self.assertEqual(connection.rows, before)
        self.assertEqual(connection.inserts, 3)
        self.assertEqual(len(connection.ddl), 3)

    def test_legacy_mutation_and_proof_entrypoints_are_all_retired(self):
        for module, names in (
            (retired_migration, ("_connect", "_run_migration", "migrate", "load_backup_evidence_file", "verify_runtime_writer", "main")),
            (retired_rehearsal, ("_connect", "export_snapshot", "rehearse_loopback", "validate_table_snapshot_evidence", "main")),
        ):
            self.assertIs(module.RETIRED, True)
            for name in names:
                with self.subTest(module=module.__name__, name=name), self.assertRaisesRegex(RuntimeError, "retired"):
                    getattr(module, name)()


if __name__ == "__main__":
    unittest.main()
