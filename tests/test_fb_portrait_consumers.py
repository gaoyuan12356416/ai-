from contextlib import closing
import json
import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from features import fb_portrait_matching as matching
from features.fb_page_current import selection
from features.fb_page_current.prepared import PreparedPages


class MatchingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.cache=self.root/'matching'/'current.sqlite3'
        self.version='20261124T020236720519Z'
        self.pointer=dict(backend='mysql',database=matching.TABLE,version=self.version)
        (self.root/'current.json').write_text(json.dumps(self.pointer))
        self.page=dict(page_id='123',status='ready',revenue_cents=25,hierarchy_version='h',
                       audiences=[dict(key='female',children=[dict(key='marital',dramas=[{'secret_detail':1}])])])

    def build(self):
        proof=dict(version=self.version,verified_rows=1,payload_hashes={'123':'digest'})
        builder=selection.Builder(dict(version=self.version),proof,self.cache)
        try:
            builder.add('123',self.page,'digest')
            builder.finish()
        finally:
            builder.close()

    def test_current_uses_verified_scalars_and_compact_tree(self):
        self.build()
        found,meta,errors=matching.read_matching(self.root,self.pointer,['123','123','999'])
        self.assertEqual(set(found),{'123'})
        self.assertEqual((found['123']['revenue_cents'],found['123']['hierarchy_version']),(25,'h'))
        self.assertNotIn('dramas',found['123']['audiences'][0]['children'][0])
        self.assertEqual((meta['version'],errors),(self.version,{}))
        self.assertEqual(selection.load(self.version,['123'],self.cache)[0]['123'],selection.project(self.page))
        self.assertEqual(matching.recovery_profiles(['123'],str(self.root)),(found,meta,[]))

    def test_pointer_gap_returns_actual_generation(self):
        self.build()
        for hint,stale in [('20261124T010000000000Z',False),('20261124T030000000000Z',True)]:
            _,meta,_=matching.read_matching(self.root,dict(self.pointer,version=hint),['123'])
            self.assertEqual(meta['version'],self.version)
            self.assertEqual(bool(meta.get('stale')),stale)

    def test_unavailable_and_bad_page_preserve_existing_error_contract(self):
        self.assertEqual(matching.recovery_profiles(['123'],str(self.root))[2],['profile_snapshot_unavailable'])
        self.assertFalse(self.cache.exists())
        self.build()
        with closing(sqlite3.connect(self.cache)) as db, db:
            db.execute("UPDATE pages SET projection='{}'")
        self.assertEqual(matching.recovery_profiles(['123'],str(self.root))[2],['profile_invalid:123'])

    def test_streamed_preview_is_valid_json_and_detects_truncation(self):
        path=self.root/'pages.json'
        path.write_text('{"meta":{"page_count":1},"pages":{\n"123":{"page_id":"123"}\n}}\n')
        prepared=PreparedPages(path)
        self.assertEqual(dict(prepared.items()),json.loads(path.read_text())['pages'])
        path.write_text(path.read_text().split('}}\n')[0])
        with self.assertRaises(ValueError):
            list(prepared.items())

    def test_rollback_checks_the_main_api_pid_from_the_current_switch(self):
        script=Path(__file__).resolve().parents[1]/'ops/fb-portrait-consumers/deploy.py'
        spec=importlib.util.spec_from_file_location('portrait_deployer',script)
        deploy=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(deploy)
        dep=self.root/'deployment'
        candidate=dep/'fb/candidate'
        candidate.mkdir(parents=True)
        state=dep/'fb/manifest.json'
        state.write_text(json.dumps({'main_api_pid':'older'}))
        (dep/'fb/schedulers-after.json').write_text('{"ok":true}')
        info={'previous':{'fb':str(self.root/'old')},'controller_sha256':deploy.digest(script)}
        with patch.multiple(deploy,DEP=dep,FILES={'fb':{}},LINKS={'fb':candidate},CONTROLLER=script), \
             patch.object(deploy,'load',return_value=info),patch.object(deploy,'prop',return_value='current'), \
             patch.object(deploy.subprocess,'run') as ctl:
            deploy.change('fb',rollback=True)
        ctl.assert_called_once()
        saved=json.loads(state.read_text())
        self.assertEqual((saved['main_api_pid'],saved['installation_main_api_pid']),('current','older'))


if __name__=='__main__':
    unittest.main()
