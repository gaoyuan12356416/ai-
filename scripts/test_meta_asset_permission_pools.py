import copy
import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import meta_asset_permission_sync as m
import meta_account_pool_permission_sync as p


class PoolTests(unittest.TestCase):
    def test_pool_scope_dedup_and_removed_rows(self):
        rows=[{'group_id':484,'platform':0,'deleted_at':0,'account_id':'act_1'},
              {'group_id':567,'platform':0,'deleted_at':0,'account_id':'1'},
              {'group_id':484,'platform':0,'deleted_at':3,'account_id':'act_2'},
              {'group_id':999,'platform':0,'deleted_at':0,'account_id':'act_3'},
              {'group_id':567,'platform':1,'deleted_at':0,'account_id':'act_4'}]
        result=p.active_members(rows,[484,567])
        self.assertEqual(set(result),{'1'})
        self.assertEqual(len(result['1']),2)

    def recovery(self,d):
        parent=Path(d)/'parent';parent.mkdir()
        (parent/'manifest.json').write_text('{}')
        digest=hashlib.sha256((parent/'manifest.json').read_bytes()).hexdigest()
        j=m.Journal(parent,digest)
        j.append('intent',key='page:123',actor={'credential_id':1,'fingerprint':'fp'},target_id='target')
        paused=j.append('bm_paused',key='page:123')
        return {'permission_profile':'basic_ads','input_scope':{'type':'account_pools','pool_ids':[484,567]},
                'assets':[{'kind':'account','asset_id':'456','pool_ids':[567]}],
                'recovery':{'prior_run_dir':str(parent),'prior_manifest_sha256':digest,
                  'restricted_at':paused['at'],'actor_credential_id':1,'actor_fingerprint':'fp','target_id':'target',
                  'key':'account:456','prior_restricted_key':'page:123','mode':'explicit_account_pool_probe'}}

    def test_new_pool_account_probe_preserves_actor_and_parent(self):
        with tempfile.TemporaryDirectory() as d:
            manifest=self.recovery(d)
            self.assertEqual(m.validate_recovery(manifest)['key'],'account:456')
            manifest['recovery']['actor_credential_id']=2
            with self.assertRaisesRegex(ValueError,'actor_or_target'):m.validate_recovery(manifest)

    def test_probe_cannot_expand_to_pages_or_another_pool(self):
        with tempfile.TemporaryDirectory() as d:
            original=self.recovery(d)
            for asset in [{'kind':'page','asset_id':'123','pool_ids':[567]},
                          {'kind':'account','asset_id':'456','pool_ids':[999]}]:
                manifest=copy.deepcopy(original);manifest['assets']=[asset]
                with self.assertRaisesRegex(ValueError,'invalid_scoped'):m.validate_recovery(manifest)
            manifest=copy.deepcopy(original);manifest['recovery']['key']='account:999'
            with self.assertRaisesRegex(ValueError,'invalid_scoped'):m.validate_recovery(manifest)

    def test_probe_requires_explicit_mode_and_basic_profile(self):
        with tempfile.TemporaryDirectory() as d:
            original=self.recovery(d)
            manifest=copy.deepcopy(original);manifest['recovery'].pop('mode')
            with self.assertRaisesRegex(ValueError,'invalid_scoped'):m.validate_recovery(manifest)
            original['permission_profile']='full_control'
            with self.assertRaisesRegex(ValueError,'invalid_scoped'):m.validate_recovery(original)

    def test_membership_is_rechecked_before_write(self):
        asset={'asset_id':'456','pool_ids':[567,484]}
        for rows,expected in [([],False),([{'platform':0,'deleted_at':1,'account_status':1}],False),
                              ([{'platform':0,'deleted_at':0,'account_status':2}],False),
                              ([{'platform':0,'deleted_at':0,'account_status':1}],True)]:
            self.assertEqual(m.pool_membership_current(SimpleNamespace(query=lambda *a:rows),asset),expected)


if __name__=='__main__': unittest.main()
