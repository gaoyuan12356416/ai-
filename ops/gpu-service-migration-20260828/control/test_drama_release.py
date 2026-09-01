import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import pathlib
import re
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


@contextlib.contextmanager
def hk_runtime_fixture(record_changes=None, raw_overrides=None,
                       partial_record_changes=None, partial_raw_overrides=None):
    record_changes = record_changes or {}
    raw_overrides = raw_overrides or {}
    partial_record_changes = partial_record_changes or {}
    partial_raw_overrides = partial_raw_overrides or {}
    with tempfile.TemporaryDirectory() as directory:
        base = pathlib.Path(directory)
        releases = base / "releases"
        work = base / "work" / "jobs"
        public = base / "results" / "public"
        jobs = work / ".runtime" / "jobs"
        diagnostics = work / ".runtime" / "diagnostics"
        for path in (releases, public, jobs, diagnostics):
            path.mkdir(parents=True, exist_ok=True)
        records = {}
        paths = {}
        hashes = {}
        for job_id in release.JOB_IDS:
            record = {
                "version": 1,
                "job_id": job_id,
                "fingerprint": release.HK_RUNTIME_FINGERPRINTS[job_id],
                "generation": 1,
                "status": "failed",
                "stage": "failed",
                "error": {"code": "gpu_render_failed", "message": "safe"},
                "_children": {},
                "_launches": {},
                "_resource_blocked": False,
                "_cache_blocked": False,
                "_payload": {"private": "must-not-appear-in-evidence"},
            }
            record.update(record_changes.get(job_id, {}))
            raw = raw_overrides.get(job_id)
            if raw is None:
                raw = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
            elif isinstance(raw, str):
                raw = raw.encode("utf-8")
            path = jobs / (job_id + ".json")
            path.write_bytes(raw)
            records[job_id] = record
            paths[job_id] = path
            hashes[job_id] = hashlib.sha256(raw).hexdigest()
        download_specs = []
        part_paths = {}
        partial_record_paths = {}
        partial_records = {}
        for original in release.HK_DOWNLOAD_PARTS:
            identity = (original["job_id"], original["episode"])
            part = (work / identity[0] / "downloads" /
                    (identity[1] + ".mp4.part"))
            part.parent.mkdir(parents=True, exist_ok=True)
            part_bytes = (b"" if identity[1] == "005" else
                          (identity[0] + "-" + identity[1]).encode("ascii"))
            part.write_bytes(part_bytes)
            part_sha = hashlib.sha256(part_bytes).hexdigest()
            partial = {
                "version": 1,
                "source_identity": "test-source-" + identity[0] + "-" + identity[1],
                "etag": '"test-etag-%s"' % identity[1],
                "expected_size": len(part_bytes) + 100,
                "partial_size": len(part_bytes),
                "partial_sha256": part_sha,
            }
            partial.update(partial_record_changes.get(identity, {}))
            record_bytes = partial_raw_overrides.get(identity)
            if record_bytes is None:
                record_bytes = json.dumps(
                    partial, sort_keys=True, separators=(",", ":")).encode("utf-8")
            elif isinstance(record_bytes, str):
                record_bytes = record_bytes.encode("utf-8")
            record_path = part.with_name(part.name + ".json")
            record_path.write_bytes(record_bytes)
            part_stat = os.lstat(str(part))
            record_stat = os.lstat(str(record_path))
            download_specs.append({
                "job_id": identity[0], "episode": identity[1],
                "part_inode": int(part_stat.st_ino),
                "part_size": len(part_bytes), "part_sha256": part_sha,
                "record_inode": int(record_stat.st_ino),
                "record_size": len(record_bytes),
                "record_sha256": hashlib.sha256(record_bytes).hexdigest(),
                "expected_size": len(part_bytes) + 100,
            })
            part_paths[identity] = part
            partial_record_paths[identity] = record_path
            partial_records[identity] = partial
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(common, "HK_BASE", base))
            stack.enter_context(mock.patch.object(release, "HK_RELEASES", releases))
            stack.enter_context(mock.patch.object(release, "HK_WORK_ROOT", work))
            stack.enter_context(mock.patch.object(release, "HK_PUBLIC_ROOT", public))
            stack.enter_context(mock.patch.object(release, "HK_RUNTIME_ACTIVE", jobs))
            stack.enter_context(mock.patch.object(
                release, "HK_RUNTIME_DIAGNOSTICS", diagnostics))
            stack.enter_context(mock.patch.object(
                release, "HK_RUNTIME_FILE_SHA256", hashes))
            stack.enter_context(mock.patch.object(
                release, "HK_DOWNLOAD_PARTS", tuple(download_specs)))
            yield {
                "base": base, "work": work, "public": public, "jobs": jobs,
                "diagnostics": diagnostics, "records": records,
                "paths": paths, "hashes": hashes,
                "download_specs": download_specs, "part_paths": part_paths,
                "partial_record_paths": partial_record_paths,
                "partial_records": partial_records,
            }


