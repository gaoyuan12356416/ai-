from copy import deepcopy
from datetime import datetime, timedelta
import unittest

from meta_violation_cleanup.handoff import derive_with_backlog
from meta_violation_cleanup.runtime import CST, digest


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.at = datetime.now(CST)
        self.basis = dict(ad_id='11', creative_id='22', account_id='33', user_id='44',
                          root_id='1479', eligible=True, material_ids=['55'])
        self.origin = dict(run_id='old', policy_version='20261024-v2', root_products=['1479','3302'],
                           candidates=[self.basis], evidence={'materials': {'55': {'count': 1}}})
        self.origin['sha256'] = digest(self.origin)
        self.entry = dict(basis=deepcopy(self.basis), origin_run_id='old',
                          origin_manifest_sha256=self.origin['sha256'], evidence=deepcopy(self.origin['evidence']))
        self.source = dict(run_id='preview', scanner='material_indexed', policy_version='20261024-v2',
                           root_products=['1479','3302'], candidates=[], recovery_creatives=[],
                           frozen_at=(self.at-timedelta(minutes=20)).isoformat(),
                           window_start='original-start', window_end='original-end', evidence={'materials': {}})
        self.seal()

    def seal(self):
        self.source['sha256'] = digest({k:v for k,v in self.source.items() if k!='sha256'})

    def derive(self, backlog=None):
        return derive_with_backlog(self.source, 'derived', [self.entry] if backlog is None else backlog,
                                   lambda rid: self.origin, at=self.at)

    def test_late_backlog_is_inherited_without_new_ads_or_renewed_expiry(self):
        before = deepcopy(self.source)
        result = self.derive()
        self.assertEqual(result['recovery_creatives'], [self.entry])
        self.assertEqual(result['candidates'], [])
        self.assertEqual(self.source, before)
        for field in ('frozen_at','window_start','window_end','evidence'):
            self.assertEqual(result[field], before[field])
        self.assertEqual(result['handoff']['added_creative_ids'], ['22'])
        self.assertNotEqual(result['sha256'], before['sha256'])

    def test_expired_preview_cannot_be_refreshed_by_backlog(self):
        self.source['frozen_at'] = (self.at-timedelta(hours=2)).isoformat();self.seal()
        with self.assertRaisesRegex(ValueError,'manifest_expired'):self.derive()

    def test_origin_hash_is_required(self):
        self.entry['origin_manifest_sha256'] = 'wrong'
        with self.assertRaisesRegex(ValueError,'origin_hash'):self.derive()

    def test_changed_account_or_material_basis_is_rejected(self):
        self.entry['basis']['account_id'] = '99'
        with self.assertRaisesRegex(ValueError,'basis_mismatch'):self.derive()

    def test_forged_evidence_is_rejected(self):
        self.entry['evidence']['materials']['55']['count'] = 4
        with self.assertRaisesRegex(ValueError,'evidence_mismatch'):self.derive()

    def test_existing_creative_is_not_duplicated(self):
        self.source['recovery_creatives']=[self.entry];self.seal()
        self.assertEqual(self.derive()['handoff']['added_creative_ids'],[])

    def test_comparison_preview_cannot_be_promoted(self):
        self.source['comparison_only']=True;self.seal()
        with self.assertRaisesRegex(ValueError,'scope_invalid'):self.derive()

    def test_duplicate_queue_identity_fails_closed(self):
        with self.assertRaisesRegex(ValueError,'duplicate_backlog'):self.derive([self.entry,self.entry])

    def test_old_origin_expiry_does_not_drop_queued_work(self):
        self.origin['frozen_at']=(self.at-timedelta(days=40)).isoformat()
        self.origin['sha256']=digest({k:v for k,v in self.origin.items() if k!='sha256'})
        self.entry['origin_manifest_sha256']=self.origin['sha256']
        self.assertEqual(len(self.derive()['recovery_creatives']),1)


if __name__ == '__main__':
    unittest.main()
