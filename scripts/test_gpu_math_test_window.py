import unittest
from unittest.mock import patch
from scripts import gpu_math_test_window as window


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.before={'target':16,'ceiling':16,'scaling_mode':'manual'}
        self.paused={'target':0,'ceiling':0,'scaling_mode':'manual'}
        self.record={'before_pool':self.before,'paused_pool':self.paused}

    def snapshot(self,pool,version=8):
        return {'version':version,'config':{'hosts':{'hk-gpu':{'prepare':pool},
            'cpu-main':{'prepare':{'target':7}},'cos-cloud':{'prepare':{'target':50}}}}}

    def test_restores_only_pool_with_current_version(self):
        with patch.object(window,'snapshot',side_effect=[self.snapshot(self.paused),self.snapshot(self.before,9)]),patch.object(window,'change') as change:
            self.assertEqual(window.restore(self.record),'restored')
            change.assert_called_once_with(8,self.before)

    def test_preserves_new_external_capacity_choice(self):
        new=dict(self.before,target=12,ceiling=12)
        with patch.object(window,'snapshot',return_value=self.snapshot(new)),patch.object(window,'change') as change:
            self.assertEqual(window.restore(self.record),'pool_changed_externally_preserved')
            change.assert_not_called()

    def test_idempotent_after_prior_restore(self):
        with patch.object(window,'snapshot',return_value=self.snapshot(self.before)),patch.object(window,'change') as change:
            self.assertEqual(window.restore(self.record),'already_restored')
            change.assert_not_called()


if __name__=='__main__':unittest.main()