class DramaReleaseTests(unittest.TestCase):
    def exact_args(self, role):
        contract = release.role_contract(role)
        return argparse.Namespace(
            role=role, run_id=common.RUN_ID, expected_host=contract["host"],
            expected_old_sha=common.OLD_SHA, expected_new_sha=common.NEW_SHA,
            data_root=str(contract["data_root"]), expected_data_device="/dev/test",
            source_root=str(contract["source_root"]),
            unit=list(contract["target_units"]),
            protected_unit=list(contract["protected_units"]), fragment=[],
            reviewed_failure_resume=False, reviewed_failure_path=None,
            reviewed_failure_sha256=None, retry_id=None, apply=False,
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

    def test_reviewed_failure_resume_requires_all_exact_hk_bindings(self):
        args = self.exact_args("hk")
        args.reviewed_failure_resume = True
        args.reviewed_failure_path = str(release.HK_REVIEWED_FAILURE_PATH)
        args.reviewed_failure_sha256 = release.HK_REVIEWED_FAILURE_SHA256
        args.retry_id = release.HK_RETRY_ID
        contract = release.validate_cli(args)
        self.assertEqual(contract["evidence"], release.HK_RETRY_EVIDENCE)
        self.assertEqual(contract["reviewed_failure_resume"]["retry_id"],
                         release.HK_RETRY_ID)
        for field, value in (
                ("reviewed_failure_path", "/data/wrong/failure.json"),
                ("reviewed_failure_sha256", "0" * 64),
                ("retry_id", "wrong-retry")):
            changed = argparse.Namespace(**vars(args))
            setattr(changed, field, value)
            with self.assertRaisesRegex(common.OperatorError, "binding"):
                release.validate_cli(changed)
        cpu = self.exact_args("cpu")
        cpu.reviewed_failure_resume = True
        cpu.reviewed_failure_path = str(release.HK_REVIEWED_FAILURE_PATH)
        cpu.reviewed_failure_sha256 = release.HK_REVIEWED_FAILURE_SHA256
        cpu.retry_id = release.HK_RETRY_ID
        with self.assertRaisesRegex(common.OperatorError, "exact HK retry"):
            release.validate_cli(cpu)
        partial = self.exact_args("hk")
        partial.reviewed_failure_path = str(release.HK_REVIEWED_FAILURE_PATH)
        with self.assertRaisesRegex(common.OperatorError, "explicit"):
            release.validate_cli(partial)

    def test_reviewed_failure_is_nofollow_hashed_and_strictly_validated(self):
        valid = {
            "schema": 1, "result": "failed", "host_role": "hk",
            "run_id": common.RUN_ID, "old_sha": common.OLD_SHA,
            "new_sha": common.NEW_SHA, "release_published": True,
            "error_type": "OperatorError",
            "rollback": {"attempted": True, "complete": True, "errors": []},
            "failed_at_epoch": 1.5,
        }
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory).resolve()
            path = root / "failure.json"

            def inspect(payload):
                raw = (json.dumps(payload, sort_keys=True, indent=2).encode("utf-8") +
                       b"\n")
                path.write_bytes(raw)
                digest = hashlib.sha256(raw).hexdigest()
                with mock.patch.object(common, "HK_DATA_ROOT", root), \
                     mock.patch.object(release, "HK_REVIEWED_FAILURE_PATH", path), \
                     mock.patch.object(release, "HK_REVIEWED_FAILURE_SHA256", digest):
                    return release.verify_reviewed_hk_failure(path, digest)

            summary = inspect(valid)
            self.assertTrue(summary["rollback_complete"])
            self.assertEqual(summary["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
            changed = dict(valid, release_published=False)
            with self.assertRaisesRegex(common.OperatorError, "complete safe rollback"):
                inspect(changed)
            changed = dict(valid, unexpected=True)
            with self.assertRaisesRegex(common.OperatorError, "fields changed"):
                inspect(changed)
            raw = (json.dumps(valid, sort_keys=True, indent=2)
                   .replace('"result": "failed",',
                            '"result": "failed",\n  "result": "failed",', 1)
                   .encode("utf-8") + b"\n")
            path.write_bytes(raw)
            digest = hashlib.sha256(raw).hexdigest()
            with mock.patch.object(common, "HK_DATA_ROOT", root), \
                 mock.patch.object(release, "HK_REVIEWED_FAILURE_PATH", path), \
                 mock.patch.object(release, "HK_REVIEWED_FAILURE_SHA256", digest):
                with self.assertRaisesRegex(common.OperatorError, "strict JSON"):
                    release.verify_reviewed_hk_failure(path, digest)

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

    def test_hk_runtime_contract_hashes_are_exact_and_well_formed(self):
        release.validate_hk_runtime_contract()
        self.assertEqual(release.HK_RUNTIME_FINGERPRINTS, {
            release.JOB_IDS[0]:
                "f7f96fa4144c00f127e7b4f2b1dbc920f2a3902729ce4da77a0dbe76f2ba852e",
            release.JOB_IDS[1]:
                "60dac1dd63668b5a60724dc6c92b475fdb4ddd1d252ce9104e81749d16142c3c",
        })
        self.assertEqual(release.HK_RUNTIME_FILE_SHA256, {
            release.JOB_IDS[0]:
                "fe204d9ce3931cb9c55d4328e26b99f9afa235c2453152284e5b20fc178c65f5",
            release.JOB_IDS[1]:
                "c92203d0baf1507d1d37e50252be0a0c8a341b583a60ae37e61540c15397512c",
        })
        for mapping in (release.HK_RUNTIME_FINGERPRINTS,
                        release.HK_RUNTIME_FILE_SHA256):
            self.assertEqual(set(mapping), set(release.JOB_IDS))
            self.assertTrue(all(len(value) == 64 for value in mapping.values()))

    def test_hk_partial_download_contract_is_exact_and_well_formed(self):
        release.validate_hk_download_contract()
        self.assertTrue(all(
            re.fullmatch(r"[0-9a-f]{64}", item[key])
            for item in release.HK_DOWNLOAD_PARTS
            for key in ("part_sha256", "record_sha256")))
        actual = [(
            item["job_id"], item["episode"], item["part_inode"],
            item["part_size"], item["part_sha256"], item["record_inode"],
            item["record_size"], item["record_sha256"], item["expected_size"])
            for item in release.HK_DOWNLOAD_PARTS]
        self.assertEqual(actual, [
            (release.JOB_IDS[0], "002", 1709371, 8388608,
             "9c5b7b48d41b0e6503f1f9b894e2086b381d94de1f21453910f7bbeea9a754ad",
             1709373, 280,
             "80c110ff1112e06f6fee80815ad855bbb81860c8a2ca69c62545d9fb2a2e923c",
             214348452),
            (release.JOB_IDS[0], "003", 1709227, 319029248,
             "4268d78394d012bef2b09306db6ce2b74e7e9245c5600217cec37d48f8e4be2b",
             1709372, 282,
             "7e5655ae516fad5e3d34102ebf6af532dc6e0a2dfbcf442657aef049ca863276",
             349379561),
            (release.JOB_IDS[0], "004", 1709370, 9437184,
             "24eff40e573e808f6e973d14ff3615c43aa82c9861c4a706a52132051f40f3d1",
             1709377, 280,
             "79522a771097eb96645425925ac9dbad373a237f7539effb9455f59e84d520ee",
             226154892),
            (release.JOB_IDS[0], "005", 1709375, 0,
             "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
             1709376, 274,
             "244a61e9711a2208ebabc6b2e6feb6ab336980641b6af607b41f5fd6845365fc",
             154755915),
            (release.JOB_IDS[1], "002", 4065235, 72089600,
             "f1418a20b25e594d01cae655a6075e2652c442b61ce6613429e84540dc773f18",
             4065270, 279,
             "5e54dee5181ff6a38edd694b779b95747d020f44f033ee934490e7cdbad0bded",
             81370743),
            (release.JOB_IDS[1], "003", 4065239, 4194304,
             "92e83d6ac16ae1b976d7b6fb8fda776566b7ba808d3f836a8d56f62a7a9595da",
             4065418, 278,
             "f6de9ac5a7a0550731e49d3a44b6a0e07208c2677ee70586ae20ea4a611564a4",
             63707705),
            (release.JOB_IDS[1], "004", 4065237, 141819904,
             "26480461441ab4573f43a99a32411b42b45dccc964c68e980a4d83aca9990c83",
             4065238, 282,
             "2a2c5e09c38ea00f1f56023f51e4961c6eeddc306b9c464ea1417766afc72caf",
             163071840),
            (release.JOB_IDS[1], "005", 4065240, 0,
             "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
             4065417, 272,
             "21854d740ce57361256930ab8706faa99b15c318f769d51ac464822df53e0d4f",
             88911492),
        ])

    def test_hk_runtime_accepts_only_two_exact_failed_records_and_redacts_payload(self):
        with hk_runtime_fixture() as fixture:
            result = release.inspect_hk_runtime()
        self.assertEqual(result["active_jobs"], 0)
        self.assertEqual(result["durable_failed_jobs"], 2)
        self.assertEqual([item["job_id"] for item in result["durable_records"]],
                         list(release.JOB_IDS))
        self.assertEqual([item["file_sha256"] for item in result["durable_records"]],
                         [fixture["hashes"][job_id] for job_id in release.JOB_IDS])
        self.assertEqual(
            result["durable_records_sha256"],
            common.sha256_bytes(common.canonical_bytes(result["durable_records"])))
        self.assertEqual(result["part_files"], 8)
        self.assertEqual(result["part_record_files"], 8)
        self.assertEqual(len(result["recoverable_downloads"]), 8)
        self.assertEqual(
            {item["relative_part_path"] for item in result["recoverable_downloads"]},
            {"%s/downloads/%s.mp4.part" % (job_id, episode)
             for job_id in release.JOB_IDS
             for episode in ("002", "003", "004", "005")})
        self.assertEqual(
            {item["relative_record_path"] for item in result["recoverable_downloads"]},
            {"%s/downloads/%s.mp4.part.json" % (job_id, episode)
             for job_id in release.JOB_IDS
             for episode in ("002", "003", "004", "005")})
        self.assertEqual(
            result["recoverable_downloads_sha256"],
            common.sha256_bytes(common.canonical_bytes(result["recoverable_downloads"])))
        self.assertNotIn("_payload", json.dumps(result, sort_keys=True))
        self.assertNotIn("must-not-appear", json.dumps(result, sort_keys=True))
        self.assertNotIn("source_identity", json.dumps(result, sort_keys=True))
        self.assertNotIn("etag", json.dumps(result, sort_keys=True))

    def test_hk_partial_download_record_requires_exact_checkpoint_fields(self):
        identity = (release.JOB_IDS[0], "002")
        cases = (
            {"version": True},
            {"source_identity": 123},
            {"etag": 123},
            {"expected_size": 1},
            {"partial_size": 1},
            {"partial_sha256": "0" * 64},
            {"unexpected": "field"},
        )
        for changes in cases:
            with self.subTest(changes=changes), \
                 hk_runtime_fixture(partial_record_changes={identity: changes}):
                with self.assertRaisesRegex(common.OperatorError,
                                            "approved checkpoint"):
                    release.inspect_hk_runtime()

    def test_hk_partial_download_record_rejects_duplicate_key(self):
        identity = (release.JOB_IDS[0], "002")
        with hk_runtime_fixture() as initial:
            raw = initial["partial_record_paths"][identity].read_bytes()
        duplicate = raw.replace(b"{", b'{"version":1,', 1)
        with hk_runtime_fixture(partial_raw_overrides={identity: duplicate}):
            with self.assertRaisesRegex(common.OperatorError, "strict UTF-8 JSON"):
                release.inspect_hk_runtime()

    def test_hk_partial_download_rejects_extra_missing_or_orphan_pair(self):
        identity = (release.JOB_IDS[0], "002")
        with hk_runtime_fixture() as fixture:
            (fixture["work"] / release.JOB_IDS[0] / "downloads" /
             "999.mp4.part").write_bytes(b"extra")
            with self.assertRaisesRegex(common.OperatorError, "eight approved pairs"):
                release.inspect_hk_runtime()
        with hk_runtime_fixture() as fixture:
            fixture["part_paths"][identity].unlink()
            with self.assertRaisesRegex(common.OperatorError, "eight approved pairs"):
                release.inspect_hk_runtime()
        with hk_runtime_fixture() as fixture:
            fixture["partial_record_paths"][identity].unlink()
            with self.assertRaisesRegex(common.OperatorError, "eight approved pairs"):
                release.inspect_hk_runtime()

    def test_hk_partial_download_rejects_inode_size_or_content_change(self):
        identity = (release.JOB_IDS[0], "002")
        with hk_runtime_fixture() as fixture:
            changed = [dict(item) for item in fixture["download_specs"]]
            changed[0]["part_inode"] += 1
            with mock.patch.object(release, "HK_DOWNLOAD_PARTS", tuple(changed)):
                with self.assertRaisesRegex(common.OperatorError, "inode or size"):
                    release.inspect_hk_runtime()
        with hk_runtime_fixture() as fixture:
            fixture["part_paths"][identity].write_bytes(b"changed")
            with self.assertRaises(common.OperatorError):
                release.inspect_hk_runtime()
        with hk_runtime_fixture() as fixture:
            fixture["partial_record_paths"][identity].write_bytes(b"{}")
            with self.assertRaises(common.OperatorError):
                release.inspect_hk_runtime()

    def test_hk_partial_download_rejects_path_replacement_after_anchored_read(self):
        identity = (release.JOB_IDS[0], "002")
        for key in ("part_paths", "partial_record_paths"):
            with self.subTest(key=key), hk_runtime_fixture() as fixture:
                target = fixture[key][identity]
                original_lstat = os.lstat
                calls = {"target": 0}

                def changed_lstat(path):
                    value = original_lstat(path)
                    if pathlib.Path(path) == target:
                        calls["target"] += 1
                        if calls["target"] == 5:
                            changed = mock.Mock()
                            for name in ("st_dev", "st_ino", "st_size", "st_mtime_ns",
                                         "st_mtime", "st_mode"):
                                setattr(changed, name, getattr(value, name))
                            changed.st_ino = int(value.st_ino) + 1
                            return changed
                    return value

                with mock.patch.object(release.os, "lstat", side_effect=changed_lstat):
                    with self.assertRaises(common.OperatorError):
                        release.inspect_hk_runtime()
                self.assertEqual(calls["target"], 5)

    def test_hk_partial_download_rechecks_path_set_after_all_anchored_reads(self):
        with hk_runtime_fixture() as fixture:
            original = release.inspect_hk_download_checkpoint
            calls = []

            def add_late_extra(item):
                result = original(item)
                calls.append((item["job_id"], item["episode"]))
                if len(calls) == len(release.HK_DOWNLOAD_PARTS):
                    (fixture["public"] / "late.mp4.part").write_bytes(b"unsafe")
                return result

            with mock.patch.object(release, "inspect_hk_download_checkpoint",
                                   side_effect=add_late_extra):
                with self.assertRaisesRegex(common.OperatorError,
                                            "changed while inspecting"):
                    release.inspect_hk_runtime()
            self.assertEqual(calls, [
                (item["job_id"], item["episode"])
                for item in release.HK_DOWNLOAD_PARTS])

    def test_hk_runtime_rechecks_records_and_diagnostics_after_download_hashing(self):
        for target in ("record", "diagnostic"):
            with self.subTest(target=target), hk_runtime_fixture() as fixture:
                original = release.inspect_hk_download_checkpoints

                def change_runtime_after_downloads():
                    result = original()
                    if target == "record":
                        fixture["paths"][release.JOB_IDS[0]].write_bytes(b"changed")
                    else:
                        (fixture["diagnostics"] / "late.json").write_bytes(b"{}")
                    return result

                with mock.patch.object(release, "inspect_hk_download_checkpoints",
                                       side_effect=change_runtime_after_downloads):
                    with self.assertRaises(common.OperatorError):
                        release.inspect_hk_runtime()

    @unittest.skipIf(os.name == "nt", "Windows test user cannot create symlinks")
    def test_hk_partial_download_rejects_symlink_part_or_record(self):
        identity = (release.JOB_IDS[0], "002")
        for key in ("part_paths", "partial_record_paths"):
            with self.subTest(key=key), hk_runtime_fixture() as fixture:
                target = fixture[key][identity]
                real = fixture["base"] / (key + "-real")
                target.rename(real)
                target.symlink_to(real)
                with self.assertRaises(common.OperatorError):
                    release.inspect_hk_runtime()

    def test_hk_runtime_rejects_each_unapproved_durable_state(self):
        job_id = release.JOB_IDS[0]
        cases = (
            {"version": True},
            {"job_id": "0" * 32},
            {"generation": True},
            {"generation": 2},
            {"status": "running"},
            {"stage": "running"},
            {"fingerprint": "0" * 64},
            {"error": {"code": "gpu_process_state_unknown"}},
            {"_children": {"1": {"pid": 1}}},
            {"_launches": {"launch": {}}},
            {"_resource_blocked": True},
            {"_resource_blocked": 0},
            {"_cache_blocked": True},
            {"_cache_blocked": 0},
        )
        for changes in cases:
            with self.subTest(changes=changes), \
                 hk_runtime_fixture({job_id: changes}):
                with self.assertRaisesRegex(common.OperatorError, "approved failed state"):
                    release.inspect_hk_runtime()

    def test_hk_runtime_rejects_duplicate_json_key(self):
        job_id = release.JOB_IDS[0]
        with hk_runtime_fixture() as initial:
            raw = initial["paths"][job_id].read_bytes()
        duplicate = raw.replace(b"{", b'{"version":1,', 1)
        with hk_runtime_fixture(raw_overrides={job_id: duplicate}):
            with self.assertRaisesRegex(common.OperatorError, "strict UTF-8 JSON"):
                release.inspect_hk_runtime()

    def test_hk_runtime_rejects_extra_missing_and_oversize_entries(self):
        with hk_runtime_fixture() as fixture:
            (fixture["jobs"] / "unexpected.tmp").write_bytes(b"unsafe")
            with self.assertRaisesRegex(common.OperatorError, "two approved records"):
                release.inspect_hk_runtime()
        with hk_runtime_fixture() as fixture:
            fixture["paths"][release.JOB_IDS[1]].unlink()
            with self.assertRaisesRegex(common.OperatorError, "two approved records"):
                release.inspect_hk_runtime()
        with hk_runtime_fixture() as fixture:
            target = fixture["paths"][release.JOB_IDS[0]]
            with target.open("wb") as stream:
                stream.truncate(release.HK_RUNTIME_RECORD_MAX_BYTES + 1)
            with mock.patch.object(common, "anchored_file") as anchored:
                with self.assertRaisesRegex(common.OperatorError, "regular JSON"):
                    release.inspect_hk_runtime()
            anchored.assert_not_called()

    @unittest.skipIf(os.name == "nt", "Windows test user cannot create symlinks")
    def test_hk_runtime_rejects_symlink_record(self):
        with hk_runtime_fixture() as fixture:
            target = fixture["paths"][release.JOB_IDS[0]]
            real = fixture["base"] / "record-real.json"
            target.rename(real)
            target.symlink_to(real)
            with self.assertRaisesRegex(common.OperatorError, "regular JSON"):
                release.inspect_hk_runtime()

    def test_hk_runtime_rejects_path_replacement_after_anchored_read(self):
        with hk_runtime_fixture() as fixture:
            target = fixture["paths"][release.JOB_IDS[0]]
            original_lstat = os.lstat
            calls = {"target": 0}

            def changed_final_lstat(path):
                value = original_lstat(path)
                if pathlib.Path(path) == target:
                    calls["target"] += 1
                    if calls["target"] == 4:
                        changed = mock.Mock()
                        for name in ("st_dev", "st_ino", "st_size", "st_mtime_ns",
                                     "st_mtime", "st_mode"):
                            setattr(changed, name, getattr(value, name))
                        changed.st_ino = int(value.st_ino) + 1
                        return changed
                return value

            with mock.patch.object(release.os, "lstat", side_effect=changed_final_lstat):
                with self.assertRaisesRegex(common.OperatorError, "changed while reading"):
                    release.inspect_hk_runtime()
            self.assertEqual(calls["target"], 4)

    def test_hk_runtime_rechecks_directory_after_both_anchored_reads(self):
        with hk_runtime_fixture() as fixture:
            original = release.inspect_hk_runtime_record
            calls = []

            def add_late_extra(path, job_id):
                result = original(path, job_id)
                calls.append(job_id)
                if len(calls) == 2:
                    (fixture["jobs"] / "late.tmp").write_bytes(b"unsafe")
                return result

            with mock.patch.object(release, "inspect_hk_runtime_record",
                                   side_effect=add_late_extra):
                with self.assertRaisesRegex(common.OperatorError,
                                            "changed while inspecting"):
                    release.inspect_hk_runtime()
            self.assertEqual(calls, list(release.JOB_IDS))

    def test_hk_runtime_keeps_diagnostics_and_part_guards(self):
        with hk_runtime_fixture() as fixture:
            (fixture["diagnostics"] / "unexpected.json").write_bytes(b"{}")
            with self.assertRaisesRegex(common.OperatorError, "diagnostics"):
                release.inspect_hk_runtime()
        with hk_runtime_fixture() as fixture:
            (fixture["public"] / "unexpected.part").write_bytes(b"partial")
            with self.assertRaisesRegex(common.OperatorError, "partial"):
                release.inspect_hk_runtime()

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

    def test_cpu_rollback_rejects_active_api_without_8787_listener(self):
        api = common.CPU_TARGET_UNITS[1]
        units = {
            common.CPU_TARGET_UNITS[0]: {"process": None},
            api: {"process": {"pid": 4321}},
        }
        with mock.patch.object(common, "assert_inactive_unit"), \
             mock.patch.object(common, "assert_active_single_process"), \
             mock.patch.object(release, "config_unchanged"), \
             mock.patch.object(release, "listener_owned_by",
                               side_effect=common.OperatorError("listener missing")) as listener:
            with self.assertRaisesRegex(common.OperatorError, "listener missing"):
                release.prove_cpu_rollback(units, units, api)
        listener.assert_called_once_with(8787, 4321)

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

    def _assert_result_publish_fsync_fault_is_committed(self, host_role):
        state = {"committed": False}
        result = {"schema": 1, "result": "deployed", "host_role": host_role}
        payload = json.dumps(result, sort_keys=True, indent=2).encode("utf-8") + b"\n"
        with tempfile.TemporaryDirectory() as directory:
            evidence = pathlib.Path(directory)
            fsync_calls = []

            def fail_after_publish(path):
                fsync_calls.append(pathlib.Path(path))
                if len(fsync_calls) == 2:
                    raise OSError("injected post-rename directory fsync failure")

            with mock.patch.object(common, "atomic_rename_noreplace",
                                   side_effect=portable_noreplace), \
                 mock.patch.object(common, "fsync_directory",
                                   side_effect=fail_after_publish):
                with self.assertRaisesRegex(OSError, "post-rename"):
                    release.publish_authoritative_result(
                        evidence, result, state, host_role)
            result_path = evidence / "result.json"
            self.assertTrue(state["committed"])
            self.assertEqual(result_path.read_bytes(), payload)
            self.assertEqual(common.sha256_file(result_path),
                             hashlib.sha256(payload).hexdigest())
            self.assertFalse(any(evidence.glob("failure*.json")))
            self.assertFalse(any(evidence.glob("post-commit-failure*.json")))
            self.assertFalse(any(evidence.glob(".result-*.tmp")))

    def test_cpu_result_post_rename_fsync_fault_is_authoritative(self):
        self._assert_result_publish_fsync_fault_is_committed("cpu")

    def test_hk_result_post_rename_fsync_fault_is_authoritative(self):
        self._assert_result_publish_fsync_fault_is_committed("hk")

    def test_result_pre_rename_failure_remains_uncommitted(self):
        state = {"committed": False}
        result = {"schema": 1, "result": "deployed", "host_role": "cpu"}
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(common, "atomic_rename_noreplace",
                               side_effect=OSError("injected pre-rename failure")):
            evidence = pathlib.Path(directory)
            with self.assertRaisesRegex(OSError, "pre-rename"):
                release.publish_authoritative_result(evidence, result, state, "cpu")
            self.assertFalse(state["committed"])
            self.assertFalse((evidence / "result.json").exists())
            temporaries = list(evidence.glob(".result-*.tmp"))
            self.assertEqual(len(temporaries), 1)
            self.assertEqual(json.loads(temporaries[0].read_text()), result)

    def test_cpu_result_failure_keeps_rollback_temporaries(self):
        state = {"committed": False}
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(release, "publish_authoritative_result",
                               side_effect=OSError("injected pre-publish failure")), \
             mock.patch.object(release, "cleanup_cpu_temporaries") as cleanup:
            with self.assertRaisesRegex(OSError, "pre-publish failure"):
                release.persist_cpu_result_and_cleanup(
                    pathlib.Path(directory), {"result": "deployed"}, [], state)
        self.assertFalse(state["committed"])
        cleanup.assert_not_called()

    def test_cpu_cleanup_failure_occurs_only_after_durable_commit(self):
        state = {"committed": False}
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(common, "atomic_rename_noreplace",
                               side_effect=portable_noreplace), \
             mock.patch.object(release, "cleanup_cpu_temporaries",
                               side_effect=OSError("injected cleanup failure")):
            with self.assertRaisesRegex(OSError, "cleanup failure"):
                release.persist_cpu_result_and_cleanup(
                    pathlib.Path(directory), {"result": "deployed"}, [], state)
        self.assertTrue(state["committed"])

    def test_cpu_apply_never_rolls_back_after_result_commit(self):
        contract = release.role_contract("cpu")
        active_api = {"process": {"pid": 123}}
        units = {
            common.CPU_TARGET_UNITS[0]: {},
            common.CPU_TARGET_UNITS[1]: active_api,
        }

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
                (common, "assert_active_single_process"),
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
            failure_writer = stack.enter_context(mock.patch.object(
                common, "write_exclusive_json", return_value="a" * 64))
            with self.assertRaisesRegex(common.OperatorError, "HIGH RISK"):
                release.apply_cpu(self.exact_args("cpu"), contract,
                                  {"unit_snapshot": units, "mode": "apply"})
        failure_writer.assert_not_called()
        self.assertEqual(systemctl.call_args_list, [
            mock.call("stop", common.CPU_TARGET_UNITS[1]),
            mock.call("start", common.CPU_TARGET_UNITS[1]),
        ])

    def test_cpu_pre_publish_result_failure_rolls_back_complete(self):
        contract = release.role_contract("cpu")
        api = common.CPU_TARGET_UNITS[1]
        active_api = {"process": {"pid": 123}}
        units = {common.CPU_TARGET_UNITS[0]: {}, api: active_api}
        after_publish_attempt = {"value": False}
        failure_paths = []

        def pre_publish_fail(evidence, result, swaps, state):
            self.assertFalse(state["committed"])
            after_publish_attempt["value"] = True
            raise OSError("injected pre-rename result failure")

        def reviewed_hash(path):
            value = str(path).replace("\\", "/")
            mapping = (common.CPU_OLD_FILES if after_publish_attempt["value"]
                       else common.CPU_NEW_FILES)
            for relative, expected in mapping.items():
                if value.endswith(relative):
                    return expected
            raise AssertionError("unexpected hash path: %s" % path)

        def write(path, value):
            failure_paths.append(pathlib.Path(path))
            return "a" * 64

        with contextlib.ExitStack() as stack:
            for owner, name in (
                    (common, "create_private_ancestry"), (release, "phase"),
                    (common, "assert_no_media_processes"),
                    (common, "assert_no_established_ports"),
                    (release, "inspect_cpu_database"),
                    (common, "assert_inactive_unit"),
                    (common, "assert_active_single_process"),
                    (release, "config_unchanged"),
                    (release, "create_cpu_swap"),
                    (release, "compile_cpu_files"),
                    (release, "prove_cpu_rollback")):
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
                release, "persist_cpu_result_and_cleanup",
                side_effect=pre_publish_fail))
            stack.enter_context(mock.patch.object(
                common, "unit_identity",
                return_value={"systemd": {"ActiveState": "active"}}))
            stack.enter_context(mock.patch.object(
                common, "write_exclusive_json", side_effect=write))
            with self.assertRaisesRegex(OSError, "pre-rename result failure"):
                release.apply_cpu(self.exact_args("cpu"), contract,
                                  {"unit_snapshot": units, "mode": "apply"})
        self.assertEqual(systemctl.call_args_list, [
            mock.call("stop", api), mock.call("start", api),
            mock.call("stop", api), mock.call("start", api),
        ])
        self.assertEqual([path.name for path in failure_paths], ["failure.json"])

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
    def test_hk_retry_reuses_exact_temporary_link_and_rollback_restores_it(self):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            releases = base / "releases"
            evidence = base / "retry-evidence"
            releases.mkdir()
            evidence.mkdir()
            old = releases / common.OLD_SHA
            new = releases / common.NEW_SHA
            old.mkdir()
            new.mkdir()
            current = base / "current"
            temporary = base / (".current-%s-%s" %
                                (common.RUN_ID, common.NEW_SHA[:12]))
            os.symlink(str(old), str(current), target_is_directory=True)
            os.symlink(str(new), str(temporary), target_is_directory=True)
            with mock.patch.object(release, "HK_RELEASES", releases), \
                 mock.patch.object(release, "HK_CURRENT", current), \
                 mock.patch.object(common, "HK_BASE", base), \
                 mock.patch.object(common, "atomic_rename_exchange",
                                   side_effect=portable_exchange), \
                 mock.patch.object(common, "atomic_rename_noreplace",
                                   side_effect=portable_noreplace), \
                 mock.patch.object(common, "fsync_directory"):
                anchor = release.inspect_hk_retry_link()
                original_inode = anchor["stat"]["inode"]
                record = {}
                release.switch_hk_current(record, anchor)
                self.assertTrue(record["reused_existing_temporary"])
                self.assertEqual(os.path.realpath(str(current)), str(new))
                self.assertEqual(os.lstat(str(current)).st_ino, original_inode)
                release.retain_hk_old_link(record, evidence)
                self.assertFalse(temporary.exists() or temporary.is_symlink())
                release.restore_hk_current(record)
                self.assertEqual(os.path.realpath(str(current)), str(old))
                self.assertEqual(os.path.realpath(str(temporary)), str(new))
                self.assertEqual(os.lstat(str(temporary)).st_ino, original_inode)
                self.assertEqual(release.assert_hk_retry_link(anchor), anchor)

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

    def test_target_restart_bound_models_explicit_counter_reset(self):
        unit = common.HK_TARGET_UNITS[0]

        def item(restarts, fragment_sha="a" * 64, generation=0):
            pid = 101 + generation
            return {
                "unit": unit,
                "systemd": {"NRestarts": str(restarts),
                            "UnitFileState": "enabled", "Restart": "on-failure",
                            "ActiveState": "active", "SubState": "running",
                            "ControlPID": "0", "MainPID": str(pid),
                            "ExecMainStartTimestampMonotonic": str(1000 + generation),
                            "ActiveEnterTimestampMonotonic": str(900 + generation)},
                "fragment": {"path": "/etc/systemd/system/worker.service",
                             "sha256": fragment_sha},
                "dropins": [],
                "process": {"pid": pid, "startticks": 2000 + generation,
                            "children": []},
                "cgroup": {"pids": [pid]},
            }

        for baseline, start, final in (
                (1, 0, 0), (1, 0, 1), (1, 1, 1),
                (0, 0, 0), (0, 0, 1), (0, 1, 1)):
            result = release.target_restart_bound(
                {unit: item(baseline)}, {unit: item(start)},
                {unit: item(final, generation=1 if final > start else 0)}, unit)
            self.assertEqual(
                (result["baseline"], result["start"], result["final"]),
                (baseline, start, final))
            self.assertEqual(result["counter_reset_possible"], baseline > 0)
            self.assertEqual(result["automatic_restarts_after_start_anchor"],
                             final - start)
            self.assertEqual(result["allowed_final_min"], start)
            self.assertEqual(result["allowed_final_max"], 1)
            self.assertEqual(result["automatic_restart_limit"], 1)
        for baseline, start, final in (
                (1, 0, 2), (1, 1, 2), (0, 0, 2), (1, 2, 2), (1, 1, 0)):
            with self.assertRaisesRegex(common.OperatorError, "maintenance window"):
                release.target_restart_bound(
                    {unit: item(baseline)}, {unit: item(start)},
                    {unit: item(final, generation=1 if final > start else 0)}, unit)
        with self.assertRaisesRegex(common.OperatorError, "definition changed"):
            release.target_restart_bound(
                {unit: item(1)}, {unit: item(0, fragment_sha="b" * 64)},
                {unit: item(0)}, unit)
        with self.assertRaisesRegex(common.OperatorError, "definition changed"):
            release.target_restart_bound(
                {unit: item(1)}, {unit: item(0)},
                {unit: item(1, fragment_sha="b" * 64)}, unit)

    def test_restart_identity_drift_is_rejected_for_every_target(self):
        targets = (common.CPU_TARGET_UNITS[1],) + common.HK_TARGET_UNITS

        def item(unit, restarts, pid=101, startticks=201,
                 exec_start=301, active_enter=401):
            return {
                "unit": unit,
                "systemd": {
                    "NRestarts": str(restarts), "UnitFileState": "enabled",
                    "Restart": "on-failure", "ActiveState": "active",
                    "SubState": "running", "ControlPID": "0",
                    "MainPID": str(pid),
                    "ExecMainStartTimestampMonotonic": str(exec_start),
                    "ActiveEnterTimestampMonotonic": str(active_enter),
                },
                "fragment": {"path": "/etc/" + unit, "sha256": "a" * 64},
                "dropins": [],
                "process": {"pid": pid, "startticks": startticks, "children": []},
                "cgroup": {"pids": [pid]},
            }

        for unit in targets:
            baseline = {unit: item(unit, 1)}
            start = {unit: item(unit, 0)}
            manual_restart_same_counter = {
                unit: item(unit, 0, pid=102, startticks=202,
                           exec_start=302, active_enter=402),
            }
            with self.assertRaisesRegex(common.OperatorError, "without an observed"):
                release.target_restart_bound(
                    baseline, start, manual_restart_same_counter, unit)
            unchanged_identity_with_increment = {unit: item(unit, 1)}
            with self.assertRaisesRegex(common.OperatorError, "not newer"):
                release.target_restart_bound(
                    baseline, start, unchanged_identity_with_increment, unit)
            active_enter_regressed = {
                unit: item(unit, 1, pid=102, startticks=202,
                           exec_start=302, active_enter=400),
            }
            with self.assertRaisesRegex(common.OperatorError, "not newer"):
                release.target_restart_bound(
                    baseline, start, active_enter_regressed, unit)

    def test_runtime_checkpoint_identity_requires_exact_summary(self):
        runtime = {
            "durable_records_sha256": "a" * 64,
            "recoverable_downloads_sha256": "b" * 64,
            "durable_failed_jobs": len(release.JOB_IDS),
            "part_files": len(release.HK_DOWNLOAD_PARTS),
            "part_record_files": len(release.HK_DOWNLOAD_PARTS),
            "durable_records": [{"job_id": release.JOB_IDS[0]}],
        }
        identity = release.assert_hk_runtime_unchanged(runtime, dict(runtime))
        self.assertEqual(identity["durable_failed_jobs"], len(release.JOB_IDS))
        changed = dict(runtime)
        changed["durable_records"] = [{"job_id": release.JOB_IDS[1]}]
        with self.assertRaisesRegex(common.OperatorError, "checkpoint changed"):
            release.assert_hk_runtime_unchanged(runtime, changed)

    def test_tunnel_window_runtime_drift_makes_rollback_high_risk(self):
        worker_unit, tunnel_unit = common.HK_TARGET_UNITS
        release_path = release.HK_RELEASES / common.NEW_SHA

        def unit_item(name, active=False, restarts=0, cwd=None):
            process = ({"pid": 100, "startticks": 200, "children": [],
                        "cwd": str(cwd or release_path)} if active else None)
            return {
                "unit": name,
                "systemd": {
                    "ActiveState": "active" if active else "inactive",
                    "SubState": "running" if active else "dead",
                    "MainPID": "100" if active else "0", "ControlPID": "0",
                    "NRestarts": str(restarts), "UnitFileState": "enabled",
                    "Restart": "on-failure",
                    "ExecMainStartTimestampMonotonic": "300" if active else "0",
                    "ActiveEnterTimestampMonotonic": "250" if active else "0",
                },
                "fragment": {"path": "/etc/systemd/system/" + name,
                             "sha256": "a" * 64},
                "dropins": [], "process": process,
                "cgroup": {"pids": [100] if active else []},
            }

        baseline = {
            worker_unit: unit_item(worker_unit, restarts=1),
            tunnel_unit: unit_item(tunnel_unit, restarts=1),
        }
        for index, unit in enumerate(common.HK_PROTECTED_UNITS, 1):
            item = unit_item(unit, active=True, restarts=0)
            item["process"]["pid"] = 1000 + index
            item["cgroup"]["pids"] = [1000 + index]
            baseline[unit] = item
        worker_after = dict(baseline)
        worker_after[worker_unit] = unit_item(worker_unit, active=True, restarts=0)
        inactive_targets = {
            worker_unit: unit_item(worker_unit),
            tunnel_unit: unit_item(tunnel_unit),
        }
        protected = {unit: baseline[unit] for unit in common.HK_PROTECTED_UNITS}
        tunnel_start = {
            unit: (unit_item(unit, active=True, restarts=0)
                   if unit in common.HK_TARGET_UNITS else baseline[unit])
            for unit in common.HK_TARGET_UNITS + common.HK_PROTECTED_UNITS
        }
        runtime = {
            "durable_records_sha256": "a" * 64,
            "recoverable_downloads_sha256": "b" * 64,
            "durable_failed_jobs": len(release.JOB_IDS),
            "part_files": len(release.HK_DOWNLOAD_PARTS),
            "part_record_files": len(release.HK_DOWNLOAD_PARTS),
            "durable_records": [{"job_id": value} for value in release.JOB_IDS],
        }
        drifted = dict(runtime)
        drifted["durable_records_sha256"] = "c" * 64
        reviewed = {"sha256": release.HK_REVIEWED_FAILURE_SHA256}
        existing = {"commit": common.NEW_SHA, "tree": "d" * 40}
        retry_link = {"path": str(release.hk_current_temporary_path())}
        before = {
            "unit_snapshot": baseline, "mode": "apply", "runtime": runtime,
            "source": {"tree": "d" * 40}, "reviewed_failure": reviewed,
            "existing_release": existing, "existing_retry_link": retry_link,
        }
        args = self.exact_args("hk")
        args.reviewed_failure_resume = True
        args.reviewed_failure_path = str(release.HK_REVIEWED_FAILURE_PATH)
        args.reviewed_failure_sha256 = release.HK_REVIEWED_FAILURE_SHA256
        args.retry_id = release.HK_RETRY_ID
        args.apply = True
        contract = release.validate_cli(args)
        failures = []
        snapshot_scopes = []

        def snapshot(units):
            units = tuple(units)
            snapshot_scopes.append(units)
            if units == common.HK_PROTECTED_UNITS:
                return protected
            if units == (worker_unit,) + common.HK_PROTECTED_UNITS:
                return {unit: worker_after[unit] for unit in units}
            if units == common.HK_TARGET_UNITS + common.HK_PROTECTED_UNITS:
                return tunnel_start
            if units == common.HK_TARGET_UNITS:
                return inactive_targets
            raise AssertionError("unexpected snapshot scope: %r" % (units,))

        def switch(record, anchor):
            record.update({"exchange_complete": True,
                           "reused_existing_temporary": True})

        def restore(record):
            record["exchange_complete"] = False

        def write(path, value):
            if pathlib.Path(path).name == "failure.json":
                self.assertEqual(pathlib.Path(path).parent, contract["evidence"])
                self.assertNotEqual(pathlib.Path(path).parent,
                                    release.HK_REVIEWED_FAILURE_PATH.parent)
                failures.append(value)
            return "e" * 64

        def realpath(path):
            if str(path) in (str(release_path), str(release.HK_CURRENT)):
                return str(release_path)
            return str(path)

        with contextlib.ExitStack() as stack:
            for owner, name in (
                    (common, "create_private_ancestry"), (release, "phase"),
                    (common, "assert_no_media_processes"),
                    (common, "assert_no_established_ports"),
                    (common, "assert_protected_units"),
                    (common, "assert_inactive_unit"),
                    (release, "assert_hk_current_release")):
                stack.enter_context(mock.patch.object(owner, name))
            stack.enter_context(mock.patch.object(
                release, "inspect_hk_runtime",
                side_effect=[runtime, runtime, runtime, runtime, runtime,
                             drifted, drifted]))
            stack.enter_context(mock.patch.object(
                release, "verify_reviewed_hk_failure", return_value=reviewed))
            stack.enter_context(mock.patch.object(
                release, "verify_existing_hk_release", return_value=existing))
            stack.enter_context(mock.patch.object(
                release, "assert_hk_retry_link", return_value=retry_link))
            stack.enter_context(mock.patch.object(
                common, "snapshot_units", side_effect=snapshot))
            stack.enter_context(mock.patch.object(
                common, "protected_signature", return_value={}))
            stack.enter_context(mock.patch.object(release, "switch_hk_current",
                                                   side_effect=switch))
            stack.enter_context(mock.patch.object(release, "restore_hk_current",
                                                   side_effect=restore))
            stack.enter_context(mock.patch.object(release, "systemctl"))
            stack.enter_context(mock.patch.object(release, "wait_unit"))
            stack.enter_context(mock.patch.object(
                common, "unit_identity",
                side_effect=lambda unit: unit_item(unit, active=True)))
            stack.enter_context(mock.patch.object(
                common, "exact_health", return_value={"ok": True}))
            stack.enter_context(mock.patch.object(
                release, "listener_owned_by", return_value={"pid": 100}))
            stack.enter_context(mock.patch.object(
                release.os.path, "realpath", side_effect=realpath))
            stack.enter_context(mock.patch.object(
                common, "write_exclusive_json", side_effect=write))
            with self.assertRaisesRegex(common.OperatorError, "HIGH RISK"):
                release.apply_hk(args, contract, before)
        self.assertEqual(len(failures), 1)
        self.assertFalse(failures[0]["rollback"]["complete"])
        self.assertIn("prove-runtime-unchanged",
                      [item["stage"] for item in failures[0]["rollback"]["errors"]])
        self.assertIn(common.HK_TARGET_UNITS + common.HK_PROTECTED_UNITS,
                      snapshot_scopes)

    def test_hk_pre_publish_result_failure_rolls_back_complete(self):
        worker_unit, tunnel_unit = common.HK_TARGET_UNITS
        release_path = release.HK_RELEASES / common.NEW_SHA
        runtime = {
            "durable_records_sha256": "a" * 64,
            "recoverable_downloads_sha256": "b" * 64,
            "durable_failed_jobs": len(release.JOB_IDS),
            "part_files": len(release.HK_DOWNLOAD_PARTS),
            "part_record_files": len(release.HK_DOWNLOAD_PARTS),
            "durable_records": [{"job_id": value} for value in release.JOB_IDS],
        }

        def item(name, active=False):
            return {
                "unit": name,
                "systemd": {"ActiveState": "active" if active else "inactive",
                            "SubState": "running" if active else "dead",
                            "MainPID": "100" if active else "0", "ControlPID": "0",
                            "NRestarts": "0", "UnitFileState": "enabled",
                            "Restart": "on-failure"},
                "fragment": {"path": "/etc/" + name, "sha256": "a" * 64},
                "dropins": [],
                "process": ({"pid": 100, "cwd": str(release_path),
                             "startticks": 1, "children": []} if active else None),
                "cgroup": {"pids": [100] if active else []},
            }

        baseline = {unit: item(unit) for unit in common.HK_TARGET_UNITS}
        baseline.update({unit: item(unit, True) for unit in common.HK_PROTECTED_UNITS})
        reviewed = {"sha256": release.HK_REVIEWED_FAILURE_SHA256}
        existing = {"commit": common.NEW_SHA, "tree": "d" * 40}
        retry_link = {"path": str(release.hk_current_temporary_path())}
        before = {
            "unit_snapshot": baseline, "mode": "apply", "runtime": runtime,
            "source": {"tree": "d" * 40}, "reviewed_failure": reviewed,
            "existing_release": existing, "existing_retry_link": retry_link,
        }
        args = self.exact_args("hk")
        args.reviewed_failure_resume = True
        args.reviewed_failure_path = str(release.HK_REVIEWED_FAILURE_PATH)
        args.reviewed_failure_sha256 = release.HK_REVIEWED_FAILURE_SHA256
        args.retry_id = release.HK_RETRY_ID
        args.apply = True
        contract = release.validate_cli(args)
        failures = []

        def snapshot(units):
            units = tuple(units)
            if units == common.HK_PROTECTED_UNITS:
                return {unit: baseline[unit] for unit in units}
            if units == (worker_unit,) + common.HK_PROTECTED_UNITS:
                return {unit: (item(unit, True) if unit == worker_unit else baseline[unit])
                        for unit in units}
            if units == common.HK_TARGET_UNITS + common.HK_PROTECTED_UNITS:
                return {unit: (item(unit, True) if unit in common.HK_TARGET_UNITS
                               else baseline[unit]) for unit in units}
            if units == common.HK_TARGET_UNITS:
                return {unit: item(unit) for unit in units}
            raise AssertionError("unexpected snapshot scope: %r" % (units,))

        def switch(record, anchor):
            record.update({"exchange_complete": True,
                           "reused_existing_temporary": True})

        def restore(record):
            record["exchange_complete"] = False

        def write(path, value):
            if pathlib.Path(path).name == "failure.json":
                self.assertEqual(pathlib.Path(path).parent, contract["evidence"])
                self.assertNotEqual(pathlib.Path(path).parent,
                                    release.HK_REVIEWED_FAILURE_PATH.parent)
                failures.append(value)
            return "e" * 64

        with contextlib.ExitStack() as stack:
            for owner, name in (
                    (common, "create_private_ancestry"), (release, "phase"),
                    (common, "assert_no_media_processes"),
                    (common, "assert_no_established_ports"),
                    (common, "assert_protected_units"),
                    (common, "assert_inactive_unit"),
                    (release, "assert_hk_current_release")):
                stack.enter_context(mock.patch.object(owner, name))
            stack.enter_context(mock.patch.object(
                release, "inspect_hk_runtime", return_value=runtime))
            stack.enter_context(mock.patch.object(
                release, "verify_reviewed_hk_failure", return_value=reviewed))
            stack.enter_context(mock.patch.object(
                release, "verify_existing_hk_release", return_value=existing))
            stack.enter_context(mock.patch.object(
                release, "assert_hk_retry_link", return_value=retry_link))
            stack.enter_context(mock.patch.object(
                common, "snapshot_units", side_effect=snapshot))
            stack.enter_context(mock.patch.object(
                common, "protected_signature", return_value={}))
            stack.enter_context(mock.patch.object(
                release, "target_restart_bound", return_value={"allowed": True}))
            stack.enter_context(mock.patch.object(release, "switch_hk_current",
                                                   side_effect=switch))
            stack.enter_context(mock.patch.object(release, "restore_hk_current",
                                                   side_effect=restore))
            stack.enter_context(mock.patch.object(release, "systemctl"))
            stack.enter_context(mock.patch.object(release, "wait_unit"))
            stack.enter_context(mock.patch.object(
                common, "unit_identity", side_effect=lambda unit: item(unit, True)))
            stack.enter_context(mock.patch.object(
                common, "exact_health",
                return_value={"ok": True}))
            stack.enter_context(mock.patch.object(
                release, "listener_owned_by", return_value={"pid": 100}))
            stack.enter_context(mock.patch.object(
                release.os.path, "realpath",
                side_effect=lambda path: (str(release_path)
                                          if str(path) in (str(release_path),
                                                           str(release.HK_CURRENT))
                                          else str(path))))
            stack.enter_context(mock.patch.object(release, "retain_hk_old_link"))
            stack.enter_context(mock.patch.object(
                release, "publish_authoritative_result",
                side_effect=OSError("injected pre-rename result failure")))
            stack.enter_context(mock.patch.object(
                common, "write_exclusive_json", side_effect=write))
            with self.assertRaisesRegex(OSError, "pre-rename result failure"):
                release.apply_hk(args, contract, before)
        self.assertEqual(len(failures), 1)
        self.assertTrue(failures[0]["rollback"]["complete"])
        self.assertEqual(failures[0]["rollback"]["errors"], [])
        self.assertEqual(failures[0]["runtime_rollback_proof"]["durable_failed_jobs"],
                         len(release.JOB_IDS))

    def test_hk_authoritative_result_failure_never_rolls_back_or_writes_failure(self):
        worker_unit, tunnel_unit = common.HK_TARGET_UNITS
        release_path = release.HK_RELEASES / common.NEW_SHA
        runtime = {
            "durable_records_sha256": "a" * 64,
            "recoverable_downloads_sha256": "b" * 64,
            "durable_failed_jobs": len(release.JOB_IDS),
            "part_files": len(release.HK_DOWNLOAD_PARTS),
            "part_record_files": len(release.HK_DOWNLOAD_PARTS),
            "durable_records": [{"job_id": value} for value in release.JOB_IDS],
        }

        def item(name, active=False):
            pid = 100 if name == worker_unit else 101
            return {
                "unit": name,
                "systemd": {
                    "ActiveState": "active" if active else "inactive",
                    "SubState": "running" if active else "dead",
                    "MainPID": str(pid) if active else "0", "ControlPID": "0",
                    "NRestarts": "0", "UnitFileState": "enabled",
                    "Restart": "on-failure",
                    "ExecMainStartTimestampMonotonic": "300" if active else "0",
                    "ActiveEnterTimestampMonotonic": "250" if active else "0",
                },
                "fragment": {"path": "/etc/" + name, "sha256": "a" * 64},
                "dropins": [],
                "process": ({"pid": pid, "cwd": str(release_path),
                             "startticks": 200 + pid, "children": []}
                            if active else None),
                "cgroup": {"pids": [pid] if active else []},
            }

        baseline = {unit: item(unit) for unit in common.HK_TARGET_UNITS}
        baseline.update({unit: item(unit, True) for unit in common.HK_PROTECTED_UNITS})
        protected = {unit: baseline[unit] for unit in common.HK_PROTECTED_UNITS}
        reviewed = {"sha256": release.HK_REVIEWED_FAILURE_SHA256}
        existing = {"commit": common.NEW_SHA, "tree": "d" * 40}
        retry_link = {"path": str(release.hk_current_temporary_path())}
        before = {
            "unit_snapshot": baseline, "mode": "apply", "runtime": runtime,
            "source": {"tree": "d" * 40}, "reviewed_failure": reviewed,
            "existing_release": existing, "existing_retry_link": retry_link,
        }
        args = self.exact_args("hk")
        args.reviewed_failure_resume = True
        args.reviewed_failure_path = str(release.HK_REVIEWED_FAILURE_PATH)
        args.reviewed_failure_sha256 = release.HK_REVIEWED_FAILURE_SHA256
        args.retry_id = release.HK_RETRY_ID
        args.apply = True
        contract = release.validate_cli(args)

        def snapshot(units):
            units = tuple(units)
            if units == common.HK_PROTECTED_UNITS:
                return protected
            return {unit: (item(unit, True) if unit in common.HK_TARGET_UNITS
                           else protected[unit]) for unit in units}

        def switch(record, anchor):
            record.update({"exchange_complete": True,
                           "reused_existing_temporary": True})

        def publish(evidence, result, state, host_role):
            self.assertEqual(host_role, "hk")
            state.update({"committed": True,
                          "result": str(pathlib.Path(evidence) / "result.json"),
                          "result_sha256": "e" * 64})
            raise OSError("injected post-rename directory fsync failure")

        with contextlib.ExitStack() as stack:
            for owner, name in (
                    (common, "create_private_ancestry"), (release, "phase"),
                    (common, "assert_no_media_processes"),
                    (common, "assert_no_established_ports"),
                    (common, "assert_protected_units")):
                stack.enter_context(mock.patch.object(owner, name))
            stack.enter_context(mock.patch.object(
                release, "inspect_hk_runtime", return_value=runtime))
            stack.enter_context(mock.patch.object(
                release, "verify_reviewed_hk_failure", return_value=reviewed))
            stack.enter_context(mock.patch.object(
                release, "verify_existing_hk_release", return_value=existing))
            stack.enter_context(mock.patch.object(
                release, "assert_hk_retry_link", return_value=retry_link))
            stack.enter_context(mock.patch.object(
                common, "snapshot_units", side_effect=snapshot))
            stack.enter_context(mock.patch.object(
                common, "protected_signature", return_value={}))
            stack.enter_context(mock.patch.object(
                release, "target_restart_bound", return_value={"allowed": True}))
            stack.enter_context(mock.patch.object(
                release, "switch_hk_current", side_effect=switch))
            restore = stack.enter_context(mock.patch.object(release, "restore_hk_current"))
            stack.enter_context(mock.patch.object(release, "retain_hk_old_link"))
            systemctl = stack.enter_context(mock.patch.object(release, "systemctl"))
            stack.enter_context(mock.patch.object(release, "wait_unit"))
            stack.enter_context(mock.patch.object(
                common, "exact_health", return_value={"ok": True}))
            stack.enter_context(mock.patch.object(
                release, "listener_owned_by", return_value={"pid": 100}))
            stack.enter_context(mock.patch.object(
                release.os.path, "realpath",
                side_effect=lambda path: (str(release_path)
                                          if str(path) in (str(release_path),
                                                           str(release.HK_CURRENT))
                                          else str(path))))
            stack.enter_context(mock.patch.object(
                release, "publish_authoritative_result", side_effect=publish))
            failure_writer = stack.enter_context(mock.patch.object(
                common, "write_exclusive_json", return_value="f" * 64))
            unit_identity = stack.enter_context(mock.patch.object(common, "unit_identity"))
            with self.assertRaisesRegex(common.OperatorError, "HIGH RISK"):
                release.apply_hk(args, contract, before)
        self.assertEqual(systemctl.call_args_list, [
            mock.call("start", worker_unit), mock.call("start", tunnel_unit),
        ])
        restore.assert_not_called()
        failure_writer.assert_not_called()
        unit_identity.assert_not_called()

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
        for action in ("start", "stop"):
            with self.assertRaises(common.OperatorError):
                release.systemctl(action, common.HK_PROTECTED_UNITS[0])

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
