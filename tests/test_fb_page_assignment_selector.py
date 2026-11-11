import copy
import unittest

from features.fb_page_assignment.selector import assign


def page(pid, language='en', active=0, fans=0, **extra):
    return dict(page_id=str(pid), language=language, status=0, forbidden_status=0,
                is_permission=1, active_ad_count=active, fan_ccount=fans, updated_at='2026-11-11', **extra)


def portrait(audience='female', themes=None, status='ready'):
    return dict(status=status, audiences=[dict(key=audience, share_bp=10000, matchable=audience != '__unknown__',
                children=[dict(key=key, share_bp=share, matchable=key != '__unknown__')
                          for key, share in (themes or {'sweet_romance': 10000}).items()])])


def drama(language='en', audience='female', themes=None, **extra):
    return dict(exists=True, language=language, audience=audience,
                themes=themes if themes is not None else ['sweet_romance'], classification={}, warnings=[], **extra)


class AssignmentSelectorTests(unittest.TestCase):
    def choose(self, pages, profiles=None, value=None):
        return assign(['d'], {'d': value or drama()}, pages, profiles or {})[0]

    def test_profile_priority_over_unprofiled_and_lower_load_fallback(self):
        result = self.choose([page(1, active=90), page(2), page(3, language='')],
                             {'1': portrait(), '3': portrait()})
        self.assertEqual((result['page_id'], result['selection_stage']), ('1', 'language_profile'))

    def test_secondary_score_uses_parent_branch_and_only_highest_three(self):
        pages = [page(1, active=9), page(2, active=8), page(3, active=7), page(4)]
        profiles = {str(i): portrait(themes={'sweet_romance': 10000-i*1000}) for i in range(1, 5)}
        result = self.choose(pages, profiles)
        self.assertEqual(result['page_id'], '3')
        self.assertEqual(result['diagnostics']['stages'][0]['eligible_count'], 4)
        self.assertEqual(result['diagnostics']['stages'][0]['candidate_count'], 3)
        self.assertEqual(result['diagnostics']['candidates'][3]['stage_exclusions']['language_profile'], 'outside_top_three')

    def test_multiple_drama_themes_sum_once(self):
        result = self.choose([page(1)], {'1': portrait(themes={'sweet_romance': 3000, 'marital': 5000})},
                             drama(themes=['sweet_romance', 'marital', 'marital']))
        self.assertEqual(result['diagnostics']['selected']['match_score_bp'], 8000)

    def test_known_secondary_direction_is_not_a_match_or_unprofiled(self):
        profile = portrait('male')
        profile['audiences'][0]['share_bp'] = 6000
        profile['audiences'].extend(portrait('female')['audiences'])
        profile['audiences'][1]['share_bp'] = 4000
        result = self.choose([page(1), page(2, active=10)], {'1': profile})
        self.assertEqual((result['page_id'], result['selection_stage']), ('2', 'language_unprofiled'))
        self.assertEqual(result['diagnostics']['candidates'][0]['match_reason'], 'dominant_audience_mismatch')

    def test_tied_dominant_audiences_match_including_known_unknown_tie(self):
        for other in ('male', '__unknown__'):
            profile = portrait('female')
            profile['audiences'].extend(portrait(other)['audiences'])
            for node in profile['audiences']:
                node['share_bp'] = 5000
            self.assertEqual(self.choose([page(1)], {'1': profile})['selection_stage'], 'language_profile')

    def test_unknown_dominant_and_nonpositive_profiles_are_unprofiled(self):
        for profile in (portrait('__unknown__'), portrait(status='no_income'), portrait(status='signed_adjustment')):
            self.assertEqual(self.choose([page(1)], {'1': profile})['selection_stage'], 'language_unprofiled')

    def test_all_six_stage_priorities(self):
        cases = [
            ([page(1)], {'1': portrait()}, 'language_profile'),
            ([page(1)], {}, 'language_unprofiled'),
            ([page(1, language='')], {'1': portrait()}, 'fallback_profile'),
            ([page(1, language='')], {}, 'fallback_unprofiled'),
            ([page(1)], {'1': portrait('male')}, 'language_relaxed'),
            ([page(1, language='')], {'1': portrait('male')}, 'fallback_relaxed'),
        ]
        for pages, profiles, stage in cases:
            with self.subTest(stage=stage):
                result = self.choose(pages, profiles)
                self.assertEqual(result['selection_stage'], stage)
                self.assertEqual(result['status'], 'degraded' if stage.endswith('_relaxed') else 'assigned')
                self.assertEqual(result['error_code'], 'profile_relaxed' if stage.endswith('_relaxed') else None)

    def test_blank_pool_matching_and_unknown_precede_language_relaxation(self):
        for profiles, stage in (({'1': portrait('male'), '2': portrait()}, 'fallback_profile'),
                                ({'1': portrait('male')}, 'fallback_unprofiled')):
            result = self.choose([page(1), page(2, language='', active=10)], profiles)
            self.assertEqual((result['page_id'], result['selection_stage']), ('2', stage))

    def test_unknown_drama_language_never_borrows_a_labeled_page(self):
        result = self.choose([page(1), page(2, language='', active=10)], value=drama(language=''))
        self.assertEqual((result['page_id'], result['selection_stage']), ('2', 'fallback_unprofiled'))
        self.assertIn('drama_language_missing', result['diagnostics']['warnings'])

    def test_only_other_language_available_fails_even_after_relaxation(self):
        result = self.choose([page(1, language='fr')])
        self.assertEqual(result['error_code'], 'no_available_page')
        self.assertIsNone(result['page_id'])

    def test_status_permission_exclusions_survive_all_fallbacks(self):
        pages = [page(1), page(2), page(3)]
        pages[0]['status'] = 1
        pages[1]['forbidden_status'] = 1
        pages[2]['is_permission'] = 0
        result = self.choose(pages)
        self.assertEqual(result['error_code'], 'no_available_page')
        self.assertTrue(all(not p['available'] for p in result['diagnostics']['candidates']))

    def test_batch_load_spreads_duplicates_and_allows_reuse(self):
        result = assign(['d'] * 4, {'d': drama()}, [page(1), page(2)], {})
        self.assertEqual([r['page_id'] for r in result], ['1', '2', '1', '2'])
        self.assertEqual([r['diagnostics']['selected']['batch_assigned_count'] for r in result], [0, 0, 1, 1])
        self.assertEqual([r['drama_id'] for r in result], ['d'] * 4)

    def test_metric_order_known_before_missing_then_numeric_id(self):
        cases = [([page(1, active=None), page(2, active=100)], '2'),
                 ([page(1, fans=None), page(2, fans=100)], '2'),
                 ([page(10), page(2)], '2'),
                 ([page(1, active=1, fans=0), page(2, active=0, fans=100)], '2'),
                 ([page(1, fans=-1), page(2, fans=100)], '2')]
        for pages, expected in cases:
            self.assertEqual(self.choose(pages)['page_id'], expected)

    def test_missing_load_still_spreads_within_unknown_metric_tier(self):
        result = assign(['d'] * 3, {'d': drama()}, [page(1, active=None), page(2, active=None)], {})
        self.assertEqual([r['page_id'] for r in result], ['1', '2', '1'])
        self.assertTrue(all(r['diagnostics']['selected']['adjusted_active_ad_count'] is None for r in result))

    def test_score_ties_cutoff_use_load_fans_and_numeric_id(self):
        pages = [page(10), page(2), page(3), page(4)]
        profiles = {p['page_id']: portrait() for p in pages}
        result = self.choose(pages, profiles)
        self.assertEqual(result['diagnostics']['stages'][0]['candidate_page_ids'], ['2', '3', '4'])

    def test_missing_and_malformed_drama_are_isolated(self):
        dramas = {'bad': drama(themes=42), 'good': drama(), 'absent': {'exists': False}}
        result = assign(['missing', 'bad', 'absent', 'good'], dramas, [page(1)], {})
        self.assertEqual([r['error_code'] for r in result], ['drama_not_found', 'selection_error', 'drama_not_found', None])
        self.assertEqual(result[-1]['page_id'], '1')
        self.assertEqual(result[-1]['diagnostics']['selected']['batch_assigned_count'], 0)

    def test_general_and_lgbtq_keep_their_own_direction(self):
        for audience in ('general', 'lgbtq'):
            result = self.choose([page(1)], {'1': portrait(audience)}, drama(audience=audience))
            self.assertEqual(result['selection_stage'], 'language_profile')

    def test_known_portrait_without_matching_themes_requires_relaxation(self):
        result = self.choose([page(1)], {'1': portrait(themes={'__unknown__': 10000})})
        self.assertEqual(result['selection_stage'], 'language_relaxed')

    def test_normalized_languages_and_no_mutation_or_cross_call_reservation(self):
        inputs = (['d', 'd'], {'d': drama(language='JP')}, [page(1, language='ja'), page(2, language='ja')], {})
        before = copy.deepcopy(inputs)
        first, second = assign(*inputs), assign(*inputs)
        self.assertEqual(first, second)
        self.assertEqual(inputs, before)
        self.assertEqual([r['page_id'] for r in first], ['1', '2'])


if __name__ == '__main__':
    unittest.main()
