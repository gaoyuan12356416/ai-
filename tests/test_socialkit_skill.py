"""Preserve remote JSON/CSV bytes through the saved Windows SSH helper."""
import importlib.util
import io
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('socialkit_skill_query', ROOT/'skills/socalkite-effect-query/scripts/query_report.py')
skill = importlib.util.module_from_spec(spec)
spec.loader.exec_module(skill)


class SkillTransportTests(unittest.TestCase):
    def invoke(self, payload, platform, format_name):
        with tempfile.TemporaryDirectory() as directory:
            helper = Path(directory)/'helper.py'
            helper.write_text('# fixture only',encoding='utf-8')
            stdout = io.BytesIO()
            environment = SimpleNamespace(name=platform, environ=dict(os.environ))
            with patch.object(skill, 'os', environment), patch.object(skill.sys, 'stdout', SimpleNamespace(buffer=stdout)), patch.object(skill.subprocess, 'run', return_value=SimpleNamespace(stdout=payload, returncode=0)) as command:
                result = skill.main(['--ssh-helper',str(helper),'--format',format_name,'--filter','drama=中文'])
            self.assertEqual(result, 0)
            kwargs = command.call_args.kwargs
            self.assertIsInstance(kwargs['input'], bytes)
            self.assertIn('中文',kwargs['input'].decode('utf-8'))
            self.assertEqual(kwargs['env']['PYTHONIOENCODING'],'utf-8')
            self.assertNotIn('text',kwargs)
            self.assertNotIn('stderr',kwargs)
            return stdout.getvalue()

    def test_windows_reverses_exactly_one_translation_for_utf8_json_and_csv(self):
        cases = [('json','{"账号":"中文"}\n'.encode('utf-8')),
                 ('csv','\ufeff账号,收入\r\n"中文\n第二行",12.34\r\n'.encode('utf-8'))]
        for kind, original in cases:
            helper_stdout = original.replace(b'\n',b'\r\n')
            self.assertEqual(self.invoke(helper_stdout,'nt',kind),original)

    def test_non_windows_preserves_remote_crlf_verbatim(self):
        original='\ufeff账号,收入\r\n中文,12.34\r\n'.encode('utf-8')
        self.assertEqual(self.invoke(original,'posix','csv'),original)


if __name__ == '__main__':
    unittest.main()
