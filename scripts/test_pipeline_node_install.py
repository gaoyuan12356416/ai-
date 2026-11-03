import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from scripts.pipeline_node_install import plan, unit_text, tunnel_text, immutable, configure_tls_ca, read_environment


class NodeInstallTests(unittest.TestCase):
    def test_private_renderer_environment_retains_verified_rgba_cache(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'node.env'
            path.write_text('RANDOM_GPU_ASSET_CACHE_ROOT=/data/cache\nRANDOM_GPU_UNRELATED=ignored\n')
            with patch.object(Path, 'stat', return_value=SimpleNamespace(st_mode=0o100600)):
                self.assertEqual(read_environment([path]), {'RANDOM_GPU_ASSET_CACHE_ROOT': '/data/cache'})

    def test_default_ca_comes_from_the_new_venv_and_worker_can_read_it(self):
        env = {}
        bundle = '/opt/pipeline-dispatch/venvs/new/lib/python3.10/site-packages/certifi/cacert.pem'
        with patch('scripts.pipeline_node_install.command', return_value=bundle) as command:
            configure_tls_ca(env, '/opt/pipeline-dispatch/venvs/new')
        self.assertEqual(env['SSL_CERT_FILE'], bundle)
        args = command.call_args.args[0]
        self.assertEqual(args[:4], ['runuser', '-u', 'pipeline-worker', '--'])
        self.assertEqual(args[4], Path('/opt/pipeline-dispatch/venvs/new/bin/python'))
        self.assertIn('os.access', args[-1])

    def test_explicit_ca_setting_is_preserved_without_replacement(self):
        for value in ('/etc/private/custom-ca.pem', ''):
            env = {'SSL_CERT_FILE': value, 'SSL_CERT_DIR': '/etc/custom-certs'}
            with patch('scripts.pipeline_node_install.command') as command:
                configure_tls_ca(env, '/unused')
            command.assert_not_called()
            self.assertEqual(env, {'SSL_CERT_FILE': value, 'SSL_CERT_DIR': '/etc/custom-certs'})

    def test_private_source_environment_preserves_explicit_ca_files(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'node.env'
            path.write_text('SSL_CERT_FILE=/etc/custom-ca.pem\nSSL_CERT_DIR=/etc/custom-certs\nUNRELATED=ignored\n')
            with patch.object(Path, 'stat', return_value=SimpleNamespace(st_mode=0o100600)):
                self.assertEqual(read_environment([path]), {'SSL_CERT_FILE': '/etc/custom-ca.pem', 'SSL_CERT_DIR': '/etc/custom-certs'})

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

    def test_https_reuses_existing_github_credentials_without_inline_secret(self):
        result = plan({**self.spec(), "repository": "https://github.com/owner/repo.git"})
        self.assertEqual(result["repository"], "https://github.com/owner/repo.git")
        with self.assertRaises(ValueError):
            plan({**self.spec(), "repository": "https://secret@github.com/owner/repo.git"})

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
