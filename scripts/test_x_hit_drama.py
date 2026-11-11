import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from features.x_auto_posts.core import XAutoPostStore
from features.x_auto_posts.publisher import AutoLiveGates, AutoPostExecutor
from features.x_tag_publish.hit_drama import TagAutoPostService, match_reason, normalize, resolve, MARKER
from features.x_tag_publish.policy import Decision
from features.x_tag_revenue.tags import VERSION as TAG_VERSION
from features.x_tag_revenue.hierarchy import VERSION as HIERARCHY_VERSION
from scripts.test_x_auto_post_service import FakeBridge, FakeSelector, FakeExecutor, Clock
from scripts.test_x_auto_post_validation import valid_payload
from scripts import test_x_auto_post_selector as fixture
from features.x_tag_publish.selection import ProfileSelector


def snapshot():
    def profile(audience, theme):
        return dict(status='complete',revenue_cents=100,hierarchy_version=HIERARCHY_VERSION,audiences=[dict(key=audience,revenue_cents=100,share_bp=10000,matchable=True,children=[dict(key=theme,revenue_cents=100,share_bp=10000,matchable=True)])])
    return ({'accounts':{'x-101':profile('female','sweet_romance'),'x-102':profile('male','revenge')}},dict(version='test',tag_mapping_version=TAG_VERSION,hierarchy_version=HIERARCHY_VERSION))


class HitDramaTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=XAutoPostStore(Path(self.temp.name)/'x.sqlite3',now_fn=Clock())
        self.service=TagAutoPostService(self.store,FakeBridge(),FakeSelector(),FakeExecutor(AutoLiveGates(True,True,True)),now_fn=Clock())
        self.actor={'user_id':'803','name':'operator'}
        self.template=self.service.create_template(dict(valid_payload(),_actor=self.actor))['template']
        self.payload=dict(expected_version=1,drama_id='123',id_type='content_id',idempotency_key='operation123',confirmed=True,_actor=self.actor)

    def test_only_matching_channel_created_and_replay_uses_frozen_scope(self):
        catalog={('123','en'):dict(audience='female',themes=['sweet_romance'])}
        with patch('features.x_tag_publish.hit_drama.resolve',return_value=dict(content_id='123',languages=['en'])),patch('features.x_tag_publish.hit_drama.catalog_labels',return_value=catalog),patch('features.x_tag_publish.hit_drama.portrait_snapshot',return_value=snapshot()):
            result=self.service.run_now(self.template['id'],self.payload)
        self.assertEqual(result['matched_channels'],1)
        self.assertEqual([t.account_id for t in self.store.list_tasks(run_id=result['run_id'])],['101'])
        with patch('features.x_tag_publish.hit_drama.resolve',side_effect=AssertionError('must replay receipt')):
            again=self.service.run_now(self.template['id'],self.payload)
        self.assertEqual(again['run_id'],result['run_id']);self.assertTrue(again['idempotent'])
        with self.assertRaisesRegex(Exception,'操作标识'):
            self.service.run_now(self.template['id'],dict(self.payload,drama_id='124'))

    def test_missing_portrait_never_falls_back_to_all_channels(self):
        with patch('features.x_tag_publish.hit_drama.resolve',return_value=dict(content_id='123',languages=['en'])),patch('features.x_tag_publish.hit_drama.catalog_labels',return_value={}),patch('features.x_tag_publish.hit_drama.portrait_snapshot',return_value=({'accounts':{}},{'unavailable':True})):
            with self.assertRaisesRegex(Exception,'没有语言'):
                self.service.run_now(self.template['id'],self.payload)
        self.assertEqual(len(self.store.list_tasks()),0)

    def test_exact_target_survives_claim_wrapper_and_never_selects_other_drama(self):
        source=fixture.FakeSource([fixture.drama('C1'),fixture.drama('C2')],{'C1':[fixture.material('101','C1')],'C2':[fixture.material('201','C2')]})
        store=fixture.FakeStore();store.get_run=lambda _:SimpleNamespace(blacklist_snapshot={MARKER:dict(account_ids=['101'],target=dict(content_id='C1'))})
        selector=ProfileSelector(source,fixture.MetricWindowRepository(fixture.FakeMetricStore()),fixture.FakePublishedHistory(),store,material_validator=fixture.FakeValidator())
        executor=object.__new__(AutoPostExecutor);executor.store=store;executor.selector=selector
        account=dict(id=101,x_user_id='x-101',drama_language='en')
        with patch.dict(os.environ,X_POST_AUTOMATION_INTERNAL_URL='http://127.0.0.1:8821',X_POST_AUTO_INTERNAL_TOKEN='a'*48),patch('features.x_tag_publish.selection.XPostAutoBridgeClient') as bridge,patch('features.x_tag_publish.policy.portrait_snapshot',return_value=snapshot()),patch('features.x_tag_publish.policy.catalog_labels',return_value={('C1','en'):dict(audience='female',themes=['sweet_romance'])}):
            bridge.return_value.account_snapshot.return_value=account
            selected=executor._phase_selector('claim').select_and_reserve(fixture.request())
        self.assertEqual(selected.drama.content_id,'C1');self.assertEqual(source.material_calls,[('C1','en')])

    def test_match_needs_both_positive_audience_and_theme(self):
        decision=Decision(dict(id=101,x_user_id='x-101'),'test',snapshot())
        self.assertFalse(match_reason(decision,dict(audience='female',themes=['sweet_romance'])))
        self.assertTrue(match_reason(decision,dict(audience='female',themes=['revenge'])))
        self.assertTrue(match_reason(decision,dict(audience='male',themes=['sweet_romance'])))

    def test_resource_mapping_is_product_scoped_and_ambiguous_rejected(self):
        source=SimpleNamespace(schema='ads',app_id=1479)
        calls=[]
        source._stream_select=lambda sql,args:(calls.append((sql,args)) or ([{'content_id':'123'}] if 'resource' in sql else [{'content_id':'123','language':'en'}]))
        self.assertEqual(resolve(source,dict(id_type='resource_id',drama_id='456'))['content_id'],'123')
        self.assertIn('app_id=%s',calls[0][0]);self.assertEqual(calls[0][1],('456',1479))
        source._stream_select=lambda *_:[{'content_id':'123'},{'content_id':'124'}]
        with self.assertRaises(Exception):resolve(source,dict(id_type='resource_id',drama_id='456'))


if __name__=='__main__':unittest.main()
