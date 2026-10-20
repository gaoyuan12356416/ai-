"""Offline narrow-deployment QA: temporary files/SQLite, mocked Git and systemd."""
import ast
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location("cover_source_deploy", Path(__file__).with_name("deploy.py"))
deploy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deploy)
COMMIT = "a" * 40
OLD_YT = "b" * 40
OLD_REPORT = "c" * 40
ORIGINAL = '''# Production-only composite features must survive.
CONFIG = {"name": "线上配置"}
def helper(x):
    return x + 1

def post_screenshot_ai_source_callback(job):
    return {"old": job, "备注": "旧接口"}

def production_only_feature():
    return "keep this"
'''


def baseline_for(raw):
    text = raw.decode("utf-8")
    _tree, node = deploy.function_node(text)
    return {"app_sha256": deploy.digest(raw),
            "old_callback_sha256": deploy.digest(ast.get_source_segment(text, node).encode("utf-8")),
            "youtube_release": OLD_YT, "report_release": OLD_REPORT}


class CompositionTests(unittest.TestCase):
    def test_exact_bytes_and_rest_ast_preserved_even_if_release_has_other_changes(self):
        original = ORIGINAL.encode("utf-8")
        release = ("CONFIG = 'unrelated release change'\n" + deploy.BRIDGE + "\n").encode("utf-8")
        composed, proof = deploy.compose_app(original, release, baseline_for(original))
        self.assertEqual(deploy.without_callback(composed.decode()), deploy.without_callback(ORIGINAL))
        self.assertIn(b"production_only_feature", composed)
        self.assertNotIn(b"unrelated release change", composed)
        old_segment = ast.get_source_segment(ORIGINAL, deploy.function_node(ORIGINAL)[1])
        self.assertEqual(composed.decode(), ORIGINAL.replace(old_segment, deploy.BRIDGE))
        self.assertEqual(proof["installed_sha256"], deploy.digest(composed))

    def test_crlf_and_unicode_last_line_preserved(self):
        original = ORIGINAL.replace("\n", "\r\n").encode()
        composed, _proof = deploy.compose_app(original, deploy.BRIDGE.encode(), baseline_for(original))
        self.assertNotIn(b"\n", composed.replace(b"\r\n", b""))
        self.assertEqual(deploy.without_callback(composed.decode()), deploy.without_callback(ORIGINAL))

    def test_full_app_or_callback_hash_drift_rejected(self):
        original = ORIGINAL.encode()
        baseline = baseline_for(original)
        with self.assertRaisesRegex(RuntimeError, "production_app_sha_drift"):
            deploy.compose_app(original + b"\n", deploy.BRIDGE.encode(), baseline)
        baseline["old_callback_sha256"] = "0" * 64
        with self.assertRaisesRegex(RuntimeError, "production_callback_sha_drift"):
            deploy.compose_app(original, deploy.BRIDGE.encode(), baseline)

    def test_duplicate_decorated_and_nonbridge_release_rejected(self):
        for release in (deploy.BRIDGE + "\n" + deploy.BRIDGE,
                        "@danger\n" + deploy.BRIDGE,
                        deploy.BRIDGE.replace("return upload_screenshot(job)", "return old_api(job)")):
            with self.subTest(release=release):
                with self.assertRaises(RuntimeError):
                    deploy.compose_app(ORIGINAL.encode(), release.encode(), baseline_for(ORIGINAL.encode()))


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.app = self.base / "app/app.py"
        self.core = self.base / "app/features/cover_material_source.py"
        self.source_db = self.base / "app/data/source.sqlite3"
        self.root = self.base / "shared"
        self.sync_db = self.base / "youtube-sync/sync.sqlite3"
        self.unit_dir = self.base / "units"
        self.release = self.base / "releases" / COMMIT
        self.repo = self.base / "repo"
        self.yt = self.base / "youtube/current"
        self.report = self.base / "report/current"
        for path in (self.app.parent, self.source_db.parent, self.root,
                     self.unit_dir, self.release, self.repo):
            path.mkdir(parents=True, exist_ok=True)
        self.app.write_bytes(ORIGINAL.encode())
        with contextlib.closing(sqlite3.connect(self.source_db)) as conn:
            conn.execute("CREATE TABLE source(id INTEGER, status TEXT)")
            conn.execute("INSERT INTO source VALUES(1,'published')")
            conn.commit()
        self.baseline = baseline_for(self.app.read_bytes())
        self.links = {
            "youtube": {"path": str(self.yt), "link": str(self.base / OLD_YT), "target": str(self.base / OLD_YT)},
            "report": {"path": str(self.report), "link": str(self.base / OLD_REPORT), "target": str(self.base / OLD_REPORT)},
        }
        self.states = {name: "active" for name in (deploy.APP_UNIT, *deploy.UNITS)}
        self.cron = b"0 10 * * * /bin/true\n"
        for name in self.states:
            (self.unit_dir / name).write_text("# previous " + name, encoding="utf-8")
        replacements = {"APP": self.app, "CORE": self.core, "SOURCE_DB": self.source_db,
                        "ROOT": self.root, "SYNC_DB": self.sync_db, "UNITS_DIR": self.unit_dir,
                        "YOUTUBE_CURRENT": self.yt, "REPORT_CURRENT": self.report}
        for key, value in replacements.items():
            patch = mock.patch.object(deploy, key, value)
            patch.start()
            self.addCleanup(patch.stop)
        contents = {
            "app.py": ("RELEASE_ONLY = True\n" + deploy.BRIDGE + "\n").encode(),
            "features/cover_material_source.py": b"def upload_screenshot(job):\n    return job\n",
            deploy.MODULE + "/baseline.json": deploy.encoded(self.baseline),
            deploy.MODULE + "/deploy.py": b"# verified deployment script\n",
            "ops/youtube-cover-material-sync/sync.py": b"# verified sync\n",
            "ops/ai-image-daily/report.py": b"# verified report\n",
            "ops/ai-image-daily/collector.py": b"# verified collector\n",
        }
        for name in deploy.UNITS:
            content = "[Unit]\nDescription=" + name + "\n[Service]\n"
            if name == deploy.UNITS[0]:
                content += "ReadWritePaths=" + str(self.root) + "\nReadOnlyPaths=" + str(self.source_db) + "\n"
            if name == deploy.UNITS[2]:
                content += "ReadOnlyPaths=-" + str(self.root / "upload.sqlite3") + "\n"
            contents[deploy.unit_relative(name)] = content.encode()
        for relative, raw in contents.items():
            target = self.release / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        self.hashes = {relative: deploy.digest(raw) for relative, raw in contents.items()}
        patches = [
            mock.patch.object(deploy, "environment_ok"),
            mock.patch.object(deploy, "validate_release", return_value=(self.release, self.baseline, self.hashes)),
            mock.patch.object(deploy, "current_links", side_effect=lambda _baseline: json.loads(json.dumps(self.links))),
            mock.patch.object(deploy, "output", side_effect=self.fake_output),
            mock.patch.object(deploy, "call"),
            mock.patch.object(deploy.subprocess, "run", side_effect=self.fake_run),
            mock.patch.object(deploy, "switch"),
        ]
        self.patches = []
        for patch in patches:
            self.patches.append(patch.start())
            self.addCleanup(patch.stop)

    def fake_output(self, *args):
        if args[:2] == ("systemctl", "cat"):
            return (self.unit_dir / args[2]).read_bytes()
        if args[:2] == ("systemctl", "show"):
            return self.states[args[2]].encode()
        raise AssertionError("unexpected external output call")

    def fake_run(self, args, **_kwargs):
        if args[:2] == ["systemctl", "is-enabled"]:
            return subprocess.CompletedProcess(args, 0, stdout="enabled\n", stderr="")
        if args == ["crontab", "-l"]:
            return subprocess.CompletedProcess(args, 0, stdout=self.cron, stderr=b"")
        raise AssertionError("unexpected external subprocess")

    def prepare(self):
        return deploy.prepare(self.repo, self.release, COMMIT)

    def evidence(self, **changes):
        body = {"cutoff": "2026-10-20T10:00:00+08:00",
                "old_outbound_blocked": True, "old_http_drained": True,
                "new_outbound_held": True, "old_http_inflight": 0,
                "evidence": "offline test operator evidence"}
        body.update(changes)
        path = self.base / "cutover.json"
        path.write_bytes(deploy.encoded(body))
        return path, deploy.digest(path.read_bytes())

    def ready(self):
        self.states.update({name: "inactive" for name in self.states})
        (self.root / "HOLD").write_text("test hold", encoding="utf-8")

    def create_ledger(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.closing(sqlite3.connect(path)) as conn:
            conn.execute("CREATE TABLE ledger(id INTEGER, state TEXT)")
            conn.execute("INSERT INTO ledger VALUES(1,'confirmed')")
            conn.commit()

    def test_prepare_online_backs_up_files_units_cron_and_sqlite(self):
        before = self.app.read_bytes()
        backup = self.prepare()
        meta = json.loads((backup / "deployment.json").read_text())
        self.assertEqual(meta["phase"], "prepared")
        self.assertFalse(meta["database_restore_allowed"])
        self.assertEqual(meta["units"][deploy.APP_UNIT]["active_state"], "active")
        self.assertEqual(self.app.read_bytes(), before)
        self.assertFalse(self.core.exists())
        self.assertEqual((backup / "files/app.py").read_bytes(), before)
        self.assertEqual((backup / "root.crontab").read_bytes(), b"0 10 * * * /bin/true\n")
        with contextlib.closing(sqlite3.connect(backup / "source.sqlite3")) as conn:
            self.assertEqual(conn.execute("SELECT * FROM source").fetchall(), [(1, "published")])
        self.assertEqual(meta["links"], self.links)
        deploy.call.assert_not_called()

    def test_existing_sync_and_shared_ledgers_are_online_backed_up_with_sha(self):
        self.create_ledger(self.sync_db)
        self.create_ledger(self.root / "upload.sqlite3")
        backup = self.prepare()
        meta = json.loads((backup / "deployment.json").read_text())
        for key, source in (("youtube_sync", self.sync_db), ("shared_upload", self.root / "upload.sqlite3")):
            with self.subTest(key=key):
                entry = meta["ledger_backups"][key]
                self.assertTrue(entry["exists"])
                self.assertEqual(entry["source"], str(source))
                self.assertFalse(entry["restore_allowed"])
                self.assertEqual(entry["sha256"], deploy.digest(Path(entry["path"]).read_bytes()))
                with contextlib.closing(sqlite3.connect(entry["path"])) as conn:
                    self.assertEqual(conn.execute("SELECT * FROM ledger").fetchall(), [(1, "confirmed")])
        self.assertFalse(meta["source_database_backup"]["restore_allowed"])

    def test_absent_optional_ledgers_are_explicit_without_fake_backups(self):
        backup = self.prepare()
        meta = json.loads((backup / "deployment.json").read_text())
        for entry in meta["ledger_backups"].values():
            self.assertFalse(entry["exists"])
            self.assertIsNone(entry["sha256"])
            self.assertFalse(Path(entry["path"]).exists())

    def test_existing_ledger_backup_failure_cannot_finish_prepare(self):
        self.create_ledger(self.sync_db)
        self.create_ledger(self.root / "upload.sqlite3")
        real_backup = deploy.backup_sqlite
        for failed_path in (self.sync_db, self.root / "upload.sqlite3"):
            with self.subTest(failed_path=failed_path):
                def fail_existing(source, target):
                    if Path(source) == failed_path:
                        raise sqlite3.OperationalError("simulated ledger backup failure")
                    return real_backup(source, target)
                with mock.patch.object(deploy, "backup_sqlite", side_effect=fail_existing):
                    with self.assertRaisesRegex(sqlite3.OperationalError, "ledger backup failure"):
                        self.prepare()
        for path in (self.root / "deployments").glob("*/deployment.json"):
            self.assertEqual(json.loads(path.read_text())["phase"], "preparing")
        deploy.call.assert_not_called()
        self.assertEqual(self.app.read_bytes(), ORIGINAL.encode())

    def test_apply_only_bridge_and_module_changes_and_never_starts_services(self):
        backup = self.prepare()
        source_before = self.source_db.read_bytes()
        self.ready()
        meta = deploy.apply(backup, *self.evidence())
        self.assertEqual(meta["phase"], "installed_held")
        self.assertEqual(meta["cutoff"], "2026-10-20T10:00:00+08:00")
        self.assertFalse(meta["restarts_performed"])
        self.assertFalse(meta["hold_removed"])
        self.assertEqual(deploy.without_callback(self.app.read_text(encoding="utf-8")), deploy.without_callback(ORIGINAL))
        self.assertEqual(self.core.read_bytes(), (self.release / "features/cover_material_source.py").read_bytes())
        self.assertEqual(self.source_db.read_bytes(), source_before)
        self.assertTrue((self.root / "HOLD").exists())
        self.assertEqual(deploy.switch.call_count, 2)
        self.assertIn(mock.call("systemctl", "daemon-reload"), deploy.call.call_args_list)
        self.assertFalse(any("start" in call.args or "restart" in call.args for call in deploy.call.call_args_list))

    def test_apply_requires_stopped_app_and_timers(self):
        backup = self.prepare()
        self.ready()
        self.states[deploy.APP_UNIT] = "active"
        with self.assertRaisesRegex(RuntimeError, "unit_not_drained"):
            deploy.apply(backup, *self.evidence())
        self.assertEqual(self.app.read_bytes(), ORIGINAL.encode())
        self.assertFalse(self.core.exists())

    def test_apply_requires_shared_hold(self):
        backup = self.prepare()
        self.ready()
        (self.root / "HOLD").unlink()
        with self.assertRaisesRegex(RuntimeError, "shared_outbound_hold_required"):
            deploy.apply(backup, *self.evidence())
        self.assertFalse(self.core.exists())

    def test_prepare_snapshot_drift_blocks_before_target_changes(self):
        backup = self.prepare()
        self.ready()
        self.app.write_bytes(ORIGINAL.encode() + b"# external production edit\n")
        with self.assertRaisesRegex(RuntimeError, "prepared_file_drift"):
            deploy.apply(backup, *self.evidence())
        self.assertFalse(self.core.exists())
        deploy.switch.assert_not_called()

    def test_unit_drift_and_previous_release_drift_block(self):
        backup = self.prepare()
        self.ready()
        (self.unit_dir / deploy.UNITS[0]).write_text("# external unit edit", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "prepared_file_drift"):
            deploy.apply(backup, *self.evidence())
        (self.unit_dir / deploy.UNITS[0]).write_text("# previous " + deploy.UNITS[0], encoding="utf-8")
        self.links["youtube"]["target"] = "changed"
        with self.assertRaisesRegex(RuntimeError, "prepared_symlink_drift"):
            deploy.apply(backup, *self.evidence())

    def test_crontab_drift_blocks_apply_before_install(self):
        backup = self.prepare()
        self.ready()
        self.cron = b"0 11 * * * /bin/true\n"
        with self.assertRaisesRegex(RuntimeError, "prepared_crontab_drift"):
            deploy.apply(backup, *self.evidence())
        self.assertEqual(self.app.read_bytes(), ORIGINAL.encode())
        self.assertFalse(self.core.exists())
        deploy.switch.assert_not_called()

    def test_corrupt_composition_and_release_hashes_block(self):
        backup = self.prepare()
        self.ready()
        (backup / "composed-app.py").write_bytes(b"# corrupt")
        with self.assertRaisesRegex(RuntimeError, "composition_proof_drift"):
            deploy.apply(backup, *self.evidence())
        self.assertFalse(self.core.exists())
        self.hashes["app.py"] = "0" * 64
        with self.assertRaisesRegex(RuntimeError, "prepared_release_drift"):
            deploy.apply(backup, *self.evidence())

    def test_cutover_needs_exact_hash_timezone_and_true_attestations(self):
        path, sha = self.evidence()
        with self.assertRaisesRegex(RuntimeError, "evidence_hash_mismatch"):
            deploy.validate_cutover(path, "0" * 64)
        for change, pattern in (({"cutoff": "2026-10-20T10:00:00"}, "timezone"),
                                ({"old_http_drained": "true"}, "attestation"),
                                ({"old_http_inflight": True}, "not_drained"),
                                ({"old_http_inflight": 1}, "not_drained"),
                                ({"evidence": ""}, "operator_evidence")):
            with self.subTest(change=change):
                with self.assertRaisesRegex(RuntimeError, pattern):
                    deploy.validate_cutover(*self.evidence(**change))

    def test_partial_apply_never_restores_old_code_or_database(self):
        backup = self.prepare()
        self.ready()
        real_write = deploy.atomic_write
        def fail_app(path, *args, **kwargs):
            if Path(path) == self.app:
                raise OSError("simulated app install failure")
            return real_write(path, *args, **kwargs)
        with mock.patch.object(deploy, "atomic_write", side_effect=fail_app):
            with self.assertRaisesRegex(OSError, "simulated"):
                deploy.apply(backup, *self.evidence())
        self.assertEqual(self.app.read_bytes(), ORIGINAL.encode())
        self.assertTrue(self.core.exists())
        self.assertEqual(json.loads((backup / "deployment.json").read_text())["phase"], "applying")
        self.assertTrue((self.root / "HOLD").exists())
        deploy.switch.assert_not_called()

    def test_safe_rollback_preserves_bridge_new_rows_and_release_links(self):
        self.create_ledger(self.sync_db)
        self.create_ledger(self.root / "upload.sqlite3")
        backup = self.prepare()
        self.ready()
        deploy.apply(backup, *self.evidence())
        with contextlib.closing(sqlite3.connect(self.source_db)) as conn:
            conn.execute("INSERT INTO source VALUES(2,'new-published')")
            conn.commit()
        app_before, db_before, core_before = self.app.read_bytes(), self.source_db.read_bytes(), self.core.read_bytes()
        ledger_before = {}
        for path in (self.sync_db, self.root / "upload.sqlite3"):
            with contextlib.closing(sqlite3.connect(path)) as conn:
                conn.execute("INSERT INTO ledger VALUES(2,'unknown')")
                conn.commit()
            ledger_before[path] = path.read_bytes()
        switches = deploy.switch.call_count
        with mock.patch.object(deploy, "place_hold",
                               side_effect=lambda: (self.root / "HOLD").write_text("rollback hold")):
            result = deploy.rollback(backup)
        self.assertEqual(result["phase"], "rollback_held")
        self.assertEqual(result["rollback_policy"], "keep_bridge_and_all_databases")
        self.assertEqual((self.app.read_bytes(), self.source_db.read_bytes(), self.core.read_bytes()),
                         (app_before, db_before, core_before))
        self.assertEqual(deploy.switch.call_count, switches)
        self.assertTrue(all(path.read_bytes() == raw for path, raw in ledger_before.items()))
        deploy.call.assert_any_call("systemctl", "disable", "--now", "youtube-cover-material-sync.timer")
        deploy.call.assert_any_call("systemctl", "stop", "youtube-cover-material-sync.service")

    def test_rollback_places_hold_before_reporting_bridge_drift(self):
        backup = self.prepare()
        self.ready()
        deploy.apply(backup, *self.evidence())
        self.app.write_bytes(ORIGINAL.encode())
        (self.root / "HOLD").unlink()
        with mock.patch.object(deploy, "place_hold",
                               side_effect=lambda: (self.root / "HOLD").write_text("rollback hold")):
            with self.assertRaisesRegex(RuntimeError, "held_but_bridge_not_installed"):
                deploy.rollback(backup)
        self.assertTrue((self.root / "HOLD").exists())
        deploy.call.assert_any_call("systemctl", "stop", "youtube-cover-material-sync.service")

    def test_online_backup_includes_committed_wal_and_does_not_restore_source(self):
        target = self.base / "wal-backup.sqlite3"
        with contextlib.closing(sqlite3.connect(self.source_db)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("INSERT INTO source VALUES(2,'committed-in-wal')")
            conn.commit()
            backup_sha = deploy.backup_sqlite(self.source_db, target)
            conn.execute("INSERT INTO source VALUES(3,'after-backup')")
            conn.commit()
        self.assertEqual(deploy.digest(target.read_bytes()), backup_sha)
        with contextlib.closing(sqlite3.connect(target)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM source").fetchone()[0], 2)
        with contextlib.closing(sqlite3.connect(self.source_db)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM source").fetchone()[0], 3)

    def test_unit_permissions_require_shared_ledger_and_source_readonly(self):
        deploy.verify_unit_permissions(self.release)
        target = self.release / deploy.unit_relative(deploy.UNITS[0])
        target.write_text("[Service]\nReadWritePaths=/\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "ledger_permissions_missing"):
            deploy.verify_unit_permissions(self.release)

    def test_backups_outside_shared_root_are_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "outside_shared_root"):
            deploy.load_deployment(self.base)


    def test_git_verification_requires_origin_and_exact_blob_bytes(self):
        real_validate = SPEC.loader.get_code("cover_source_deploy")
        # Recover the real function without disturbing the integration test patch.
        namespace = {"__name__": "offline_deploy_copy", "__file__": str(Path(__file__).with_name("deploy.py"))}
        exec(real_validate, namespace)
        namespace.update(ROOT=self.root, SOURCE_DB=self.source_db)
        blobs, records = {}, []
        for index, relative in enumerate(self.hashes):
            blob = format(index + 1, "040x")
            blobs[blob] = (self.release / relative).read_bytes()
            records.append(("100644 blob " + blob + "\t" + relative).encode() + b"\0")
        def git(*args):
            if "rev-parse" in args:
                return COMMIT.encode()
            if "for-each-ref" in args:
                return b"refs/remotes/origin/release\n"
            if "ls-tree" in args:
                return b"".join(records)
            if "cat-file" in args:
                return blobs[args[-1]]
            raise AssertionError("unmocked git command")
        namespace["output"] = git
        release, baseline, hashes = namespace["validate_release"](self.repo, self.release, COMMIT)
        self.assertEqual((release, baseline, hashes), (self.release, self.baseline, self.hashes))
        (self.release / "features/cover_material_source.py").write_bytes(b"# drift")
        with self.assertRaisesRegex(RuntimeError, "differs_from_git"):
            namespace["validate_release"](self.repo, self.release, COMMIT)
        namespace["output"] = lambda *args: b"" if "for-each-ref" in args else git(*args)
        with self.assertRaisesRegex(RuntimeError, "not_in_fetched_origin"):
            namespace["validate_release"](self.repo, self.release, COMMIT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
