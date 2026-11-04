import json
import hashlib
import io
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch
from types import SimpleNamespace
from scripts.pipeline_node_install import (plan, unit_text, tunnel_text, immutable,
    configure_tls_ca, read_environment, runtime_requirements, stage_runtime,
    configure_gpu_runtime, gpu_runtime_doctor, main)


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

    def test_native_gpu_option_is_explicit_and_gpu_only(self):
        spec = self.spec()
        for change in ({'gpu_native': True}, {'backend': 'gpu', 'gpu_native': '1'}):
            with self.assertRaises(ValueError):
                plan({**spec, **change})
        plan({**spec, 'backend': 'gpu', 'gpu_native': True})

    def test_native_runtime_fingerprint_includes_base_and_gpu_pins(self):
        with tempfile.TemporaryDirectory() as directory:
            release = Path(directory); deploy = release/'deploy'; deploy.mkdir()
            base = deploy/'pipeline-node.requirements.txt'; base.write_bytes(b'base==1\n')
            gpu = deploy/'pipeline-gpu.requirements.txt'; gpu.write_bytes(b'-r pipeline-node.requirements.txt\ngpu==1\n')
            selected, base_hash = runtime_requirements({}, release)
            self.assertEqual(selected, base)
            self.assertEqual(base_hash, hashlib.sha256(base.read_bytes()).hexdigest())
            selected, before = runtime_requirements({'gpu_native': True}, release)
            self.assertEqual(selected, gpu)
            base.write_bytes(b'base==2\n')
            self.assertNotEqual(runtime_requirements({'gpu_native': True}, release)[1], before)

    def test_runtime_refuses_existing_base_environment_for_gpu_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); deploy = root/'deploy'; deploy.mkdir()
            (deploy/'pipeline-node.requirements.txt').write_bytes(b'base==1\n')
            (deploy/'pipeline-gpu.requirements.txt').write_bytes(b'gpu==1\n')
            venv = root/'venv'; venv.mkdir()
            marker = venv/'.requirements.sha256'
            marker.write_text(runtime_requirements({}, root)[1])
            with patch('scripts.pipeline_node_install.command') as command:
                with self.assertRaisesRegex(ValueError, 'venv_requirements_mismatch'):
                    stage_runtime({'gpu_native': True}, {'release': str(root), 'venv': str(venv)})
            command.assert_not_called()

    def test_gpu_runtime_environment_uses_its_own_sha_interpreter(self):
        info = {'venv': '/opt/pipeline-dispatch/venvs/'+'b'*40}
        env = {'RANDOM_GPU_COMPUTE': '0'}
        configure_gpu_runtime({'gpu_native': True}, info, env)
        self.assertEqual(env, {'RANDOM_GPU_NATIVE': '1', 'RANDOM_GPU_COMPUTE': '0',
                              'RANDOM_GPU_PYTHON': info['venv']+'/bin/python'})
        with self.assertRaisesRegex(ValueError, 'gpu_runtime_interpreter_mismatch'):
            configure_gpu_runtime({'gpu_native': True}, info, {'RANDOM_GPU_PYTHON': '/data/evaluation/bin/python'})

    def test_gpu_doctor_is_scoped_local_and_runs_as_service_user(self):
        info = plan({**self.spec(), 'backend': 'gpu', 'gpu_native': True})
        with patch('scripts.pipeline_node_install.command') as command:
            gpu_runtime_doctor({'gpu_native': True}, info)
        args = command.call_args.args[0]
        self.assertEqual(args[:4], ['runuser', '-u', 'pipeline-worker', '--'])
        self.assertEqual(str(args[4]), str(Path(info['venv'])/'bin/python'))
        self.assertEqual(command.call_args.kwargs['cwd'], info['release'])
        self.assertNotIn('enroll', args[-1]); self.assertNotIn('CoordinatorClient', args[-1])

    def test_verify_local_has_no_upload_enrollment_or_activation(self):
        spec = {**self.spec(), 'backend': 'gpu', 'gpu_native': True}
        with patch('sys.argv', ['installer', '--manifest', '/private/spec.json', '--verify-local']), \
             patch('scripts.pipeline_node_install.private_json', return_value=spec), \
             patch('scripts.pipeline_node_install.stage'), \
             patch('scripts.pipeline_node_install.gpu_runtime_doctor') as doctor, \
             patch('scripts.pipeline_node_install.command') as command, \
             patch('scripts.pipeline_node_install.immutable') as save, \
             patch('scripts.pipeline_node_install.activate') as activate, redirect_stdout(io.StringIO()):
            self.assertEqual(main(), 0)
        doctor.assert_called_once(); activate.assert_not_called()
        args = command.call_args.args[0]
        self.assertIn('--encode', args); self.assertNotIn('--cos-check', args)
        self.assertNotIn('--enroll', args)
        self.assertEqual(save.call_args.args[0].name, '.verified-local')

    def test_verify_local_cannot_start_or_enroll(self):
        for option in ('--activate', '--enroll', '--verify'):
            with patch('sys.argv', ['installer', '--manifest', '/private/spec.json', '--verify-local', option]), \
                 patch('scripts.pipeline_node_install.stage') as stage, redirect_stdout(io.StringIO()):
                self.assertEqual(main(), 1)
            stage.assert_not_called()


if __name__ == "__main__": unittest.main()
