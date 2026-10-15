import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


spec = importlib.util.spec_from_file_location(
    "screenshot_service", str(Path(__file__).resolve().parents[1] / "codex_screenshot_service.py"))
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)


class CompatibilityTests(unittest.TestCase):
    def test_provider_error_survives_startup_noise_and_prompt_echo(self):
        message = "The 'gpt-5.5' model is not supported when using Codex with a ChatGPT account."
        stderr = ('Reading additional input from stdin...\n'
                  'ERROR codex_skills_extension: not a directory\n'
                  'user\n' + 'private prompt ' * 1000 + '\nERROR: ' +
                  json.dumps({'error': {'message': message}}))
        proc = subprocess.CompletedProcess([], 1, '', stderr)
        with mock.patch.object(service.subprocess, 'run', return_value=proc):
            with self.assertRaises(service.CodexConfigurationError) as caught:
                service.run_cmd(['codex'])
        self.assertEqual(str(caught.exception), 'command failed (1): ' + message)
        self.assertIs(caught.exception.proc, proc)

    def test_non_json_and_empty_error(self):
        self.assertEqual(service.command_failure_detail('', 'startup\nERROR: rate limit'), 'rate limit')
        self.assertEqual(service.command_failure_detail('', ''), 'Codex exited without an error detail')
        self.assertEqual(service.command_failure_detail('terminal failure', ''), 'terminal failure')

    def test_isolated_home_replaces_legacy_skills_file_with_directory(self):
        with tempfile.TemporaryDirectory() as root:
            old = Path(root) / 'codex_home'
            old.mkdir()
            (old / 'skills').write_text('legacy sentinel')
            with mock.patch.object(service, 'SOURCE_CODEX_HOME', str(Path(root) / 'empty')):
                result = service.prepare_isolated_codex_home(root, str(Path(root) / 'workspace'))
            self.assertTrue((Path(result) / 'skills').is_dir())
            self.assertFalse((Path(result) / 'skills' / 'personal').exists())

    def test_batch_configuration_failure_does_not_retry_each_size(self):
        with tempfile.TemporaryDirectory() as root:
            items = [{'key': k, 'workspace_output_path': os.path.join(root, k + '.jpg'),
                      'public_output_path': os.path.join(root, 'public', k + '.jpg')}
                     for k in ('square', 'landscape')]
            error = service.CodexConfigurationError('model unsupported')
            with mock.patch.multiple(service, WORK_ROOT=root, CACHE_ROOT=root,
                                     PROMPT_WORKSPACE_ROOT=root, ITEM_PARALLELISM=1), \
                 mock.patch.object(service, 'prepare_source_file', return_value='source.jpg'), \
                 mock.patch.object(service, 'try_restore_from_cache', return_value=None), \
                 mock.patch.object(service, 'build_codex_env', return_value={}), \
                 mock.patch.object(service, 'build_codex_batch_imagegen_instruction', return_value='test'), \
                 mock.patch.object(service, 'run_codex_cmd', side_effect=error) as run:
                with self.assertRaises(service.CodexConfigurationError):
                    service.generate_screenshots({'job_id': 'test-job', 'items': items})
                self.assertEqual(run.call_count, 1)

    def test_explicit_model_is_used_in_exec(self):
        with mock.patch.object(service, 'CODEX_MODEL', 'gpt-6.1-sol'):
            cmd = service.build_codex_exec_cmd('workspace', 'source.jpg', 'result.json', 'generate')
        self.assertEqual(cmd[cmd.index('-m') + 1], 'gpt-6.1-sol')


if __name__ == '__main__':
    unittest.main()
