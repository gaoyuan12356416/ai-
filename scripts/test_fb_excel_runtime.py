"""Guard against validating root-only packages or ignoring a failed identity probe."""
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

import check_fb_excel_runtime as probe


class RuntimeProbeTests(unittest.TestCase):
    def test_uses_service_identity_and_ignores_private_python_paths(self):
        with patch.object(probe.subprocess, "check_output", return_value="fb-auto-post\n"), \
                patch.object(probe.subprocess, "run") as run, \
                patch.dict(os.environ, {"PYTHONPATH": "/root/private", "PYTHONUSERBASE": "/root/.local",
                                        "PYTHONNOUSERSITE": "1"}):
            probe.check_as_service_user(Path.cwd())
        args, kwargs = run.call_args
        self.assertEqual(args[0][:6], ["runuser", "-u", "fb-auto-post", "--", "/usr/bin/python3", "-"])
        self.assertNotIn("PYTHONPATH", kwargs["env"])
        self.assertNotIn("PYTHONUSERBASE", kwargs["env"])
        self.assertNotIn("PYTHONNOUSERSITE", kwargs["env"])
        self.assertTrue(kwargs["check"])

    def test_failed_runtime_probe_aborts(self):
        with patch.object(probe.subprocess, "check_output", return_value="fb-auto-post\n"), \
                patch.object(probe.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "probe")):
            with self.assertRaises(subprocess.CalledProcessError):
                probe.check_as_service_user(Path.cwd())

    def test_does_not_accept_root_service_identity(self):
        with patch.object(probe.subprocess, "check_output", return_value="root\n"), \
                patch.object(probe.subprocess, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "identity"):
                probe.check_as_service_user(Path.cwd())
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
