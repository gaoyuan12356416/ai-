import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from deploy import apply_ad_control_v3_navigation as navigation_deploy


V3_GROUP = {
    "key": "ad_control_v3",
    "label": "AI自动调控 V3",
    "order": 6,
    "module": "ad_control_center",
    "items": [
        {
            "key": "adControlV3Rules",
            "label": "规则组管理",
            "kind": "page",
            "href": "/api/ad-control/v3/ui/rule-groups",
            "enabled": True,
            "order": 10,
        },
        {
            "key": "adControlV3Logs",
            "label": "执行日志",
            "kind": "page",
            "href": "/api/ad-control/v3/ui/execution-logs",
            "enabled": True,
            "order": 20,
        },
    ],
}

LEGACY_GROUP = {
    "key": "ad_control",
    "label": "旧版调控",
    "order": 5,
    "items": [
        {
            "key": "adControl",
            "kind": "page",
            "href": "/ad-control.html",
        }
    ],
}

DRAMA_GROUP = {
    "key": "drama",
    "label": "短剧任务",
    "order": 10,
    "items": [
        {
            "key": "tasks",
            "kind": "page",
            "href": "/drama-synthesis.html",
        }
    ],
}


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


class AdControlV3NavigationDeployTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="ad-control-v3-nav-")
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.static = self.repo / "static"
        self.live = self.root / "live-navigation.json"
        self.backup = self.root / "data-disk-backups"
        self.static.mkdir(parents=True)
        self._git("init")
        self._git("config", "user.name", "Codex Test")
        self._git("config", "user.email", "codex-test@example.invalid")
        self._commit_source([LEGACY_GROUP, V3_GROUP, DRAMA_GROUP], "reviewed navigation")
        self.original_live_bytes = encoded([LEGACY_GROUP, DRAMA_GROUP])
        self.live.write_bytes(self.original_live_bytes)

    def tearDown(self):
        self.temporary.cleanup()

    def _git(self, *arguments):
        return subprocess.check_output(
            ["git", "-C", str(self.repo)] + list(arguments),
            text=True,
            stderr=subprocess.STDOUT,
        )

    def _commit_source(self, value, message):
        path = self.static / "navigation.json"
        if isinstance(value, bytes):
            path.write_bytes(value)
        else:
            path.write_bytes(encoded(value))
        self._git("add", "static/navigation.json")
        self._git("commit", "-m", message)
        return self._git("rev-parse", "HEAD").strip()

    def apply(self, check=False):
        return navigation_deploy.apply_navigation(
            repo_root=self.repo,
            live_target=self.live,
            backup_root=self.backup,
            check=check,
        )

    def test_check_is_read_only_then_apply_and_repeat_are_idempotent(self):
        checked = self.apply(check=True)
        self.assertEqual("would_change", checked["status"])
        self.assertEqual(self.original_live_bytes, self.live.read_bytes())
        self.assertFalse(self.backup.exists())

        applied = self.apply()
        self.assertEqual("changed", applied["status"])
        checkpoint = Path(applied["checkpoint"])
        self.assertTrue(checkpoint.is_file())
        manifest = json.loads(checkpoint.read_text(encoding="utf-8"))
        backup_file = checkpoint.parent / manifest["backup_file"]
        self.assertEqual(self.original_live_bytes, backup_file.read_bytes())
        self.assertEqual(
            navigation_deploy.sha256_bytes(self.original_live_bytes),
            manifest["before_sha256"],
        )
        self.assertEqual(
            navigation_deploy.sha256_bytes(self.live.read_bytes()),
            manifest["after_sha256"],
        )
        live_navigation = json.loads(self.live.read_text(encoding="utf-8"))
        self.assertEqual(
            ["ad_control", "ad_control_v3", "drama"],
            [group["key"] for group in live_navigation],
        )
        self.assertEqual(V3_GROUP, live_navigation[1])

        repeated = self.apply()
        self.assertEqual("unchanged", repeated["status"])
        self.assertEqual("", repeated["checkpoint"])
        self.assertEqual(
            1, len(list(self.backup.glob("ad-control-v3-navigation-*")))
        )

    def test_existing_different_v3_group_is_drift_and_never_backed_up(self):
        different = json.loads(json.dumps(V3_GROUP))
        different["label"] = "未经评审的名称"
        self.live.write_bytes(encoded([LEGACY_GROUP, different, DRAMA_GROUP]))
        before = self.live.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "group drift"):
            self.apply()
        self.assertEqual(before, self.live.read_bytes())
        self.assertFalse(self.backup.exists())

    def test_duplicate_live_group_key_is_rejected(self):
        self.live.write_bytes(encoded([LEGACY_GROUP, LEGACY_GROUP, DRAMA_GROUP]))
        with self.assertRaisesRegex(RuntimeError, "duplicate group key"):
            self.apply(check=True)
        self.assertFalse(self.backup.exists())

    def test_invalid_committed_source_json_is_rejected(self):
        self._commit_source(b"{not-json\n", "invalid source")
        with self.assertRaisesRegex(RuntimeError, "not valid UTF-8 JSON"):
            self.apply(check=True)
        self.assertEqual(self.original_live_bytes, self.live.read_bytes())

    def test_source_with_wrong_dynamic_pages_is_rejected(self):
        wrong = json.loads(json.dumps(V3_GROUP))
        wrong["items"][1]["href"] = "/static/not-reviewed.html"
        self._commit_source([LEGACY_GROUP, wrong], "wrong V3 page")
        with self.assertRaisesRegex(RuntimeError, "two reviewed dynamic routes"):
            self.apply(check=True)

    def test_dirty_source_file_is_rejected_in_favor_of_exact_head_blob(self):
        (self.static / "navigation.json").write_bytes(encoded([LEGACY_GROUP]))
        with self.assertRaisesRegex(RuntimeError, "differs from exact HEAD commit"):
            self.apply(check=True)
        self.assertEqual(self.original_live_bytes, self.live.read_bytes())

    def test_live_target_symlink_is_rejected(self):
        real_target = self.root / "real-navigation.json"
        real_target.write_bytes(self.original_live_bytes)
        self.live.unlink()
        try:
            self.live.symlink_to(real_target)
        except (OSError, NotImplementedError) as exc:
            self.live.write_bytes(self.original_live_bytes)
            path_class = type(self.live)
            real_is_symlink = path_class.is_symlink

            def simulated_symlink(path):
                return path == self.live or real_is_symlink(path)

            with mock.patch.object(path_class, "is_symlink", simulated_symlink):
                with self.assertRaisesRegex(RuntimeError, "regular non-symlink file"):
                    self.apply(check=True)
        else:
            with self.assertRaisesRegex(RuntimeError, "regular non-symlink file"):
                self.apply(check=True)
        self.assertEqual(self.original_live_bytes, real_target.read_bytes())

    def test_source_navigation_symlink_is_rejected(self):
        source_path = self.static / "navigation.json"
        source_bytes = source_path.read_bytes()
        alternate = self.static / "navigation-real.json"
        alternate.write_bytes(source_bytes)
        source_path.unlink()
        try:
            source_path.symlink_to(alternate)
        except (OSError, NotImplementedError) as exc:
            source_path.write_bytes(source_bytes)
            path_class = type(source_path)
            real_is_symlink = path_class.is_symlink

            def simulated_symlink(path):
                return path == source_path or real_is_symlink(path)

            with mock.patch.object(path_class, "is_symlink", simulated_symlink):
                with self.assertRaisesRegex(RuntimeError, "regular non-symlink file"):
                    self.apply(check=True)
        else:
            with self.assertRaisesRegex(RuntimeError, "regular non-symlink file"):
                self.apply(check=True)

    def test_apply_requires_backup_root_but_check_does_not(self):
        result = navigation_deploy.apply_navigation(
            repo_root=self.repo,
            live_target=self.live,
            backup_root=None,
            check=True,
        )
        self.assertEqual("would_change", result["status"])
        with self.assertRaisesRegex(RuntimeError, "backup_root on the data disk is required"):
            navigation_deploy.apply_navigation(
                repo_root=self.repo,
                live_target=self.live,
                backup_root=None,
            )
        self.assertEqual(self.original_live_bytes, self.live.read_bytes())

    def test_checkpoint_can_check_and_restore_exact_original_bytes(self):
        applied = self.apply()
        installed_bytes = self.live.read_bytes()
        checked = navigation_deploy.rollback_navigation(
            applied["checkpoint"], live_target=self.live, check=True
        )
        self.assertEqual("would_rollback", checked["status"])
        self.assertEqual(installed_bytes, self.live.read_bytes())

        rolled_back = navigation_deploy.rollback_navigation(
            applied["checkpoint"], live_target=self.live
        )
        self.assertEqual("rolled_back", rolled_back["status"])
        self.assertEqual(self.original_live_bytes, self.live.read_bytes())

    def test_rollback_refuses_current_live_drift(self):
        applied = self.apply()
        drift = encoded([LEGACY_GROUP, DRAMA_GROUP, {"key": "manual", "items": []}])
        self.live.write_bytes(drift)
        with self.assertRaisesRegex(RuntimeError, "blocked by current live drift"):
            navigation_deploy.rollback_navigation(
                applied["checkpoint"], live_target=self.live
            )
        self.assertEqual(drift, self.live.read_bytes())

    def test_rollback_refuses_tampered_backup(self):
        applied = self.apply()
        manifest_path = Path(applied["checkpoint"])
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        backup_path = manifest_path.parent / manifest["backup_file"]
        backup_path.write_bytes(b"tampered\n")
        installed = self.live.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "backup checksum mismatch"):
            navigation_deploy.rollback_navigation(
                manifest_path, live_target=self.live
            )
        self.assertEqual(installed, self.live.read_bytes())

    def test_rollback_refuses_a_different_target_path(self):
        applied = self.apply()
        another = self.root / "another-navigation.json"
        another.write_bytes(self.live.read_bytes())
        with self.assertRaisesRegex(RuntimeError, "does not match checkpoint"):
            navigation_deploy.rollback_navigation(
                applied["checkpoint"], live_target=another
            )


if __name__ == "__main__":
    unittest.main()
