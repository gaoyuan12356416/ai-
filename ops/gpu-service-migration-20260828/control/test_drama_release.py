import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import pathlib
import subprocess
import tempfile
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
    release = load("drama_release")


def portable_exchange(left, right):
    left = pathlib.Path(left)
    right = pathlib.Path(right)
    hold = left.parent / (left.name + ".exchange-test")
    if hold.exists() or hold.is_symlink():
        raise RuntimeError("test exchange collision")
    os.rename(str(left), str(hold))
    os.rename(str(right), str(left))
    os.rename(str(hold), str(right))


def portable_content_exchange(left, right):
    left = pathlib.Path(left)
    right = pathlib.Path(right)
    left_bytes = left.read_bytes()
    right_bytes = right.read_bytes()
    left.write_bytes(right_bytes)
    right.write_bytes(left_bytes)


def portable_noreplace(source, destination):
    source = pathlib.Path(source)
    destination = pathlib.Path(destination)
    if destination.exists() or destination.is_symlink():
        raise RuntimeError("test no-replace collision")
    os.rename(str(source), str(destination))


class DramaReleaseTests(unittest.TestCase):
    def exact_args(self, role):
        contract = release.role_contract(role)
        return argparse.Namespace(
            role=role, run_id=common.RUN_ID, expected_host=contract["host"],
            expected_old_sha=common.OLD_SHA, expected_new_sha=common.NEW_SHA,
            data_root=str(contract["data_root"]), expected_data_device="/dev/test",
            source_root=str(contract["source_root"]),
            unit=list(contract["target_units"]),
            protected_unit=list(contract["protected_units"]), fragment=[], apply=False,
        )

    def test_exact_host_commit_path_and_unit_bindings(self):
        self.assertEqual(common.CPU_TARGET_UNITS, (
            "drama-material-job-worker.service",
            "drama-material-api.service",
        ))
        self.assertNotIn("drama-material-worker.service", common.CPU_TARGET_UNITS)
        for role in ("cpu", "hk"):
            args = self.exact_args(role)
            self.assertEqual(release.validate_cli(args), release.role_contract(role))
            for field, value in (
                ("expected_host", "wrong-host"),
                ("expected_old_sha", "0" * 40),
                ("expected_new_sha", "1" * 40),
                ("data_root", "/tmp"),
                ("source_root", "/tmp/source"),
            ):
                changed = argparse.Namespace(**vars(args))
                setattr(changed, field, value)
                with self.assertRaises(common.OperatorError, msg=field):
                    release.validate_cli(changed)
            changed = argparse.Namespace(**vars(args))
            changed.unit = list(reversed(args.unit))
            with self.assertRaises(common.OperatorError):
                release.validate_cli(changed)
            changed = argparse.Namespace(**vars(args))
            changed.protected_unit = (changed.protected_unit[:-1]
                                      if changed.protected_unit else ["unexpected.service"])
            with self.assertRaises(common.OperatorError):
                release.validate_cli(changed)

    def test_not_found_cpu_worker_name_is_rejected_before_fragment_access(self):
        wrong = "drama-material-worker.service"
        output = ("Id=%s\nLoadState=not-found\nNeedDaemonReload=no\n"
                  "FragmentPath=\n" % wrong)
        with mock.patch.object(common, "run", return_value=(0, output, "")), \
             mock.patch.object(common, "fragment_record") as fragment_record:
            with self.assertRaisesRegex(common.OperatorError, "not loaded"):
                common.unit_identity(wrong)
        fragment_record.assert_not_called()

    def test_reviewed_commit_file_hash_constants_match_git_objects(self):
        repository = pathlib.Path(__file__).resolve().parents[3]
        for commit, mapping in ((common.OLD_SHA, common.CPU_OLD_FILES),
                                (common.NEW_SHA, common.CPU_NEW_FILES)):
            for relative, expected in mapping.items():
                data = subprocess.check_output(
                    ["git", "-C", str(repository), "show", "%s:%s" % (commit, relative)])
                self.assertEqual(hashlib.sha256(data).hexdigest(), expected)
        remote = subprocess.check_output(
            ["git", "-C", str(repository), "rev-parse", common.NEW_REMOTE_REF],
            universal_newlines=True).strip()
        self.assertEqual(remote, common.NEW_SHA)

    def test_apply_requires_fresh_exact_fragment_bindings(self):
        item = {"unit": "drama.service",
                "fragment": {"path": "/etc/systemd/system/drama.service",
                             "sha256": "a" * 64}}
        snapshot = {"drama.service": item}
        with self.assertRaises(common.OperatorError):
            release.validate_fragment_bindings(snapshot, {}, True)
        binding = {"drama.service": {"path": item["fragment"]["path"],
                                      "sha256": "a" * 64}}
        self.assertEqual(release.validate_fragment_bindings(snapshot, binding, True), binding)
        changed = {"drama.service": {"path": item["fragment"]["path"],
                                      "sha256": "b" * 64}}
        with self.assertRaises(common.OperatorError):
            release.validate_fragment_bindings(snapshot, changed, True)

    def test_default_cli_is_dry_run_and_never_calls_apply(self):
        contract = release.role_contract("cpu")
        snapshot = {"mode": "dry-run", "unit_snapshot": {}, "ready": True}
        argv = [
            "cpu", "--run-id", common.RUN_ID, "--expected-host", common.CPU_HOST,
            "--expected-old-sha", common.OLD_SHA, "--expected-new-sha", common.NEW_SHA,
            "--data-root", str(common.CPU_DATA_ROOT), "--expected-data-device", "/dev/test",
            "--source-root", str(contract["source_root"]),
            "--unit", common.CPU_TARGET_UNITS[0], "--unit", common.CPU_TARGET_UNITS[1],
        ]
        output = io.StringIO()
        with mock.patch.object(release, "initial_snapshot", return_value=snapshot), \
             mock.patch.object(release, "apply_cpu") as apply_cpu, \
             mock.patch.object(release, "apply_hk") as apply_hk, \
             contextlib.redirect_stdout(output):
            self.assertEqual(release.main(argv), 0)
        apply_cpu.assert_not_called()
        apply_hk.assert_not_called()
        self.assertTrue(json.loads(output.getvalue())["ready"])

    @unittest.skipIf(os.name == "nt", "Windows cannot rename an open anchored file")
    def test_cpu_atomic_exchange_and_rollback_preserve_exact_old_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source_root = root / "source"
            live_root = root / "live"
            source_root.mkdir()
            live_root.mkdir()
            relative = "app.py"
            old = b"old reviewed app\n"
            new = b"new reviewed app\n"
            (source_root / relative).write_bytes(new)
            (live_root / relative).write_bytes(old)
            old_files = {relative: hashlib.sha256(old).hexdigest()}
            new_files = {relative: hashlib.sha256(new).hexdigest()}
            original_contract = release.role_contract

            def contract(role):
                value = dict(original_contract(role))
                if role == "cpu":
                    value["source_root"] = source_root
                return value

            with mock.patch.object(release, "role_contract", side_effect=contract), \
                 mock.patch.object(common, "CPU_LIVE_ROOT", live_root), \
                 mock.patch.object(common, "CPU_OLD_FILES", old_files), \
                 mock.patch.object(common, "CPU_NEW_FILES", new_files), \
                 mock.patch.object(common, "atomic_rename_exchange", side_effect=portable_exchange), \
                 mock.patch.object(common, "fsync_directory"):
                swaps = []
                swap = release.create_cpu_swap(relative, root, swaps)
                self.assertEqual(swaps, [swap])
                self.assertEqual((live_root / relative).read_bytes(), new)
                self.assertEqual(pathlib.Path(swap["temporary_old"]).read_bytes(), old)
                self.assertEqual(release.restore_cpu_swaps([swap]), [])
                self.assertEqual((live_root / relative).read_bytes(), old)

    def test_cpu_rollback_exchange_restores_old_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            live = root / "app.py"
            temporary = root / ".old-app.py"
            old = b"old exact bytes"
            new = b"new exact bytes"
            live.write_bytes(new)
            temporary.write_bytes(old)
            item = {"relative": "app.py", "live": str(live),
                    "temporary_old": str(temporary),
                    "old_sha256": hashlib.sha256(old).hexdigest(),
                    "new_sha256": hashlib.sha256(new).hexdigest(),
                    "exchange_complete": True}
            with mock.patch.object(common, "atomic_rename_exchange",
                                   side_effect=portable_exchange), \
                 mock.patch.object(common, "fsync_directory"):
                self.assertEqual(release.restore_cpu_swaps([item]), [])
            self.assertEqual(live.read_bytes(), old)
            self.assertEqual(temporary.read_bytes(), new)

    def test_cpu_exchange_is_journaled_before_post_exchange_fsync_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source_root = root / "source"
            live_root = root / "live"
            source_root.mkdir()
            live_root.mkdir()
            relative = "app.py"
            old = b"old reviewed app\n"
            new = b"new reviewed app\n"
            (source_root / relative).write_bytes(new)
            (live_root / relative).write_bytes(old)
            old_files = {relative: hashlib.sha256(old).hexdigest()}
            new_files = {relative: hashlib.sha256(new).hexdigest()}
            original_contract = release.role_contract

            def contract(role):
                value = dict(original_contract(role))
                if role == "cpu":
                    value["source_root"] = source_root
                return value

            fsync_calls = []

            def fail_after_exchange(path):
                fsync_calls.append(pathlib.Path(path))
                if len(fsync_calls) == 2:
                    raise OSError("injected post-exchange fsync failure")

            swaps = []
            with mock.patch.object(release, "role_contract", side_effect=contract), \
                 mock.patch.object(common, "CPU_LIVE_ROOT", live_root), \
                 mock.patch.object(common, "CPU_OLD_FILES", old_files), \
                 mock.patch.object(common, "CPU_NEW_FILES", new_files), \
                 mock.patch.object(common, "atomic_rename_exchange",
                                   side_effect=portable_content_exchange), \
                 mock.patch.object(common, "fsync_directory",
                                   side_effect=fail_after_exchange):
                with self.assertRaisesRegex(OSError, "post-exchange"):
                    release.create_cpu_swap(relative, root, swaps)
            self.assertEqual(len(swaps), 1)
            self.assertTrue(swaps[0]["exchange_complete"])
            self.assertEqual((live_root / relative).read_bytes(), new)
            with mock.patch.object(common, "atomic_rename_exchange",
                                   side_effect=portable_content_exchange), \
                 mock.patch.object(common, "fsync_directory"):
                self.assertEqual(release.restore_cpu_swaps(swaps), [])
            self.assertEqual((live_root / relative).read_bytes(), old)

    def test_cpu_result_failure_keeps_rollback_temporaries(self):
        state = {"committed": False}
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(common, "write_exclusive_json",
                               side_effect=OSError("injected result failure")), \
             mock.patch.object(release, "cleanup_cpu_temporaries") as cleanup:
            with self.assertRaisesRegex(OSError, "result failure"):
                release.persist_cpu_result_and_cleanup(
                    pathlib.Path(directory), {"result": "deployed"}, [], state)
        self.assertFalse(state["committed"])
        cleanup.assert_not_called()

    def test_cpu_cleanup_failure_occurs_only_after_durable_commit(self):
        state = {"committed": False}
        writes = []

        def write(path, value):
            writes.append(pathlib.Path(path).name)
            return "a" * 64

        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(common, "write_exclusive_json", side_effect=write), \
             mock.patch.object(release, "cleanup_cpu_temporaries",
                               side_effect=OSError("injected cleanup failure")):
            with self.assertRaisesRegex(OSError, "cleanup failure"):
                release.persist_cpu_result_and_cleanup(
                    pathlib.Path(directory), {"result": "deployed"}, [], state)
        self.assertTrue(state["committed"])
        self.assertEqual(writes, ["result.json"])

    def test_cpu_apply_never_rolls_back_after_result_commit(self):
        contract = release.role_contract("cpu")
        units = {unit: {} for unit in common.CPU_TARGET_UNITS}
        active_api = {"process": {"pid": 123}}

        def commit_then_fail(evidence, result, swaps, state):
            state["committed"] = True
            raise OSError("injected post-commit cleanup failure")

        def reviewed_hash(path):
            value = str(path).replace("\\", "/")
            for relative, expected in common.CPU_NEW_FILES.items():
                if value.endswith(relative):
                    return expected
            raise AssertionError("unexpected hash path: %s" % path)

        with contextlib.ExitStack() as stack:
            for owner, name in (
                (common, "create_private_ancestry"),
                (release, "phase"),
                (common, "assert_no_media_processes"),
                (common, "assert_no_established_ports"),
                (release, "inspect_cpu_database"),
                (common, "assert_inactive_unit"),
                (release, "config_unchanged"),
                (release, "create_cpu_swap"),
                (release, "compile_cpu_files"),
            ):
                stack.enter_context(mock.patch.object(owner, name))
            systemctl = stack.enter_context(mock.patch.object(release, "systemctl"))
            stack.enter_context(mock.patch.object(
                release, "compact_snapshot", return_value={}))
            stack.enter_context(mock.patch.object(
                release, "backup_cpu_files", return_value={}))
            stack.enter_context(mock.patch.object(
                release, "wait_unit", return_value=active_api))
            stack.enter_context(mock.patch.object(
                common, "snapshot_units", return_value=units))
            stack.enter_context(mock.patch.object(
                release, "target_restart_bound", return_value={}))
            stack.enter_context(mock.patch.object(
                common, "exact_health", return_value={"ok": True}))
            stack.enter_context(mock.patch.object(
                release, "listener_owned_by", return_value={}))
            stack.enter_context(mock.patch.object(
                common, "sha256_file", side_effect=reviewed_hash))
            stack.enter_context(mock.patch.object(
                common, "protected_signature", return_value={}))
            stack.enter_context(mock.patch.object(
                release, "persist_cpu_result_and_cleanup", side_effect=commit_then_fail))
            stack.enter_context(mock.patch.object(
                common, "write_exclusive_json", return_value="a" * 64))
            with self.assertRaisesRegex(common.OperatorError, "POST-COMMIT"):
                release.apply_cpu(self.exact_args("cpu"), contract,
                                  {"unit_snapshot": units, "mode": "apply"})
        self.assertEqual(systemctl.call_args_list, [
            mock.call("stop", common.CPU_TARGET_UNITS[1]),
            mock.call("start", common.CPU_TARGET_UNITS[1]),
        ])

    def test_cpu_backup_is_exclusive_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            live_root = root / "live"
            evidence = root / "evidence"
            live_root.mkdir()
            evidence.mkdir()
            data = b"only approved bytes\n"
            (live_root / "app.py").write_bytes(data)
            mapping = {"app.py": hashlib.sha256(data).hexdigest()}
            with mock.patch.object(common, "CPU_LIVE_ROOT", live_root), \
                 mock.patch.object(common, "CPU_OLD_FILES", mapping), \
                 mock.patch.object(common, "fsync_directory"):
                first = release.backup_cpu_files(evidence)
                self.assertEqual(common.sha256_file(first["manifest"]), first["manifest_sha256"])
                with self.assertRaises(FileExistsError):
                    release.backup_cpu_files(evidence)

    @unittest.skipIf(os.name == "nt", "Windows test user cannot create symlinks")
    def test_hk_current_exchange_is_reversible_and_never_adopts_other_target(self):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            releases = base / "releases"
            releases.mkdir()
            old = releases / common.OLD_SHA
            new = releases / common.NEW_SHA
            old.mkdir()
            new.mkdir()
            current = base / "current"
            os.symlink(str(old), str(current), target_is_directory=True)
            with mock.patch.object(release, "HK_RELEASES", releases), \
                 mock.patch.object(release, "HK_CURRENT", current), \
                 mock.patch.object(common, "HK_BASE", base), \
                 mock.patch.object(common, "atomic_rename_exchange", side_effect=portable_exchange), \
                 mock.patch.object(common, "fsync_directory"):
                record = {}
                release.switch_hk_current(record)
                self.assertEqual(os.path.realpath(str(current)), str(new))
                release.restore_hk_current(record)
                self.assertEqual(os.path.realpath(str(current)), str(old))

    @unittest.skipIf(os.name == "nt", "Windows test user cannot create symlinks")
    def test_hk_exchange_is_journaled_before_post_exchange_fsync_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            releases = base / "releases"
            releases.mkdir()
            old = releases / common.OLD_SHA
            new = releases / common.NEW_SHA
            old.mkdir()
            new.mkdir()
            current = base / "current"
            os.symlink(str(old), str(current), target_is_directory=True)
            fsync_calls = []

            def fail_after_exchange(path):
                fsync_calls.append(pathlib.Path(path))
                if len(fsync_calls) == 2:
                    raise OSError("injected HK post-exchange fsync failure")

            record = {}
            with mock.patch.object(release, "HK_RELEASES", releases), \
                 mock.patch.object(release, "HK_CURRENT", current), \
                 mock.patch.object(common, "HK_BASE", base), \
                 mock.patch.object(common, "atomic_rename_exchange",
                                   side_effect=portable_exchange), \
                 mock.patch.object(common, "fsync_directory",
                                   side_effect=fail_after_exchange):
                with self.assertRaisesRegex(OSError, "post-exchange"):
                    release.switch_hk_current(record)
            self.assertTrue(record["exchange_complete"])
            self.assertEqual(os.path.realpath(str(current)), str(new))
            with mock.patch.object(release, "HK_RELEASES", releases), \
                 mock.patch.object(release, "HK_CURRENT", current), \
                 mock.patch.object(common, "HK_BASE", base), \
                 mock.patch.object(common, "atomic_rename_exchange",
                                   side_effect=portable_exchange), \
                 mock.patch.object(common, "fsync_directory"):
                release.restore_hk_current(record)
            self.assertEqual(os.path.realpath(str(current)), str(old))

    @unittest.skipIf(os.name == "nt", "Windows test user cannot create symlinks")
    def test_hk_retained_link_location_updates_before_fsync_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            releases = base / "releases"
            evidence = base / "evidence"
            releases.mkdir()
            evidence.mkdir()
            old = releases / common.OLD_SHA
            new = releases / common.NEW_SHA
            old.mkdir()
            new.mkdir()
            current = base / "current"
            os.symlink(str(old), str(current), target_is_directory=True)
            record = {}
            with mock.patch.object(release, "HK_RELEASES", releases), \
                 mock.patch.object(release, "HK_CURRENT", current), \
                 mock.patch.object(common, "HK_BASE", base), \
                 mock.patch.object(common, "atomic_rename_exchange",
                                   side_effect=portable_exchange), \
                 mock.patch.object(common, "fsync_directory"):
                release.switch_hk_current(record)
            retained = evidence / "current-before"
            with mock.patch.object(common, "atomic_rename_noreplace",
                                   side_effect=portable_noreplace), \
                 mock.patch.object(common, "fsync_directory",
                                   side_effect=OSError("injected retained-link fsync failure")):
                with self.assertRaisesRegex(OSError, "retained-link"):
                    release.retain_hk_old_link(record, evidence)
            self.assertEqual(record["old_link_temporary"], str(retained))
            self.assertTrue(record["old_link_retained"])
            with mock.patch.object(release, "HK_RELEASES", releases), \
                 mock.patch.object(release, "HK_CURRENT", current), \
                 mock.patch.object(common, "HK_BASE", base), \
                 mock.patch.object(common, "atomic_rename_exchange",
                                   side_effect=portable_exchange), \
                 mock.patch.object(common, "fsync_directory"):
                release.restore_hk_current(record)
            self.assertEqual(os.path.realpath(str(current)), str(old))

    def test_hk_rollback_proof_rejects_current_still_on_new_release(self):
        fake_symlink = mock.Mock(st_mode=release.stat.S_IFLNK | 0o777)
        with mock.patch.object(release.os, "lstat", return_value=fake_symlink), \
             mock.patch.object(release.os.path, "realpath",
                               return_value=str(release.HK_RELEASES / common.NEW_SHA)):
            with self.assertRaisesRegex(common.OperatorError, "expected release"):
                release.assert_hk_current_release(release.HK_RELEASES / common.OLD_SHA)

    def test_protected_pid_startticks_and_restart_count_must_be_identical(self):
        item = {
            "unit": "x.service",
            "systemd": {"ActiveState": "active", "SubState": "running",
                        "ControlPID": "0", "NRestarts": "3",
                        "UnitFileState": "enabled", "Restart": "always"},
            "fragment": {"path": "/etc/x.service", "sha256": "a" * 64},
            "dropins": [],
            "process": {"pid": 11, "startticks": 22, "children": []},
            "cgroup": {"pids": [11]},
        }
        common.assert_protected_units({"x.service": item}, {"x.service": dict(item)})
        for field, value in (("pid", 12), ("startticks", 23)):
            changed = dict(item)
            changed["process"] = dict(item["process"], **{field: value})
            with self.assertRaises(common.OperatorError):
                common.assert_protected_units({"x.service": item}, {"x.service": changed})
        changed = dict(item)
        changed["systemd"] = dict(item["systemd"], NRestarts="4")
        with self.assertRaises(common.OperatorError):
            common.assert_protected_units({"x.service": item}, {"x.service": changed})

    def test_systemctl_cannot_target_unapproved_action(self):
        with mock.patch.object(common, "run") as runner:
            release.systemctl("start", common.HK_TARGET_UNITS[0])
            runner.assert_called_once_with([
                "systemctl", "--job-mode=ignore-dependencies", "start",
                common.HK_TARGET_UNITS[0]])
        with self.assertRaises(common.OperatorError):
            release.systemctl("restart", common.HK_PROTECTED_UNITS[0])

    def test_cpu_pre_mutation_guard_failure_never_restarts_api(self):
        contract = release.role_contract("cpu")
        before = {"unit_snapshot": {}, "mode": "apply"}
        with mock.patch.object(common, "create_private_ancestry"), \
             mock.patch.object(release, "phase"), \
             mock.patch.object(release, "backup_cpu_files", return_value={}), \
             mock.patch.object(common, "assert_no_media_processes",
                               side_effect=common.OperatorError("busy")), \
             mock.patch.object(common, "write_exclusive_json"), \
             mock.patch.object(release, "systemctl") as systemctl:
            with self.assertRaises(common.OperatorError):
                release.apply_cpu(self.exact_args("cpu"), contract, before)
        systemctl.assert_not_called()


if __name__ == "__main__":
    unittest.main()
