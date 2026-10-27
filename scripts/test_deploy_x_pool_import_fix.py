"""Scoped deployment and failure recovery never restart publishers or restore DBs."""
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import deploy_x_pool_import_fix as deploy


class DeploymentTests(unittest.TestCase):
    def exercise(self, *, rollback=False, fail_health=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backup = root / 'backup'; backup.mkdir()
            source = root / deploy.SOURCE; source.parent.mkdir(parents=True); source.write_bytes(b'new source')
            target = root / 'live.py'; target.write_bytes(b'new source' if rollback else b'old source')
            previous = backup / 'old.py'; previous.write_bytes(b'old source')
            db = root / 'ledger.sqlite'; db.write_bytes(b'ledger must be retained')
            tokens = root / 'tokens'; tokens.mkdir(); (tokens / 'fixture').write_bytes(b'inert test fixture')
            config = {'paths': {'post_db': str(db), 'tokens': str(tokens)}}
            manifest = {'backup': str(backup), 'files': [{'target': str(target), 'backup': str(previous),
                        'before': deploy.base.digest(previous), 'after': deploy.base.digest(source)}]}
            with patch.object(deploy, 'ROOT', root), patch.object(deploy, 'pids', return_value={'x': '123'}), \
                 patch.object(deploy.base, 'unit_state', return_value={'ActiveState': 'active'}), \
                 patch.object(deploy.base, 'token_facts', return_value={'fixture': 'same'}), \
                 patch.object(deploy.base, 'snapshot', side_effect=lambda src,dst: shutil.copyfile(src,dst)) as snapshot, \
                 patch.object(deploy.base, 'atomic_copy', side_effect=shutil.copyfile), \
                 patch.object(deploy.base, 'run') as run, \
                 patch.object(deploy.base, 'health', side_effect=[RuntimeError('health failed'), None] if fail_health else None):
                if fail_health:
                    with self.assertRaises(RuntimeError):
                        deploy.change(config, manifest, rollback)
                else:
                    deploy.change(config, manifest, rollback)
                self.assertEqual(run.call_args_list[0].args, ('systemctl', 'restart', deploy.MAIN))
                self.assertTrue(all(call.args == ('systemctl', 'restart', deploy.MAIN) for call in run.call_args_list))
                self.assertEqual(snapshot.call_count, 1)
            expected = b'old source' if rollback != fail_health else b'new source'
            self.assertEqual(target.read_bytes(), expected)
            self.assertEqual(db.read_bytes(), b'ledger must be retained')
            self.assertEqual((tokens / 'fixture').read_bytes(), b'inert test fixture')
            saved = json.loads((backup / 'manifest.json').read_text())
            self.assertEqual(saved['status'], 'failed_restored' if fail_health else 'rolled_back' if rollback else 'deployed')

    def test_apply_restarts_only_main(self): self.exercise()
    def test_rollback_preserves_live_database(self): self.exercise(rollback=True)
    def test_failed_apply_restores_code(self): self.exercise(fail_health=True)
    def test_failed_rollback_restores_new_code(self): self.exercise(rollback=True, fail_health=True)


if __name__ == '__main__':
    unittest.main()
