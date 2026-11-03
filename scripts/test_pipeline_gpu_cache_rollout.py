import unittest
from scripts.enable_pipeline_gpu_asset_cache import verify_drain_contract, DRAIN_RELEASES


class CacheRolloutGuardTests(unittest.TestCase):
    def test_only_reviewed_infinite_graceful_shutdown_can_be_used(self):
        info = {'sha': next(iter(DRAIN_RELEASES)), 'release': '/opt/verified'}
        props = {'KillMode': 'mixed', 'TimeoutStopUSec': 'infinity', 'ActiveState': 'active', 'MainPID': '123'}
        argv = ['python', '/opt/verified/scripts/pipeline_worker.py']
        verify_drain_contract(info, props, argv)
        for change in ({'KillMode': 'control-group'}, {'TimeoutStopUSec': '90s'}, {'ActiveState': 'deactivating'}, {'MainPID': '0'}):
            with self.assertRaises(ValueError): verify_drain_contract(info, {**props, **change}, argv)
        with self.assertRaises(ValueError): verify_drain_contract({**info, 'sha': 'a'*40}, props, argv)
        with self.assertRaises(ValueError): verify_drain_contract(info, props, ['python', '/other/worker.py'])


if __name__ == '__main__': unittest.main()
