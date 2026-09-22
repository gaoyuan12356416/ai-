import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.youtube_auto_publish.cover_provenance import isolated_home, remove_private_auth, verify_generated_origin
from features.youtube_auto_publish.templates import WorkflowError


class OriginCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name); self.home = self.root / 'home'
        self.id = '01234567-0123-0123-0123-012345678901'
        self.directory = self.home / 'generated_images' / self.id
        self.directory.mkdir(parents=True)
        self.raw = b'new native artifact'
        self.path = self.directory / 'exec-tool.png'; self.path.write_bytes(self.raw)
        self.events = json.dumps({'type': 'thread.started', 'thread_id': self.id})
        self.started = time.time() - 1

    def verify(self, events=None):
        return verify_generated_origin(self.events if events is None else events, self.home,
                                       self.raw, self.started, self.root / 'generation')

    def rejects(self, code='cover_generation_origin_unverified', events=None):
        with self.assertRaises(WorkflowError) as e: self.verify(events)
        self.assertEqual(e.exception.code, code)

    def test_accepts_current_thread_bytes_and_keeps_digest(self):
        result = self.verify()
        self.assertEqual(result['sha256'], hashlib.sha256(self.raw).hexdigest())
        self.assertEqual(result['thread_id'], self.id)

    def test_final_claim_does_not_substitute_for_cli_thread(self):
        self.rejects(events=json.dumps({'type': 'item.completed', 'item': {'text': 'Generated successfully'}}))

    def test_history_copy_is_rejected_even_when_cover_is_valid(self):
        self.path.write_bytes(b'unrelated newly generated image')
        self.rejects()

    def test_old_artifact_and_other_thread_are_rejected(self):
        os.utime(self.path, (1, 1)); self.rejects()
        self.path.write_bytes(self.raw)
        self.rejects(events=json.dumps({'type': 'thread.started', 'thread_id': 'ffffffff-ffff-ffff-ffff-ffffffffffff'}))

    def test_prior_accepted_hash_cannot_be_copied_into_new_home(self):
        prior = self.root / 'generation' / 'old-task' / 'v1'
        prior.mkdir(parents=True)
        (prior / 'generation-output.json').write_text(json.dumps({'source_sha256': hashlib.sha256(self.raw).hexdigest()}))
        self.rejects('cover_generation_reused_image')

    def test_duplicate_or_invalid_thread_identity_fails_closed(self):
        self.rejects(events=self.events + '\n' + self.events)
        self.rejects(events=json.dumps({'type': 'thread.started', 'thread_id': '../old'}))

    def test_symlink_artifact_is_not_accepted(self):
        target = self.root / 'old.png'; target.write_bytes(self.raw); self.path.unlink()
        try: self.path.symlink_to(target)
        except OSError: self.skipTest('OS disallows symlinks')
        self.rejects()

    def test_fresh_home_seeds_only_cli_auth_and_removes_auth(self):
        source = self.root / 'source'; source.mkdir()
        for name in ('auth.json', 'config.toml', 'AGENTS.md', 'history.jsonl'):
            (source / name).write_text('fixture')
        (source / 'generated_images').mkdir()
        (source / 'generated_images' / 'old.png').write_bytes(self.raw)
        work = self.root / 'work'; work.mkdir()
        with patch.dict(os.environ, {'CODEX_HOME': str(source)}): home = isolated_home(work)
        self.assertTrue((home / 'auth.json').is_file())
        self.assertFalse((home / 'config.toml').exists())
        self.assertFalse((home / 'AGENTS.md').exists())
        self.assertEqual(list((home / 'generated_images').iterdir()), [])
        remove_private_auth(home)
        self.assertFalse((home / 'auth.json').exists())
        self.assertEqual((source / 'auth.json').read_text(), 'fixture')


if __name__ == '__main__': unittest.main(verbosity=2)
