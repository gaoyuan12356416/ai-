"""Offline surgical deployment and rollback validation."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import deploy_youtube_analytics as deploy


class DeployTests(unittest.TestCase):
    def test_app_patch_preserves_unrelated_live_changes(self):
        anchor=b'        if parsed.path == "/api/youtube-auto-publish" or parsed.path.startswith("/api/youtube-auto-publish/"):\n'
        current=b'# unrelated live feature\n'+anchor+b'            pass\n'+anchor
        result=deploy.patch_app(current)
        self.assertTrue(result.startswith(b'# unrelated live feature\n'))
        self.assertEqual(result.count(b'features.youtube_analytics.routes'),2)
        self.assertEqual(result.replace(b'        if parsed.path == "/api/youtube-analytics" or parsed.path.startswith("/api/youtube-analytics/"):\n            from features.youtube_analytics.routes import dispatch\n            return dispatch(self, parsed, globals())\n\n',b''),current)
        with self.assertRaises(RuntimeError): deploy.patch_app(result)
        with self.assertRaises(RuntimeError): deploy.patch_app(b'unknown app')

    def test_navigation_retains_existing_restrictions(self):
        original=[{'key':'youtube_platform','module':'youtube_auto_publish','items':[{'key':'youtubeAutoPublish','adminOnly':True,'enabled':False,'module':'youtube_auto_publish','href':'/youtube-publish.html'}]}]
        changed=json.loads(deploy.nav_config(json.dumps(original)))
        self.assertEqual(changed[0]['items'][0],original[0]['items'][0])
        self.assertEqual(changed[0]['items'][1]['key'],'youtubeAnalytics')
        self.assertTrue(changed[0]['items'][1]['adminOnly'])
        self.assertFalse(changed[0]['items'][1]['enabled'])
        with self.assertRaises(RuntimeError): deploy.nav_config(json.dumps(changed))

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
