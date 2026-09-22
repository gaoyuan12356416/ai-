"""Release transformations and rollback rehearsed on isolated filesystem state."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import deploy_youtube_channel_templates as deploy


class Deployment(unittest.TestCase):
    def test_append_preserves_groups_and_permission_metadata(self):
        source = [{'key':'existing','items':[]}, {'key':'youtube_platform','adminOnly':False,'items':[
            {'key':'youtubeAutoPublish','href':'/youtube-publish.html','enabled':True,'allowedUserIds':['one'],'module':'youtube_auto_publish'}]}]
        result = json.loads(deploy.nav_config(json.dumps(source)))
        self.assertEqual(result[0], source[0])
        self.assertEqual(result[1]['items'][0], source[1]['items'][0])
        self.assertEqual(result[1]['items'][1]['allowedUserIds'], ['one'])
        self.assertEqual(result[1]['items'][1]['module'], 'youtube_auto_publish')
        with self.assertRaises(RuntimeError):deploy.nav_config(json.dumps(result))

    def test_script_patch_preserves_live_only_changes(self):
        source = (Path(__file__).resolve().parents[1]/'static/quick-nav.js').read_text(encoding='utf-8')
        marker='          key: "youtubeChannelList",'
        start=source.rfind('        {',0,source.index(marker))
        end=source.index('        },',source.index(marker))+len('        },\n')
        live=source[:start]+source[end:]+'\n// existing independent navigation patch\n'
        result=deploy.nav_script(live,source)
        self.assertEqual(result,source+'\n// existing independent navigation patch\n')

    def test_rollback_retains_database_and_refuses_later_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'runtime';root.mkdir();public=Path(tmp)/'public';public.mkdir()
            base=Path(tmp)/'deploy';backup=base/'backups'/'one';backup.mkdir(parents=True)
            old=root/'old.py';old.write_bytes(b'new')
            new=public/'added.js';new.write_bytes(b'added')
            db=root/'data.sqlite3';db.write_bytes(b'keep-published-facts')
            saved=deploy.saved_file(backup,old);saved.parent.mkdir(parents=True);saved.write_bytes(b'old')
            manifest={'files':{str(old):{'old':deploy.digest(b'old'),'new':deploy.digest(b'new'),'mode':0o644},
                               str(new):{'old':None,'new':deploy.digest(b'added'),'mode':0o644}}}
            (backup/'manifest.json').write_text(json.dumps(manifest))
            with patch.multiple(deploy,ROOT=root,PUBLIC=public,BASE=base),patch.object(deploy,'run') as run,patch.object(deploy,'healthy'):
                new.write_bytes(b'later')
                with self.assertRaises(RuntimeError):deploy.rollback(backup)
                run.assert_not_called()
                new.write_bytes(b'added')
                deploy.rollback(backup)
                self.assertEqual(old.read_bytes(),b'old')
                self.assertFalse(new.exists())
                self.assertEqual(db.read_bytes(),b'keep-published-facts')


if __name__=='__main__':unittest.main()
