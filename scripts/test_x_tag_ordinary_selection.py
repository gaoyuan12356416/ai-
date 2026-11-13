"""Normal profile templates have no hit scope; audit must still reserve once."""
import sys, threading, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from features.x_tag_publish.selection import AuditStore

class Tests(unittest.TestCase):
    def context(self, hit):
        local=threading.local();decision=SimpleNamespace(audit=Mock(return_value={'policy_version':'test'}))
        local.value=dict(catalog={},decision=decision,language='en',fallback='',hit=hit,now='test-now')
        store=SimpleNamespace(reserve_material=Mock(return_value={'reserved':True}))
        return AuditStore(store,local),store,local,decision
    def test_ordinary_template_with_null_hit_reserves_and_keeps_audit(self):
        audit,store,local,decision=self.context(None)
        self.assertEqual(audit.reserve_material(content_id='drama',selection_snapshot={'old':'kept'}),{'reserved':True})
        store.reserve_material.assert_called_once()
        snapshot=store.reserve_material.call_args.kwargs['selection_snapshot']
        self.assertEqual(snapshot,{'old':'kept','tag_profile':{'policy_version':'test'}})
        self.assertEqual(local.value['selected_audit'],snapshot['tag_profile'])
    def test_empty_hit_keeps_ordinary_admission(self):
        audit,store,_,_=self.context({});audit.reserve_material(content_id='drama')
        self.assertNotIn('hit_publish_timing',store.reserve_material.call_args.kwargs['selection_snapshot'])
    def test_explicit_hit_preserves_frozen_timing(self):
        audit,store,_,_=self.context({'timing_policy':'frozen','release_times':{'key':1}})
        with patch('features.hit_publish.timing',return_value={'publish_at_utc':'frozen-time'}) as timing:
            audit.reserve_material(content_id='drama')
        timing.assert_called_once_with({'key':1},'drama','en','test-now')
        self.assertEqual(store.reserve_material.call_args.kwargs['selection_snapshot']['hit_publish_timing'],{'publish_at_utc':'frozen-time'})
    def test_underlying_reservation_conflict_is_not_swallowed_or_retried(self):
        audit,store,_,_=self.context(None);store.reserve_material.side_effect=RuntimeError('reservation conflict')
        with self.assertRaisesRegex(RuntimeError,'reservation conflict'):audit.reserve_material(content_id='drama')
        store.reserve_material.assert_called_once()
if __name__=='__main__':unittest.main()
