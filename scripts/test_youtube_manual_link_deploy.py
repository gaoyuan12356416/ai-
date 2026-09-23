"""Offline surgical deployment and rollback validation."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import deploy_youtube_manual_links as deploy


class DeployTests(unittest.TestCase):
    def test_app_patch_preserves_unrelated_live_changes(self):
        current=b'# unrelated live feature\n        if "/x-share" in path:\n            pass\n'
        result=deploy.patch_app(current)
        self.assertTrue(result.startswith(b'# unrelated live feature\n'))
        self.assertTrue(result.endswith(b'        if "/x-share" in path:\n            pass\n'))
        self.assertEqual(result.count(b'manual_link_routes'),1)
        with self.assertRaises(RuntimeError):deploy.patch_app(result)
        with self.assertRaises(RuntimeError):deploy.patch_app(b'unknown app')

    def test_rollback_keeps_all_generated_data_and_refuses_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'root';root.mkdir();public=Path(tmp)/'public';public.mkdir()
            base=Path(tmp)/'deploy';backup=base/'backups'/'one';backup.mkdir(parents=True)
            old=root/'app.py';old.write_bytes(b'new');new=public/'added.js';new.write_bytes(b'added')
            db=root/'db.sqlite3';db.write_bytes(b'keep-manual-links-and-video-facts')
            link=public/'10.html';link.write_bytes(b'keep-published-link')
            saved=deploy.saved_file(backup,old);saved.parent.mkdir(parents=True);saved.write_bytes(b'old')
            manifest={'files':{str(old):{'old':deploy.digest(b'old'),'new':deploy.digest(b'new'),'mode':0o644},str(new):{'old':None,'new':deploy.digest(b'added'),'mode':0o644}}}
            (backup/'manifest.json').write_text(json.dumps(manifest))
            with patch.multiple(deploy,ROOT=root,PUBLIC=public,BASE=base),patch.object(deploy,'run') as run,patch.object(deploy,'healthy'):
                new.write_bytes(b'later')
                with self.assertRaises(RuntimeError):deploy.rollback(backup)
                run.assert_not_called();new.write_bytes(b'added');deploy.rollback(backup)
                self.assertEqual(old.read_bytes(),b'old');self.assertFalse(new.exists())
                self.assertEqual(db.read_bytes(),b'keep-manual-links-and-video-facts')
                self.assertEqual(link.read_bytes(),b'keep-published-link')


if __name__=='__main__':unittest.main()
