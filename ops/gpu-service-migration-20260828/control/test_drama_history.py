import importlib.util
import json
import os
import pathlib
import stat
import tempfile
import types
import unittest
from unittest import mock


def load_module():
    path = pathlib.Path(__file__).with_name("drama_history.py")
    spec = importlib.util.spec_from_file_location("drama_history", str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


history = load_module()


class PortableTreeIO(object):
    """Path-backed test double; production always uses PosixTreeIO/openat."""

    def open_root(self, path):
        return ("directory", pathlib.Path(path))

    def listdir(self, handle):
        return os.listdir(str(handle[1]))

    def stat_child(self, handle, name):
        return os.lstat(str(handle[1] / name))

    def open_directory_child(self, handle, name):
        path = handle[1] / name
        info = os.lstat(str(path))
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise RuntimeError("test directory is not safe")
        return ("directory", path)

    def open_file_child(self, handle, name):
        path = handle[1] / name
        info = os.lstat(str(path))
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise RuntimeError("test file is not safe")
        descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_BINARY", 0))
        return ("file", descriptor)

    def fstat(self, handle):
        if handle[0] == "directory":
            return os.lstat(str(handle[1]))
        return os.fstat(handle[1])

    def mount_id(self, handle):
        return 1

    def read(self, handle, size):
        return os.read(handle[1], size)

    def close(self, handle):
        if handle[0] == "file":
            os.close(handle[1])


class DramaHistoryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.folder.name)
        self.data = self.root / "data"
        self.data.mkdir()
        self.base = self.data / "migrations" / history.RUN_ID / "drama-history"
        self.source_one = self.root / "root-drama_material_jobs"
        self.source_two = self.root / "usr-share-nginx-html-drama-materials"
        self.source_one.mkdir()
        self.source_two.mkdir()
        (self.source_one / "job.json").write_bytes(b"fixture-job")
        nested = self.source_two / "nested"
        nested.mkdir()
        (nested / "video.mp4").write_bytes(b"fixture-video")
        self.constants = mock.patch.multiple(
            history, DATA_ROOT=self.data, BASE=self.base,
            ARCHIVE=self.base / "archive",
            SOURCE_SPECS=(("root-drama_material_jobs", self.source_one),
                          ("usr-share-nginx-html-drama-materials", self.source_two)))
        self.constants.start()
        self.mount = mock.patch.object(history.os.path, "ismount", return_value=True)
        self.mount.start()
        self.findmnt = mock.patch.object(history, "findmnt_target", return_value=str(self.data))
        self.findmnt.start()
        self.free = mock.patch.object(history, "free_bytes", return_value=200 * 1024 * 1024 * 1024)
        self.free.start()
        self.fsync = mock.patch.object(history, "fsync_directory", return_value=None)
        self.fsync.start()
        self.host = mock.patch.object(history.socket, "gethostname",
                                      return_value=history.EXPECTED_HOST)
        self.host.start()
        self.tree = PortableTreeIO()
        self.tree_patch = mock.patch.object(history, "TREE_IO", self.tree)
        self.tree_patch.start()

    def tearDown(self):
        self.tree_patch.stop()
        self.host.stop()
        self.fsync.stop()
        self.free.stop()
        self.findmnt.stop()
        self.mount.stop()
        self.constants.stop()
        self.folder.cleanup()

    def portable_publish(self, source, destination):
        if os.path.lexists(str(destination)):
            raise RuntimeError("refuse to overwrite the published drama archive")
        os.rename(str(source), str(destination))

    def test_host_is_exactly_bound(self):
        with mock.patch.object(history.socket, "gethostname", return_value="wrong-host"):
            with self.assertRaises(RuntimeError):
                history.assert_host()
        with mock.patch.object(history.socket, "gethostname", return_value=history.EXPECTED_HOST):
            history.assert_host()

    def test_host_requires_root_user_and_group(self):
        with mock.patch.object(history.os, "geteuid", return_value=0, create=True), \
             mock.patch.object(history.os, "getegid", return_value=1000, create=True):
            with self.assertRaises(RuntimeError):
                history.assert_host()
        with mock.patch.object(history.os, "geteuid", return_value=0, create=True), \
             mock.patch.object(history.os, "getegid", return_value=0, create=True):
            history.assert_host()

    def test_storage_guard_requires_real_exact_mount(self):
        with mock.patch.object(history.os.path, "ismount", return_value=False):
            with self.assertRaises(RuntimeError):
                history.storage_guard()
        with mock.patch.object(history, "findmnt_target", return_value="/"):
            with self.assertRaises(RuntimeError):
                history.storage_guard()
        with mock.patch.object(history, "free_bytes", return_value=history.MIN_FREE_BYTES - 1):
            with self.assertRaises(RuntimeError):
                history.storage_guard(require_reserve=True)

    def test_storage_and_source_symlinks_are_rejected(self):
        self.base.mkdir(parents=True)
        real_lstat_kind = history.lstat_kind

        def symlink_base(path):
            if pathlib.Path(path) == self.base:
                return "symlink", os.lstat(str(self.base))
            return real_lstat_kind(path)

        with mock.patch.object(history, "lstat_kind", side_effect=symlink_base):
            with self.assertRaises(RuntimeError):
                history.storage_guard()

        link_info = os.stat_result((stat.S_IFLNK | 0o777, 1, 1, 1, 0, 0, 0, 0, 0, 0))
        with mock.patch.object(self.tree, "listdir", return_value=["link"]), \
             mock.patch.object(self.tree, "stat_child", return_value=link_info):
            with self.assertRaises(RuntimeError):
                history.manifest_tree(self.source_one)

    def test_dry_run_writes_nothing(self):
        result = history.dry_run()
        self.assertEqual(result["mode"], "dry-run")
        self.assertFalse(result["would_write"])
        self.assertFalse(self.base.exists())
        self.assertEqual(result["file_count"], 2)

    def test_root_replacement_during_fd_anchored_scan_is_rejected(self):
        anchor = history.open_root_anchor(self.source_one)
        changed = {"value": False}
        real_listdir = self.tree.listdir
        real_lstat = history.os.lstat

        def list_and_replace(handle):
            result = real_listdir(handle)
            changed["value"] = True
            return result

        def replaced_root(path):
            info = real_lstat(path)
            if pathlib.Path(path) == self.source_one and changed["value"]:
                values = {name: getattr(info, name) for name in dir(info)
                          if name.startswith("st_") and not callable(getattr(info, name))}
                values["st_ino"] = int(info.st_ino) + 1
                return types.SimpleNamespace(**values)
            return info

        try:
            with mock.patch.object(self.tree, "listdir", side_effect=list_and_replace), \
                 mock.patch.object(history.os, "lstat", side_effect=replaced_root):
                with self.assertRaises(RuntimeError):
                    history.manifest_tree_from_anchor(anchor)
        finally:
            history.close_root_anchor(anchor)

    def test_nested_bind_mount_identity_is_rejected(self):
        real_mount_id = self.tree.mount_id

        def changed_mount(handle):
            if handle[0] == "directory" and pathlib.Path(handle[1]).name == "nested":
                return 2
            return real_mount_id(handle)

        with mock.patch.object(self.tree, "mount_id", side_effect=changed_mount):
            with self.assertRaises(RuntimeError):
                history.build_manifest()

    def test_posix_backend_uses_only_fd_relative_child_operations(self):
        backend = history.PosixTreeIO()
        with mock.patch.object(history.os, "O_DIRECTORY", 0x10000, create=True), \
             mock.patch.object(history.os, "O_NOFOLLOW", 0x20000, create=True), \
             mock.patch.object(history.os, "listdir", return_value=["child"]) as listdir, \
             mock.patch.object(history.os, "stat", return_value=object()) as stat_call, \
             mock.patch.object(history.os, "open", return_value=92) as open_call:
            self.assertEqual(backend.listdir(91), ["child"])
            backend.stat_child(91, "child")
            backend.open_directory_child(91, "child")
            backend.open_file_child(91, "child")
        listdir.assert_called_once_with(91)
        stat_call.assert_called_once_with("child", dir_fd=91, follow_symlinks=False)
        first_args, first_kwargs = open_call.call_args_list[0]
        second_args, second_kwargs = open_call.call_args_list[1]
        self.assertEqual(first_args[0], "child")
        self.assertEqual(first_kwargs["dir_fd"], 91)
        self.assertTrue(first_args[1] & 0x20000)
        self.assertEqual(second_args[0], "child")
        self.assertEqual(second_kwargs["dir_fd"], 91)
        self.assertTrue(second_args[1] & 0x20000)

    def test_apply_copies_and_verifies_every_file(self):
        with mock.patch.object(history, "atomic_rename_noreplace", self.portable_publish):
            result = history.apply_archive()
        self.assertEqual(result["result"], "archived")
        self.assertEqual((history.ARCHIVE / history.PAYLOAD_NAME /
                          "root-drama_material_jobs" / "job.json").read_bytes(), b"fixture-job")
        if os.name == "posix":
            self.assertEqual(
                stat.S_IMODE(os.lstat(str(history.ARCHIVE / history.RECEIPT_NAME)).st_mode),
                0o600)
            self.assertEqual(stat.S_IMODE(os.lstat(str(history.ARCHIVE)).st_mode), 0o700)
            archived_file = (history.ARCHIVE / history.PAYLOAD_NAME /
                             "root-drama_material_jobs" / "job.json")
            self.assertEqual(stat.S_IMODE(os.lstat(str(archived_file)).st_mode), 0o600)
        verified = history.verify_archive()
        self.assertTrue(verified["verified"])
        self.assertFalse(verified["writes_performed"])

    def test_source_concurrent_change_retains_private_failure_evidence(self):
        original = history.build_manifest_from_anchors
        calls = {"source": 0}

        def changing_manifest(anchors, private_archive=False):
            value = original(anchors, private_archive)
            source_paths = {pathlib.Path(path) for _, path in history.SOURCE_SPECS}
            anchor_paths = {pathlib.Path(anchor["path"]) for anchor in anchors.values()}
            if anchor_paths == source_paths:
                calls["source"] += 1
                if calls["source"] == 2:
                    changed = dict(value)
                    changed["fingerprint_sha256"] = "f" * 64
                    return changed
            return value

        with mock.patch.object(history, "build_manifest_from_anchors",
                               side_effect=changing_manifest), \
             mock.patch.object(history, "atomic_rename_noreplace", self.portable_publish):
            with self.assertRaises(RuntimeError):
                history.apply_archive()
        staging = list(self.base.glob(".staging-*"))
        self.assertEqual(len(staging), 1)
        evidence = json.loads((staging[0] / "failure.json").read_text())
        self.assertEqual(evidence["stage"], "rescan-live-sources")
        self.assertFalse(history.ARCHIVE.exists())

    def test_copy_manifest_mismatch_is_not_published(self):
        original = history.copy_source

        def corrupt_copy(source, destination, source_manifest):
            original(source, destination, source_manifest)
            files = [item for item in source_manifest["entries"] if item["kind"] == "file"]
            if files:
                target = history.entry_destination_path(destination, files[0]["relative_path"])
                target.write_bytes(target.read_bytes() + b"corrupt")

        with mock.patch.object(history, "copy_source", side_effect=corrupt_copy), \
             mock.patch.object(history, "atomic_rename_noreplace", self.portable_publish):
            with self.assertRaises(RuntimeError):
                history.apply_archive()
        staging = list(self.base.glob(".staging-*"))
        self.assertEqual(len(staging), 1)
        evidence = json.loads((staging[0] / "failure.json").read_text())
        self.assertEqual(evidence["stage"], "verify-copied-payload")
        self.assertFalse(history.ARCHIVE.exists())

    def test_atomic_publication_never_overwrites_existing_archive(self):
        self.base.mkdir(parents=True)
        history.ARCHIVE.mkdir()
        marker = history.ARCHIVE / "keep"
        marker.write_bytes(b"existing")
        with self.assertRaises(RuntimeError):
            history.apply_archive()
        self.assertEqual(marker.read_bytes(), b"existing")
        self.assertEqual(list(self.base.glob(".staging-*")), [])

    def test_precommit_capacity_guard_failure_never_publishes(self):
        real_guard = history.storage_guard
        calls = {"count": 0}

        def fail_final_guard(create=False, require_reserve=False):
            calls["count"] += 1
            if require_reserve:
                raise RuntimeError("fixture reserve changed")
            return real_guard(create=create, require_reserve=require_reserve)

        with mock.patch.object(history, "storage_guard", side_effect=fail_final_guard), \
             mock.patch.object(history, "atomic_rename_noreplace", self.portable_publish):
            with self.assertRaises(RuntimeError):
                history.apply_archive()
        self.assertFalse(history.ARCHIVE.exists())
        staging = list(self.base.glob(".staging-*"))
        self.assertEqual(len(staging), 1)
        evidence = json.loads((staging[0] / "failure.json").read_text())
        self.assertEqual(evidence["stage"], "precommit-storage-guard")

    def test_post_commit_fsync_failure_is_evidenced_and_verify_recovers(self):
        def fail_published_parent(path):
            if history.ARCHIVE.exists():
                raise OSError("fixture parent fsync failure")

        with mock.patch.object(history, "atomic_rename_noreplace", self.portable_publish), \
             mock.patch.object(history, "fsync_directory", side_effect=fail_published_parent):
            with self.assertRaisesRegex(RuntimeError, "archive_published=true"):
                history.apply_archive()
        self.assertTrue(history.ARCHIVE.is_dir())
        self.assertEqual(list(self.base.glob(".staging-*")), [])
        state = json.loads((history.ARCHIVE / history.POST_COMMIT_STATE_NAME).read_text())
        failure = json.loads((history.ARCHIVE / history.POST_COMMIT_FAILURE_NAME).read_text())
        self.assertTrue(state["archive_published_if_present"])
        self.assertTrue(failure["archive_published"])
        self.assertFalse(failure["verified"])
        result = history.verify_archive()
        self.assertTrue(result["verified"])
        self.assertTrue(result["post_commit_failure_present"])
        self.assertTrue(result["recovered_from_post_commit_failure"])
        self.assertFalse(result["writes_performed"])

    def test_partial_post_commit_diagnostic_is_ignored_as_private_temporary(self):
        real_write = history.write_private_bytes_exclusive
        real_unlink = history.os.unlink

        def partial_write(path, encoded):
            if pathlib.Path(path).name.startswith(".post-commit-failure-"):
                pathlib.Path(path).write_bytes(b"{")
                os.chmod(str(path), 0o600)
                raise OSError("fixture partial diagnostic write")
            return real_write(path, encoded)

        def retain_temporary(path):
            if pathlib.Path(path).name.startswith(".post-commit-failure-"):
                raise OSError("fixture temporary unlink failure")
            return real_unlink(path)

        def fail_after_publish(path):
            if history.ARCHIVE.exists():
                raise OSError("fixture post-commit fsync failure")

        with mock.patch.object(history, "atomic_rename_noreplace", self.portable_publish), \
             mock.patch.object(history, "fsync_directory", side_effect=fail_after_publish), \
             mock.patch.object(history, "write_private_bytes_exclusive",
                               side_effect=partial_write), \
             mock.patch.object(history.os, "unlink", side_effect=retain_temporary):
            with self.assertRaisesRegex(RuntimeError, "archive_published=true"):
                history.apply_archive()
        self.assertFalse((history.ARCHIVE / history.POST_COMMIT_FAILURE_NAME).exists())
        temporaries = list(history.ARCHIVE.glob(".post-commit-failure-*.tmp"))
        self.assertEqual(len(temporaries), 1)
        self.assertEqual(temporaries[0].read_bytes(), b"{")
        result = history.verify_archive()
        self.assertTrue(result["verified"])
        self.assertEqual(result["ignored_incomplete_post_commit_temporary_count"], 1)
        self.assertTrue(result["post_commit_temporary_warning"])

    def test_tampered_formal_post_commit_failure_still_blocks_verify(self):
        def fail_published_parent(path):
            if pathlib.Path(path) == history.BASE and history.ARCHIVE.exists():
                raise OSError("fixture parent fsync failure")

        with mock.patch.object(history, "atomic_rename_noreplace", self.portable_publish), \
             mock.patch.object(history, "fsync_directory", side_effect=fail_published_parent):
            with self.assertRaisesRegex(RuntimeError, "archive_published=true"):
                history.apply_archive()
        formal = history.ARCHIVE / history.POST_COMMIT_FAILURE_NAME
        formal.write_bytes(b"{")
        os.chmod(str(formal), 0o600)
        with self.assertRaises(RuntimeError):
            history.verify_archive()

    def test_formal_post_commit_failure_semantic_forgery_is_rejected(self):
        def fail_published_parent(path):
            if pathlib.Path(path) == history.BASE and history.ARCHIVE.exists():
                raise OSError("fixture parent fsync failure")

        with mock.patch.object(history, "atomic_rename_noreplace", self.portable_publish), \
             mock.patch.object(history, "fsync_directory", side_effect=fail_published_parent):
            with self.assertRaisesRegex(RuntimeError, "archive_published=true"):
                history.apply_archive()
        formal = history.ARCHIVE / history.POST_COMMIT_FAILURE_NAME
        original = json.loads(formal.read_text())
        for field, forged in (("stage", "forged-stage"),
                              ("error_type", "ForgedError"),
                              ("failed_at_epoch", 0)):
            with self.subTest(field=field):
                changed = dict(original)
                changed[field] = forged
                formal.write_text(json.dumps(changed, sort_keys=True, indent=2) + "\n")
                os.chmod(str(formal), 0o600)
                with self.assertRaises(RuntimeError):
                    history.verify_archive()
        formal.write_text(json.dumps(original, sort_keys=True, indent=2) + "\n")
        os.chmod(str(formal), 0o600)
        self.assertTrue(history.verify_archive()["verified"])

    def test_verify_rejects_public_archive_mode(self):
        with mock.patch.object(history, "atomic_rename_noreplace", self.portable_publish):
            history.apply_archive()
        if os.name == "posix":
            os.chmod(str(history.ARCHIVE), 0o755)
            with self.assertRaises(RuntimeError):
                history.verify_archive()
        else:
            real_lstat = history.os.lstat

            def public_archive(path):
                info = real_lstat(path)
                if pathlib.Path(path) == history.ARCHIVE:
                    values = {name: getattr(info, name) for name in dir(info)
                              if name.startswith("st_") and not callable(getattr(info, name))}
                    values["st_mode"] = stat.S_IFDIR | 0o755
                    values["st_uid"] = 0
                    values["st_gid"] = 0
                    return types.SimpleNamespace(**values)
                return info

            with mock.patch.object(history.os, "name", "posix"), \
                 mock.patch.object(history.os, "lstat", side_effect=public_archive):
                with self.assertRaises(RuntimeError):
                    history.verify_archive()

    def test_private_contract_rejects_public_file_and_non_root_owner(self):
        public = types.SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_uid=0, st_gid=0)
        foreign = types.SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=1000, st_gid=0)
        with mock.patch.object(history.os, "name", "posix"):
            with self.assertRaises(RuntimeError):
                history.validate_private_info(public, "file", "unsafe")
            with self.assertRaises(RuntimeError):
                history.validate_private_info(foreign, "file", "unsafe")

    def test_verify_detects_live_source_drift_without_copying(self):
        with mock.patch.object(history, "atomic_rename_noreplace", self.portable_publish):
            history.apply_archive()
        (self.source_one / "job.json").write_bytes(b"changed-after-archive")
        with mock.patch.object(history, "allocate_staging") as allocate:
            with self.assertRaises(RuntimeError):
                history.verify_archive()
        allocate.assert_not_called()
        self.assertEqual(list(self.base.glob(".staging-*")), [])


if __name__ == "__main__":
    unittest.main()
