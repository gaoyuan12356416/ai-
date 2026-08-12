#!/usr/bin/env python3
"""Static fail-closed deployment contracts for X automatic templates."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class XAutoPostDeployTests(unittest.TestCase):
    @staticmethod
    def text(path: str) -> str:
        return (ROOT / path).read_text(encoding="utf-8")

    def test_initial_environment_is_isolated_and_all_live_gates_are_closed(self):
        env = self.text("deploy/x-auto-post.env.example")
        self.assertIn("X_AUTO_POST_SERVICE_HOST=127.0.0.1", env)
        self.assertIn("X_AUTO_POST_SERVICE_PORT=18833", env)
        for gate in (
            "X_AUTO_POST_LIVE_ENABLED",
            "X_AUTO_POST_ACCOUNT_AUDIT_APPROVED",
            "X_AUTO_POST_URL_PROPERTY_VERIFIED",
        ):
            self.assertIn(gate + "=0", env)
            self.assertNotIn(gate + "=1", env)
        self.assertIn(
            "X_AUTO_POST_DB_PATH=/mnt/data-disk/x-auto-post-publisher/x-auto-post.sqlite3",
            env,
        )
        self.assertIn("X_AUTO_POST_X_BRIDGE_TIMEOUT=120", env)
        self.assertIn("X_AUTO_POST_X_PUBLISH_TIMEOUT=9000", env)
        self.assertIn("X_AUTO_POST_EXECUTE_TIMEOUT=10200", env)
        self.assertIn("X_AUTO_POST_LEASE_SECONDS=10800", env)

    def test_auto_and_existing_x_publishers_share_only_the_publish_lock(self):
        env = self.text("deploy/x-auto-post.env.example")
        runner = self.text("scripts/x_auto_post_runner.py")
        unit = self.text("deploy/x-auto-post-runner.service")
        expected = "/run/x-post-daily/runner.lock"
        self.assertIn("X_AUTO_POST_RUNNER_LOCK_PATH=" + expected, env)
        self.assertIn('"' + expected + '"', runner)
        self.assertIn("ReadWritePaths=/run/x-auto-post /run/x-post-daily", unit)
        self.assertNotIn("/run/x-auto-post/runner.lock", env + runner + unit)

    def test_sidecar_declares_and_checks_the_shared_ffprobe_dependency(self):
        env = self.text("deploy/x-auto-post.env.example")
        unit = self.text("deploy/x-auto-post-service.service")
        expected = "/mnt/data-disk/x-post-automation/bin/ffprobe"
        self.assertIn("X_POST_FFPROBE_BIN=" + expected, env)
        self.assertIn("ExecStartPre=/usr/bin/test -x " + expected, unit)
        self.assertIn(
            "RequiresMountsFor=/mnt/data-disk/x-auto-post-publisher "
            "/mnt/data-disk/x-post-automation",
            unit,
        )
        self.assertNotIn("/usr/bin/ffprobe", env + unit)

    def test_shared_lock_directories_have_one_persistent_tmpfiles_owner(self):
        tmpfiles = self.text("deploy/x-post-runtime-tmpfiles.conf")
        auto_tmpfiles = self.text("deploy/x-auto-post-tmpfiles.conf")
        self.assertIn(
            "d /run/x-auto-post 0700 x-post-daily x-post-daily -", tmpfiles
        )
        self.assertIn(
            "d /run/x-post-daily 0700 x-post-daily x-post-daily -", tmpfiles
        )
        self.assertEqual(tmpfiles.count("d /run/x-auto-post "), 1)
        self.assertEqual(tmpfiles.count("d /run/x-post-daily "), 1)
        self.assertNotIn("/run/x-auto-post", auto_tmpfiles)
        self.assertNotIn("/run/x-post-daily", auto_tmpfiles)

        shared_units = (
            "x-auto-post-service.service",
            "x-auto-post-scheduler.service",
            "x-auto-post-runner.service",
            "x-auto-post-metric.service",
            "x-post-daily.service",
            "x-post-manual.service",
            "x-post-schedule.service",
            "x-post-catchup.service",
        )
        for name in shared_units:
            with self.subTest(unit=name):
                unit = self.text("deploy/" + name)
                self.assertNotIn("RuntimeDirectory=", unit)
                self.assertIn("systemd-tmpfiles-setup.service", unit)
                if "/run/x-auto-post" in unit:
                    self.assertIn(
                        "ConditionPathIsDirectory=/run/x-auto-post", unit
                    )
                if "/run/x-post-daily" in unit:
                    self.assertIn(
                        "ConditionPathIsDirectory=/run/x-post-daily", unit
                    )

    def test_admin_html_shells_are_no_store_in_nginx(self):
        nginx = self.text("deploy/x-auto-post-nginx.conf")
        for page in (
            "x-auto-publish-templates.html",
            "x-auto-publish-template.html",
            "x-auto-publish-runs.html",
        ):
            with self.subTest(page=page):
                self.assertIn(f"location = /{page}", nginx)
        self.assertEqual(
            nginx.count('add_header Cache-Control "no-store, max-age=0" always;'),
            3,
        )
        self.assertEqual(nginx.count("try_files $uri =404;"), 3)

    @unittest.skipIf(os.name == "nt", "requires Linux flock and inode semantics")
    def test_linux_shared_lock_inode_survives_peer_process_exit(self):
        """A peer unit/process exit must not replace the shared flock inode."""

        from scripts.x_auto_post_runner import exclusive_lock

        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            runtime = root / "x-post-daily"
            runtime.mkdir(mode=0o700)
            lock_path = runtime / "runner.lock"
            with exclusive_lock(str(lock_path)) as acquired:
                self.assertTrue(acquired)
                inode = lock_path.stat().st_ino
                peer = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        textwrap.dedent(
                            """
                            import fcntl
                            import os
                            import sys

                            path = sys.argv[1]
                            handle = open(path, "a+b")
                            try:
                                fcntl.flock(
                                    handle.fileno(),
                                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                                )
                            except BlockingIOError:
                                sys.exit(75)
                            sys.exit(0)
                            """
                        ),
                        str(lock_path),
                    ],
                    check=False,
                )
                self.assertEqual(peer.returncode, 75)
                self.assertTrue(runtime.is_dir())
                self.assertEqual(lock_path.stat().st_ino, inode)
            self.assertTrue(runtime.is_dir())
            self.assertEqual(lock_path.stat().st_ino, inode)

    def test_sidecar_cannot_read_existing_x_tokens_or_accounts_database(self):
        unit = self.text("deploy/x-auto-post-service.service")
        self.assertIn("User=x-post-daily", unit)
        self.assertIn("EnvironmentFile=/etc/x-post-schedule.env", unit)
        self.assertNotIn("EnvironmentFile=/etc/x-post-daily.env", unit)
        self.assertIn("InaccessiblePaths=/var/lib/x-post-automation /etc/ssh", unit)
        self.assertNotIn("X_CLIENT_SECRET", unit)
        self.assertNotIn("X_TOKENS_DIR", unit)
        self.assertIn("ProtectSystem=strict", unit)
        self.assertIn("NoNewPrivileges=true", unit)

    def test_metric_and_sidecar_use_only_the_narrow_auto_mysql_environment(self):
        secrets = self.text("deploy/x-auto-post.secrets.example")
        for name in (
            "X_AUTO_POST_MYSQL_HOST",
            "X_AUTO_POST_MYSQL_PORT",
            "X_AUTO_POST_MYSQL_USER",
            "X_AUTO_POST_MYSQL_PASSWORD",
        ):
            self.assertIn(name + "=", secrets)
        for name in ("x-auto-post-service.service", "x-auto-post-metric.service"):
            unit = self.text("deploy/" + name)
            self.assertIn("EnvironmentFile=/etc/x-post-schedule.env", unit)
            self.assertIn("EnvironmentFile=/etc/x-auto-post.secrets", unit)
            self.assertNotIn("EnvironmentFile=/etc/x-post-daily.env", unit)

    def test_execution_bearer_is_dedicated_and_not_in_browser_configuration(self):
        secrets = self.text("deploy/x-auto-post.secrets.example")
        existing = self.text("deploy/x-post-automation.env.example")
        app = self.text("deploy/x-auto-post-app.env.example")
        self.assertIn("X_AUTO_POST_INTERNAL_TOKEN=", secrets)
        self.assertIn("X_POST_AUTO_INTERNAL_TOKEN=", secrets)
        self.assertIn("X_POST_AUTO_INTERNAL_TOKEN=", existing)
        self.assertNotIn("X_POST_AUTO_INTERNAL_TOKEN", app)
        values = [
            line.split("=", 1)[1]
            for line in secrets.splitlines()
            if line.startswith(("X_AUTO_POST_INTERNAL_TOKEN=", "X_POST_AUTO_INTERNAL_TOKEN="))
        ]
        self.assertEqual(len(values), 2)
        self.assertNotEqual(values[0], values[1])

    def test_main_api_uses_an_optional_dedicated_environment_dropin(self):
        dropin = self.text("deploy/x-auto-post-app.conf")
        self.assertIn("[Service]", dropin)
        self.assertIn("EnvironmentFile=-/etc/x-auto-post-app.env", dropin)
        self.assertNotIn("X_POST_AUTO_INTERNAL_TOKEN", dropin)

    def test_new_timers_are_nonpersistent_and_do_not_replace_existing_units(self):
        for name in (
            "x-auto-post-metric.timer",
            "x-auto-post-runner.timer",
            "x-auto-post-scheduler.timer",
        ):
            timer = self.text("deploy/" + name)
            self.assertIn("Persistent=false", timer)
            self.assertIn("WantedBy=timers.target", timer)
        units = "\n".join(
            self.text("deploy/" + name)
            for name in (
                "x-auto-post-service.service",
                "x-auto-post-metric.service",
                "x-auto-post-runner.service",
                "x-auto-post-scheduler.service",
            )
        )
        self.assertNotIn("ExecStart=/usr/bin/python3 /opt/x-post-automation/current/scripts/x_post_", units)


if __name__ == "__main__":
    unittest.main()
