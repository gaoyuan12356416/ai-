import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import pathlib
import tempfile
import types
import unittest
from unittest import mock


CONTROL = pathlib.Path(__file__).parent


def load(name):
    spec = importlib.util.spec_from_file_location(name, str(CONTROL / (name + ".py")))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


common = load("drama_operator_common")
with mock.patch.dict("sys.modules", {"drama_operator_common": common}):
    quarantine = load("drama_quarantine")


class DramaQuarantineTests(unittest.TestCase):
    def exact_args(self, root, apply=False):
        ledger = pathlib.Path(root) / "drama-ledger-before-20260901T050000Z.json"
        return argparse.Namespace(
            run_id=common.RUN_ID,
            stamp="20260901T050100Z",
            expected_host=common.HK_HOST,
            data_root=str(common.HK_DATA_ROOT),
            expected_data_device="/dev/test",
            expected_current_sha=common.NEW_SHA,
            preflight=str(quarantine.PREFLIGHT_PATH),
            preflight_sha256=quarantine.PREFLIGHT_SHA256,
            ledger_evidence=str(ledger),
            ledger_evidence_sha256="a" * 64,
            ffprobe=str(quarantine.FFPROBE_DEFAULT),
            expected_ffprobe_sha256="b" * 64 if apply else "",
            unit=list(common.HK_TARGET_UNITS),
            protected_unit=list(common.HK_PROTECTED_UNITS),
            fragment=[], apply=apply,
        )

    def ledger_document(self, now):
        return {
            "schema": 1,
            "kind": "drama_quarantine_ledger_before",
            "run_id": common.RUN_ID,
            "host": common.CPU_HOST,
            "captured_at_epoch": now,
            "production_mutations": 0,
            "database": {
                "path": str(quarantine.CPU_DB_PATH),
                "realpath": str(quarantine.CPU_DB_PATH),
                "device": 1, "inode": 2, "size": 3, "mtime_ns": 4,
            },
            "snapshot": {
                "method": "sqlite_online_backup",
                "path": "/mnt/data-disk/migrations/drama-ledger.sqlite3",
                "sha256": "c" * 64,
            },
            "sqlite": {
                "quick_check": "ok", "foreign_key_violations": 0,
                "active_jobs": 0, "active_leases": 0,
                "approved_job_statuses": [
                    {"job_id": job_id, "status": "failed"}
                    for job_id in quarantine.JOB_IDS
                ],
            },
        }

    def test_exact_six_source_and_destination_contract(self):
        quarantine.validate_approved_contract()
        expected = (
            {
                "index": 1,
                "path": "/data/drama-synthesis-gpu/work/jobs/679e7c49acbf4af79f78bf60d76c5dd7/segments/000_intro.mp4",
                "job_id": "679e7c49acbf4af79f78bf60d76c5dd7",
                "destination_relative": "679e7c49acbf4af79f78bf60d76c5dd7/work/segments/000_intro.mp4",
                "inode": 1709295, "size": 197134,
                "sha256": "6ae022b06b8c581ebf3190d08189d597771ed5e2f7c0e2008650dcaef6e0137d",
            },
            {
                "index": 2,
                "path": "/data/drama-synthesis-gpu/work/jobs/679e7c49acbf4af79f78bf60d76c5dd7/8HehaA3263_679e7c49_eps_1_70.mp4",
                "job_id": "679e7c49acbf4af79f78bf60d76c5dd7",
                "destination_relative": "679e7c49acbf4af79f78bf60d76c5dd7/work/8HehaA3263_679e7c49_eps_1_70.mp4",
                "inode": 1709367, "size": 5139047136,
                "sha256": "5ba715a816999afef724215e7124ddf84638fae95ac74d1387a646df3b8162e0",
            },
            {
                "index": 3,
                "path": "/data/drama-synthesis-gpu/work/jobs/b6e0bc51bb3f44e19c12b20cef7b93fe/segments/000_intro.mp4",
                "job_id": "b6e0bc51bb3f44e19c12b20cef7b93fe",
                "destination_relative": "b6e0bc51bb3f44e19c12b20cef7b93fe/work/segments/000_intro.mp4",
                "inode": 4065349, "size": 291805,
                "sha256": "0c1475d611a92ced8f3be8d2cfb99f26b1698aed722b6568a1867f9511265864",
            },
            {
                "index": 4,
                "path": "/data/drama-synthesis-gpu/work/jobs/b6e0bc51bb3f44e19c12b20cef7b93fe/6oTsxN8BO6_b6e0bc51_eps_1_60.mp4",
                "job_id": "b6e0bc51bb3f44e19c12b20cef7b93fe",
                "destination_relative": "b6e0bc51bb3f44e19c12b20cef7b93fe/work/6oTsxN8BO6_b6e0bc51_eps_1_60.mp4",
                "inode": 4065412, "size": 4511337915,
                "sha256": "0366898789d8da0a9e0db54c5172b989b68d9038308f930c2baa56d9dd82f6a4",
            },
            {
                "index": 5,
                "path": "/data/drama-synthesis-gpu/work/jobs/b6e0bc51bb3f44e19c12b20cef7b93fe/material_no_bgm.mp4",
                "job_id": "b6e0bc51bb3f44e19c12b20cef7b93fe",
                "destination_relative": "b6e0bc51bb3f44e19c12b20cef7b93fe/work/material_no_bgm.mp4",
                "inode": 4065413, "size": 4550359900,
                "sha256": "fe0bfe122eb11fbf4e28512d18cec368c1e84c7a357e3e3150292fe1f7c9c3ac",
            },
            {
                "index": 6,
                "path": "/data/drama-synthesis-gpu/results/public/b6e0bc51bb3f44e19c12b20cef7b93fe/material_no_bgm.mp4",
                "job_id": "b6e0bc51bb3f44e19c12b20cef7b93fe",
                "destination_relative": "b6e0bc51bb3f44e19c12b20cef7b93fe/public/material_no_bgm.mp4",
                "inode": 4065414, "size": 4550359900,
                "sha256": "fe0bfe122eb11fbf4e28512d18cec368c1e84c7a357e3e3150292fe1f7c9c3ac",
            },
        )
        self.assertEqual(quarantine.APPROVED, expected)
        self.assertEqual(
            common.sha256_bytes(common.canonical_bytes(expected)),
            "db8257608535f54908608e8acc77509d90527e4aa2120f8d7e8999c1964c00ce")
        root = pathlib.Path("/data/migrations") / common.RUN_ID / \
            "drama-legacy-artifacts-20260901T050100Z"
        destinations = [quarantine.destination_path(root, item)
                        for item in quarantine.APPROVED]
        self.assertEqual(len(set(destinations)), 6)
        for item, destination in zip(quarantine.APPROVED, destinations):
            self.assertEqual(destination.parts[len(root.parts)], item["job_id"])
            self.assertTrue(str(destination).startswith(str(root) + os.sep))

    def test_cli_binds_stamp_ledger_preflight_and_exact_units(self):
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(quarantine, "LEDGER_INPUT_ROOT", pathlib.Path(directory)):
            args = self.exact_args(directory, apply=True)
            quarantine.validate_cli(args)
            for field, value in (
                ("run_id", "wrong"),
                ("stamp", "2026-09-01"),
                ("preflight_sha256", "0" * 64),
                ("ledger_evidence_sha256", "bad"),
                ("expected_host", "wrong-host"),
            ):
                changed = argparse.Namespace(**vars(args))
                setattr(changed, field, value)
                with self.assertRaises(common.OperatorError, msg=field):
                    quarantine.validate_cli(changed)
            changed = argparse.Namespace(**vars(args))
            changed.unit = list(reversed(changed.unit))
            with self.assertRaises(common.OperatorError):
                quarantine.validate_cli(changed)

    def test_quarantine_root_collision_is_never_adopted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory) / "drama-legacy-artifacts-20260901T050100Z"
            root.mkdir()
            with mock.patch.object(common, "HK_DATA_ROOT", pathlib.Path(directory)), \
                 mock.patch.object(common, "create_private_ancestry"):
                with self.assertRaisesRegex(common.OperatorError, "adoption is forbidden"):
                    quarantine.create_quarantine_root(root)

    def test_ledger_evidence_requires_fresh_exact_failed_contract(self):
        now = 1_777_777_777.0
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            path = root / "drama-ledger-before-20260901T050000Z.json"
            args = self.exact_args(root)

            def verify(document, captured_now=now):
                payload = json.dumps(document, sort_keys=True).encode("utf-8")
                return verify_payload(payload, captured_now)

            def verify_payload(payload, captured_now=now):
                path.write_bytes(payload)
                args.ledger_evidence_sha256 = hashlib.sha256(payload).hexdigest()
                with mock.patch.object(quarantine, "LEDGER_INPUT_ROOT", root), \
                     mock.patch.object(common, "validate_existing_ancestry"):
                    return quarantine.verify_ledger_evidence(args, now=captured_now)

            result = verify(self.ledger_document(now))
            self.assertEqual(result["sqlite"]["approved_job_statuses"], [
                {"job_id": job_id, "status": "failed"}
                for job_id in quarantine.JOB_IDS
            ])
            changed = self.ledger_document(now)
            changed["sqlite"]["approved_job_statuses"][1]["status"] = "done"
            with self.assertRaisesRegex(common.OperatorError, "drained-failed"):
                verify(changed)
            changed = self.ledger_document(now)
            changed["api_token"] = "must-not-be-recorded"
            with self.assertRaisesRegex(common.OperatorError, "credential"):
                verify(changed)
            changed = self.ledger_document(now)
            changed["snapshot"]["path"] = "/mnt/data-disk/../root/escaped.sqlite3"
            with self.assertRaisesRegex(common.OperatorError, "drained-failed"):
                verify(changed)
            changed = self.ledger_document(now)
            changed["captured_at_epoch"] = float("nan")
            with self.assertRaisesRegex(common.OperatorError, "valid JSON"):
                verify(changed)
            changed = self.ledger_document(now)
            changed["schema"] = True
            with self.assertRaisesRegex(common.OperatorError, "drained-failed"):
                verify(changed)
            changed = self.ledger_document(now)
            changed["sqlite"]["active_jobs"] = False
            with self.assertRaisesRegex(common.OperatorError, "drained-failed"):
                verify(changed)
            changed = self.ledger_document(now)
            changed["snapshot"]["sha256"] = int("1" * 64)
            with self.assertRaisesRegex(common.OperatorError, "drained-failed"):
                verify(changed)
            changed = self.ledger_document(now)
            changed["snapshot"]["comment"] = "unexpected nested field"
            with self.assertRaisesRegex(common.OperatorError, "drained-failed"):
                verify(changed)
            payload = json.dumps(self.ledger_document(now), sort_keys=True)
            payload = payload.replace('"schema": 1', '"schema": 1, "schema": 1', 1)
            with self.assertRaisesRegex(common.OperatorError, "valid JSON"):
                verify_payload(payload.encode("utf-8"))
            stale = self.ledger_document(now - quarantine.LEDGER_MAX_AGE_SECONDS - 1)
            with self.assertRaisesRegex(common.OperatorError, "stale"):
                verify(stale)

    def test_ffprobe_runs_only_against_anchored_proc_fd(self):
        with tempfile.NamedTemporaryFile(delete=False) as stream:
            stream.write(b"anchored video bytes")
            path = pathlib.Path(stream.name)
        with tempfile.NamedTemporaryFile(delete=False) as stream:
            stream.write(b"anchored ffprobe bytes")
            ffprobe_path = pathlib.Path(stream.name)
        descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_BINARY", 0))
        ffprobe_descriptor = os.open(
            str(ffprobe_path), os.O_RDONLY | getattr(os, "O_BINARY", 0))
        try:
            handle = {"fd": descriptor, "stat": common.stat_record(os.fstat(descriptor))}
            ffprobe = {
                "descriptor": ffprobe_descriptor,
                "stat": common.stat_record(os.fstat(ffprobe_descriptor)),
                "sha256": common.sha256_fd(ffprobe_descriptor),
            }
            output = json.dumps({"streams": [{"codec_type": "video"}],
                                 "format": {"format_name": "mp4", "duration": "1"}})
            with mock.patch.object(common, "run", return_value=(0, output, "")) as runner:
                probe = quarantine.probe_artifact(handle, ffprobe)
            argv = runner.call_args.args[0]
            self.assertEqual(argv[0], "/proc/self/fd/%d" % ffprobe_descriptor)
            self.assertEqual(argv[-1], "/proc/self/fd/%d" % descriptor)
            self.assertEqual(runner.call_args.kwargs["pass_fds"],
                             (ffprobe_descriptor, descriptor))
            self.assertEqual(probe["stream_count"], 1)
        finally:
            os.close(descriptor)
            os.close(ffprobe_descriptor)
            path.unlink()
            ffprobe_path.unlink()

    def test_artifact_open_binds_nofollow_parent_inode_size_sha_and_marker(self):
        parent = pathlib.Path("/approved/job")
        item = {"path": str(parent / "output.mp4"), "inode": 22, "size": 33,
                "sha256": "a" * 64}
        directory = types.SimpleNamespace(st_dev=1, st_ino=11, st_size=0,
                                          st_mtime=1.0, st_mtime_ns=1,
                                          st_mode=0o40700, st_uid=0, st_gid=0)
        artifact = types.SimpleNamespace(st_dev=1, st_ino=22, st_size=33,
                                         st_mtime=2.0, st_mtime_ns=2,
                                         st_mode=0o100600, st_uid=0, st_gid=0)
        with mock.patch.object(quarantine, "parent_root", return_value=pathlib.Path("/approved")), \
             mock.patch.object(common, "real_directory"), \
             mock.patch.object(common, "validate_existing_ancestry"), \
             mock.patch.object(quarantine.os, "lstat", return_value=directory), \
             mock.patch.object(quarantine.os, "stat", return_value=artifact), \
             mock.patch.object(quarantine.os, "open", side_effect=[100, 101]) as opener, \
             mock.patch.object(quarantine.os, "fstat",
                               side_effect=[directory, artifact, artifact]), \
             mock.patch.object(common, "sha256_fd", return_value="a" * 64), \
             mock.patch.object(quarantine, "marker_absent", return_value="output.mp4.completed.json"), \
             mock.patch.object(quarantine.os, "close"):
            handle = quarantine.open_artifact(item)
        self.assertEqual(handle["fd"], 101)
        file_call = opener.call_args_list[1]
        self.assertEqual(file_call.kwargs["dir_fd"], 100)
        self.assertTrue(file_call.args[1] & getattr(os, "O_NOFOLLOW", 0) or
                        getattr(os, "O_NOFOLLOW", 0) == 0)
        self.assertEqual(handle["stat"]["inode"], 22)
        self.assertEqual(handle["marker"], "output.mp4.completed.json")

    def test_any_completed_marker_entry_is_rejected(self):
        with mock.patch.object(quarantine.os, "stat", return_value=object()):
            with self.assertRaisesRegex(common.OperatorError, "completed marker exists"):
                quarantine.marker_absent(10, "output.mp4")

    def test_move_is_journaled_before_post_rename_hash_failure(self):
        source = pathlib.Path("/source/job/output.mp4")
        destination = pathlib.Path("/quarantine/job/work/output.mp4")
        fake = types.SimpleNamespace(st_dev=1, st_ino=2, st_size=3,
                                     st_mtime=4.0, st_mtime_ns=4,
                                     st_mode=0o100600, st_uid=0, st_gid=0)
        handle = {
            "item": {"index": 1, "job_id": quarantine.JOB_IDS[0],
                     "inode": 2, "size": 3, "sha256": "a" * 64},
            "path": source, "parent": source.parent, "parent_fd": 10, "fd": 11,
            "parent_stat": {},
            "stat": common.stat_record(fake), "sha256": "a" * 64,
            "destination_candidate": destination, "destination_parent_fd": 12,
            "destination_parent_stat": {},
            "destination": None, "move_complete": False, "target_sha256": None,
        }
        journal = []
        events = []

        def entry(directory_fd, name):
            if directory_fd == 10:
                raise FileNotFoundError(name)
            return fake

        def rename(*_args):
            events.append("rename")

        def write(path, _value):
            events.append(pathlib.Path(path).name)
            return "c" * 64

        def digest(_descriptor):
            events.append("sha256")
            return "b" * 64

        with mock.patch.object(quarantine, "verify_live_anchor"), \
             mock.patch.object(quarantine, "verify_directory_anchor"), \
             mock.patch.object(quarantine, "marker_absent"), \
             mock.patch.object(quarantine, "renameat2_noreplace", side_effect=rename), \
             mock.patch.object(quarantine, "entry_stat", side_effect=entry), \
             mock.patch.object(quarantine.os, "fstat", return_value=fake), \
             mock.patch.object(common, "sha256_fd", side_effect=digest), \
             mock.patch.object(common, "fsync_directory"), \
             mock.patch.object(common, "write_exclusive_json", side_effect=write):
            with self.assertRaisesRegex(common.OperatorError, "target SHA256"):
                quarantine.move_to_quarantine(handle, journal, pathlib.Path("/evidence"))
        self.assertEqual(journal, [handle])
        self.assertTrue(handle["move_complete"])
        self.assertEqual(handle["destination"], destination)
        self.assertLess(events.index("move-intent-01.json"), events.index("rename"))
        self.assertLess(events.index("rename-01.json"), events.index("sha256"))

    def test_rollback_uses_no_replace_rename_and_never_delete(self):
        source = pathlib.Path("/source/job/output.mp4")
        destination = pathlib.Path("/quarantine/job/work/output.mp4")
        fake = types.SimpleNamespace(st_dev=1, st_ino=2, st_size=3,
                                     st_mtime=4.0, st_mtime_ns=4,
                                     st_mode=0o100600, st_uid=0, st_gid=0)
        handle = {
            "item": {"index": 1, "sha256": "a" * 64},
            "path": source, "parent": source.parent,
            "parent_fd": 10, "fd": 11, "parent_stat": {},
            "stat": common.stat_record(fake),
            "destination_parent_fd": 12, "destination": destination,
            "destination_parent_stat": {},
            "move_complete": True, "target_sha256": "a" * 64,
        }

        def entry(directory_fd, name):
            if directory_fd == 10:
                raise FileNotFoundError(name)
            return fake

        with mock.patch.object(quarantine, "marker_absent"), \
             mock.patch.object(quarantine, "verify_directory_anchor"), \
             mock.patch.object(quarantine, "entry_stat", side_effect=entry), \
             mock.patch.object(quarantine, "renameat2_noreplace") as rename, \
             mock.patch.object(quarantine, "verify_live_anchor"), \
             mock.patch.object(common, "sha256_fd", return_value="a" * 64), \
             mock.patch.object(common, "fsync_directory"):
            self.assertEqual(quarantine.rollback_moves([handle]), [])
        rename.assert_called_once_with(12, destination.name, 10, source.name)
        self.assertFalse(handle["move_complete"])
        source_text = (CONTROL / "drama_quarantine.py").read_text(encoding="utf-8")
        self.assertNotIn("os.unlink", source_text)
        self.assertNotIn("os.remove", source_text)
        self.assertNotIn("systemctl", source_text)
        self.assertIn('control / ".drama-release-hk.lock"', source_text)
        self.assertNotIn(".drama-quarantine.lock", source_text)

    def test_unjournaled_or_ambiguous_move_cannot_prove_rollback_complete(self):
        with mock.patch.object(
                quarantine, "verify_live_anchor",
                side_effect=common.OperatorError("source missing after uncertain rename")):
            with self.assertRaisesRegex(common.OperatorError, "source missing"):
                quarantine.prove_all_sources_restored([{"item": {"index": 1}}])

    def test_guard_requires_worker_stopped_and_preserves_tunnel_and_eight_units(self):
        units = {unit: {"fragment": {"path": "/etc/%s" % unit,
                                     "sha256": "a" * 64}}
                 for unit in common.HK_TARGET_UNITS + common.HK_PROTECTED_UNITS}
        baseline = {unit: units[unit] for unit in quarantine.PRESERVED_UNITS}
        args = types.SimpleNamespace(fragment=[] , apply=False)
        with mock.patch.object(common, "snapshot_units", return_value=units), \
             mock.patch.object(common, "assert_inactive_unit") as inactive, \
             mock.patch.object(common, "assert_active_single_process") as active, \
             mock.patch.object(common, "assert_protected_units") as protected, \
             mock.patch.object(common, "assert_no_media_processes"), \
             mock.patch.object(quarantine, "assert_no_listener_8787"), \
             mock.patch.object(common, "assert_no_established_ports"):
            quarantine.snapshot_and_guard(args, baseline=baseline)
        inactive.assert_called_once_with(units[quarantine.WORKER_UNIT])
        self.assertEqual([call.args[0] for call in active.call_args_list],
                         [units[unit] for unit in quarantine.PRESERVED_UNITS])
        protected.assert_called_once()
        self.assertEqual(set(protected.call_args.args[1]), set(quarantine.PRESERVED_UNITS))

    def test_orphan_listener_on_stopped_worker_port_is_rejected(self):
        with mock.patch.object(
                common, "run",
                return_value=(0, 'LISTEN 0 128 127.0.0.1:8787 users:(("python",pid=9,fd=3))\n',
                              "")) as runner:
            with self.assertRaisesRegex(common.OperatorError, "listener remains"):
                quarantine.assert_no_listener_8787()
        runner.assert_called_once_with(["ss", "-Hltnp", "sport = :8787"])

        with mock.patch.object(common, "run", return_value=(0, "\n", "")):
            self.assertEqual(quarantine.assert_no_listener_8787(),
                             {"port": 8787, "listener_count": 0})

    def test_sigterm_is_converted_to_auditable_operator_exception(self):
        installed = {}

        def install(value, handler):
            installed[value] = handler

        with mock.patch.object(quarantine.signal, "getsignal", return_value="old"), \
             mock.patch.object(quarantine.signal, "signal", side_effect=install):
            with self.assertRaises(quarantine.OperatorInterrupted):
                with quarantine.interruption_guard():
                    installed[quarantine.signal.SIGTERM](quarantine.signal.SIGTERM, None)
        self.assertEqual(installed[quarantine.signal.SIGTERM], "old")

    def test_default_main_is_read_only_and_never_calls_apply(self):
        result = {"ready": True, "mode": "dry-run"}
        output = io.StringIO()
        with mock.patch.object(quarantine, "validate_cli"), \
             mock.patch.object(quarantine, "inspect", return_value=result), \
             mock.patch.object(quarantine, "apply") as apply, \
             contextlib.redirect_stdout(output):
            argv = [
                "--run-id", common.RUN_ID,
                "--stamp", "20260901T050100Z",
                "--expected-host", common.HK_HOST,
                "--data-root", str(common.HK_DATA_ROOT),
                "--expected-data-device", "/dev/test",
                "--expected-current-sha", common.NEW_SHA,
                "--preflight", str(quarantine.PREFLIGHT_PATH),
                "--preflight-sha256", quarantine.PREFLIGHT_SHA256,
                "--ledger-evidence", str(quarantine.LEDGER_INPUT_ROOT /
                                         "drama-ledger-before-20260901T050000Z.json"),
                "--ledger-evidence-sha256", "a" * 64,
                "--ffprobe", str(quarantine.FFPROBE_DEFAULT),
            ]
            for unit in common.HK_TARGET_UNITS:
                argv.extend(["--unit", unit])
            for unit in common.HK_PROTECTED_UNITS:
                argv.extend(["--protected-unit", unit])
            self.assertEqual(quarantine.main(argv), 0)
        apply.assert_not_called()
        self.assertTrue(json.loads(output.getvalue())["ready"])


if __name__ == "__main__":
    unittest.main()
