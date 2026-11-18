import copy
import json
import re
import unittest

from features.post_daily_report.report import build_card, build_compact_card, preview_html


def visible(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key == 'content' and isinstance(item, str):
                yield re.sub('<[^>]+>', '', item)
            else:
                yield from visible(item)
    elif isinstance(value, list):
        for item in value:
            yield from visible(item)


class PresentationTests(unittest.TestCase):
    def report(self):
        return {'date': '2026-11-17', 'channels': [{'channel': 'FB', 'accounting': 'actual-fb-v2',
            'rows': [{'source': 'auto_template', 'expected': 10, 'published': 4, 'late': 1,
                      'blocked': 2, 'unknown': 1, 'policy_skipped': 2,
                      'reasons': [{'code': 'fb_auto_page_language_missing', 'count': 2},
                                  {'code': 'unknown_outcome', 'count': 1}],
                      'policy_reasons': [{'code': 'fb_auto_drama_cooldown_at_publish', 'count': 2}]},
                     {'source': 'video:pool', 'expected': 0}]}]}

    def test_counts_do_not_mix_success_unknown_and_skips_or_change_input(self):
        report = self.report()
        before = copy.deepcopy(report)
        card = build_card(report)
        self.assertEqual(report, before)
        platform = card['elements'][3]['columns']
        self.assertEqual([list(visible(c))[0] for c in platform], ['**FB**', '10', '**5**', '3', '2'])
        self.assertNotIn('主页素材池', '\n'.join(visible(card)))
        self.assertIn('待确认 1', '\n'.join(visible(card)))

    def test_expanded_summary_explains_causes_and_details_have_no_error_codes(self):
        card = build_card(self.report())
        main = '\n'.join(e['content'] for e in card['elements'] if e['tag'] == 'markdown')
        self.assertIn('主页未设置语言 **2** 条', main)
        self.assertNotIn('fb_auto_page_language_missing', json.dumps(card))
        self.assertEqual(set(re.findall('[A-Za-z_]+', '\n'.join(visible(card)))), {'FB'})
        panels = [e for e in card['elements'] if e['tag'] == 'collapsible_panel']
        self.assertTrue(all(not e['expanded'] for e in panels))
        self.assertIn('<details>', preview_html(self.report(), card))

    def test_unknown_code_does_not_turn_into_a_guessed_cause(self):
        report = self.report()
        report['channels'][0]['rows'][0]['reasons'] = [
            {'code': 'new_secret_error', 'count': 3, 'reason': '原错误码：new_secret_error'}]
        content = '\n'.join(visible(build_card(report)))
        self.assertIn('暂未记录具体原因', content)
        self.assertNotIn('new_secret_error', content)

    def test_missing_channel_keeps_healthy_counts_but_total_unknown(self):
        report = self.report()
        report['channels'].append({'channel': 'X', 'error': 'ReadError', 'rows': []})
        card = build_card(report)
        self.assertTrue(all('未知' in list(visible(c))[0] for c in card['elements'][0]['columns']))
        self.assertIn('确认成功 5', '\n'.join(visible(card)))
        self.assertEqual(card['header']['template'], 'orange')

    def test_cost_unknown_retains_estimate_exclusions_and_original_rate(self):
        report = self.report()
        report['channels'][0]['cost'] = {'display_usd': None, 'known_estimated_usd': '2.00',
                                        'rate_snapshot_date': '2026-10-08', 'unpriced_posts': 1}
        content = '\n'.join(visible(build_card(report)))
        for expected in ['费用估算 · 未知', '2.00 美元', '2026-10-08', '不含读取、上传', '缺少正文 1']:
            self.assertIn(expected, content)

    def test_compact_path_retains_sources_and_marks_omitted_reasons(self):
        report = self.report()
        report['channels'][0]['rows'][0]['reasons'] = [
            {'reason': '原因第%s项' % i, 'count': 1} for i in range(15)]
        content = '\n'.join(visible(build_compact_card(report)))
        self.assertIn('自动模板：成功 **5** / 应发 10', content)
        self.assertIn('另有 11 项原因', content)


if __name__ == '__main__':
    unittest.main()
