"""Operator notifications expose product counts and shared-object impact."""
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from meta_violation_cleanup.runner import Runner
from meta_violation_cleanup.runtime import Runtime


class ReportTests(unittest.TestCase):
    def test_end_report_contains_both_products_shared_outside_ad_and_elapsed(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime = Runtime(folder)
            try:
                runtime.begin('report', 'run', {'execution_started_at': '2026-10-23T10:00:00+08:00',
                    'product_scanned_ads': {'1479': 12, '3302': 7},
                    'candidate_ads': 2, 'rule1_ads': 1, 'rule2_ads': 1,
                    'result_counts': {'creative_api_accepted': 1, 'creative_unknown': 1,
                                      'ad_deleted_verified': 1}})
                runtime.artifact('report', 'manifest.json', {
                    'scope': {'1479': '1479', '3302': '3302'},
                    'candidates': [{'root_id': '1479', 'rules': ['rule1']},
                                   {'root_id': '3302', 'rules': ['rule2']}],
                    'shared_reference_coverage': 'partial',
                    'shared_creative_impacts': {'987': [{'ad_id': '456', 'product_ids': ['9999']}]}})
                runtime.update('report', status='partial')
                runner = Runner({}, None, None, runtime, None, None)
                text = runner.end_text('report')
                for expected in ('DramaWave：已识别广告12', 'HotDrama：已识别广告7',
                                 'Ad 456｜产品 9999（包含范围外产品）', 'API接受1',
                                 '回读确认删除1', '未知1', '耗时：', '状态：部分完成'):
                    self.assertIn(expected, text)
                self.assertIn('启动：', runner.start_text('report', 'start', 'end'))
            finally:
                runtime.db.close()


if __name__ == '__main__':
    unittest.main()
