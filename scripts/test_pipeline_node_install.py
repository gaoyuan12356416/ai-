import json
from pathlib import Path
import tempfile
import unittest
from scripts.pipeline_node_install import plan, unit_text, tunnel_text, immutable


class NodeInstallTests(unittest.TestCase):
    def spec(self):
        return {"node_id": "arbitrary-node-3", "sha": "a" * 40, "backend": "cpu", "state_root": "/data/pipeline-dispatch"}

    def test_generic_stage_is_exact_commit_and_dedicated(self):
        spec = self.spec(); info = plan(spec)
        self.assertTrue(info["release"].endswith("a" * 40))
        text = unit_text(spec, info)
        self.assertIn("User=pipeline-worker", text)
        self.assertIn("TimeoutStopSec=infinity", text)
        self.assertNotIn("/root/", text)
        self.assertNotIn("restart", text)

    def test_reject_legacy_root_and_mutable_revision(self):
        for change in ({"sha": "main"}, {"state_root": "/data/tt-post-gpu"}, {"install_root": "/opt/legacy"}, {"node_id": "x; reboot"}):
            with self.assertRaises(ValueError): plan({**self.spec(), **change})

    def test_dedicated_forward_preserves_host_verification_and_new_port(self):
        spec = {**self.spec(), "tunnel": {"target": "limited@example.internal", "identity_file": "/etc/tunnel/id_ed25519", "known_hosts": "/etc/tunnel/known_hosts"}}
        plan(spec)
        text = tunnel_text(spec)
        self.assertIn("127.0.0.1:18846:127.0.0.1:18846", text)
        self.assertIn("StrictHostKeyChecking=yes", text)
        self.assertIn("UserKnownHostsFile=/etc/tunnel/known_hosts", text)

    def test_staging_never_overwrites_changed_configuration(self):
        with tempfile.TemporaryDirectory() as temp:
            file = Path(temp) / "node.env"
            immutable(file, b"first"); immutable(file, b"first")
            with self.assertRaises(ValueError): immutable(file, b"second")
            self.assertEqual(file.read_bytes(), b"first")


if __name__ == "__main__": unittest.main()
