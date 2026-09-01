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
                swap = release.create_cpu_swap(relative, root)
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
                    "new_sha256": hashlib.sha256(new).hexdigest()}
            with mock.patch.object(common, "atomic_rename_exchange",
                                   side_effect=portable_exchange), \
                 mock.patch.object(common, "fsync_directory"):
                self.assertEqual(release.restore_cpu_swaps([item]), [])
            self.assertEqual(live.read_bytes(), old)
            self.assertEqual(temporary.read_bytes(), new)

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
                record = release.switch_hk_current()
                self.assertEqual(os.path.realpath(str(current)), str(new))
                release.restore_hk_current(record)
                self.assertEqual(os.path.realpath(str(current)), str(old))

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
