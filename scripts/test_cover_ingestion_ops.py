import ast
import copy
import hashlib
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


deploy = load('ingestion_deploy', 'ops/cover-ingestion-retry/deploy.py')
recover = load('ingestion_recover', 'ops/cover-ingestion-retry/recover.py')


class DeploymentTests(unittest.TestCase):
    def fixture(self):
        old = '# preserve comment\nOTHER = 3\n' + ''.join('def %s():\n    return "before"\n\n' % n for n in deploy.NAMES)
        baseline = {'app_sha256': hashlib.sha256(old.encode()).hexdigest(),
                    'functions': {n.name: hashlib.sha256(ast.get_source_segment(old, n).encode()).hexdigest()
                                  for n in ast.parse(old).body if isinstance(n, ast.FunctionDef)}}
        return old.encode(), old.replace('"before"', '"after"').encode(), baseline

    def test_only_three_functions_change(self):
        old, new, baseline = self.fixture()
        self.assertEqual(deploy.compose(old, new, baseline), new)

    def test_live_drift_rejected(self):
        old, new, baseline = self.fixture()
        with self.assertRaisesRegex(RuntimeError, 'live_app_drift'):
            deploy.compose(old + b'\n', new, baseline)

    def test_unrelated_release_code_never_installed(self):
        old, new, baseline = self.fixture()
        merged = deploy.compose(old, new.replace(b'OTHER = 3', b'OTHER = 99'), baseline)
        self.assertIn(b'OTHER = 3', merged)
        self.assertNotIn(b'OTHER = 99', merged)


class RecoveryTests(unittest.TestCase):
    def fixture(self):
        images = [dict(source_kind='screenshot', source_key='job:' + size, app_id=1479, sha256=size)
                  for size, _ in recover.material.SPECS]
        after = [dict(key=recover.material.identity(i), state='confirmed', api_id=100+k,
                      final_id=200+k, attempts=1) for k,i in enumerate(images)]
        before = [copy.deepcopy(after[0]), dict(after[1], state='prepared', api_id=None, final_id=None, attempts=0)]
        return images, before, after

    def test_one_success_and_two_new_intakes(self):
        images, before, after = self.fixture()
        recover.validate_confirmed(before, after, images)

    def test_changed_confirmed_receipt_rejected(self):
        images, before, after = self.fixture()
        after[0]['api_id'] += 1
        with self.assertRaisesRegex(RuntimeError, 'previously_confirmed'):
            recover.validate_confirmed(before, after, images)

    def test_unknown_missing_and_duplicate_attempts_do_not_complete_job(self):
        for kind in ('unknown', 'missing', 'repeat'):
            images, before, after = self.fixture()
            if kind == 'unknown':
                after[2]['state'] = 'unknown'
            elif kind == 'missing':
                after.pop()
            else:
                after[2]['attempts'] = 2
            with self.assertRaises(RuntimeError):
                recover.validate_confirmed(before, after, images)


if __name__ == '__main__':
    unittest.main()
