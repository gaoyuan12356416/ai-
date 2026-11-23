import copy
import unittest
from features.gpu_offload.media import clamp_capacity


class MediaIsolationTests(unittest.TestCase):
    def test_coordinator_cannot_start_publishers_or_exceed_local_budget(self):
        response = {'capacity': {'prepare': {'target': 20, 'ceiling': 20},
                                 'publish': {'target': 8, 'ceiling': 8}}}
        saved = copy.deepcopy(response)
        self.assertEqual(clamp_capacity(response, 1)['capacity']['prepare']['target'], 1)
        self.assertEqual(clamp_capacity(response, 2)['capacity']['publish']['target'], 0)
        self.assertEqual(response, saved)

    def test_admission_zero_is_preserved(self):
        response = {'capacity': {'prepare': {'target': 0}, 'publish': {'target': 0}}}
        self.assertEqual(clamp_capacity(response, 2)['capacity']['prepare']['target'], 0)


if __name__ == '__main__':
    unittest.main()
