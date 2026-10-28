import copy
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from meta_violation_cleanup.cover_payload import (
    PayloadError, build_payload, creation_payload, payload_key, verify_creative, verify_ad_binding,
)


def video():
    return {
        'id': '20', 'account_id': '100', 'name': 'original', 'object_type': 'VIDEO',
        'status': 'ACTIVE', 'video_id': '30', 'effective_object_story_id': '40_50',
        'url_tags': 'utm_source=a&campaign={{campaign.id}}',
        'degrees_of_freedom_spec': {'creative_features_spec': {
            'standard_enhancements': {'enroll_status': 'OPT_OUT'},
            'text_optimizations': {'enroll_status': 'OPT_OUT'}}},
        'regional_regulation_disclaimer_spec': {'taiwan_finserv_beneficiary': '60'},
        'object_story_spec': {'page_id': '40', 'instagram_user_id': '70', 'video_data': {
            'video_id': '30', 'image_url': 'https://old.example/thumb.jpg',
            'title': '标题', 'message': '正文', 'link_description': '描述',
            'call_to_action': {'type': 'INSTALL_MOBILE_APP', 'value': {
                'link': 'https://apps.example/app?x=1&sub_user_id=803',
                'app_link': 'dramawave://drama/abc?channel=FB'}},
        }},
    }


class PayloadTests(unittest.TestCase):
    def test_approved_video_crop_absence_for_image_does_not_allow_other_losses(self):
        old = video()
        old['degrees_of_freedom_spec']['creative_features_spec']['video_auto_crop'] = {'enroll_status':'OPT_IN'}
        expected = build_payload(old,'https://cdn.example/new.jpg')
        actual = copy.deepcopy(expected)
        actual['degrees_of_freedom_spec']['creative_features_spec'].pop('video_auto_crop')
        verify_creative(actual, expected)
        self.assertIn('video_auto_crop',expected['degrees_of_freedom_spec']['creative_features_spec'])
        for mutate in (
            lambda x: x['degrees_of_freedom_spec']['creative_features_spec'].pop('text_optimizations'),
            lambda x: x['degrees_of_freedom_spec']['creative_features_spec'].update(video_auto_crop={'enroll_status':'OPT_OUT'}),
        ):
            changed=copy.deepcopy(actual); mutate(changed)
            with self.assertRaises(PayloadError): verify_creative(changed,expected)

    def test_binding_accepts_only_verified_derived_story_post_changes(self):
        old = {'id':'1', 'creative':{'id':'20'}, 'tracking_specs':[
            {'action.type':['offsite_conversion'], 'fb_pixel':['800']},
            {'action.type':['onsite_conversion'], 'conversion_id':['900']},
            {'action.type':['post_engagement'], 'post':['50'], 'page':['40']},
            {'action.type':['link_click'], 'post':['50'], 'post.wall':['40']}]}
        new = copy.deepcopy(old); new['creative'] = {'id':'21'}
        for row in new['tracking_specs']:
            if 'post' in row: row['post'] = ['51']
        new['tracking_specs'].reverse()
        proof = {'old_story_id':'40_50','new_story_id':'40_51'}
        verify_ad_binding(new, old, '21', **proof)
        mixed = copy.deepcopy(new)
        mixed['tracking_specs'][0]['post'] = ['50']
        verify_ad_binding(mixed, old, '21', **proof)
        old_posts = copy.deepcopy(old); old_posts['creative'] = {'id':'21'}
        old_posts['tracking_specs'].reverse()
        verify_ad_binding(old_posts,old,'21',**proof)
        with self.assertRaises(PayloadError): verify_ad_binding(new, old, '21')
        for mutate in (
            lambda x: x['tracking_specs'][-1].update(fb_pixel=['801']),
            lambda x: x['tracking_specs'][-2].update(conversion_id=['901']),
            lambda x: x['tracking_specs'][0].update(post=['52']),
            lambda x: x['tracking_specs'][0].update(**{'post.wall':['41']}),
            lambda x: x['tracking_specs'].pop(),
            lambda x: x['tracking_specs'].append(copy.deepcopy(x['tracking_specs'][0])),
        ):
            changed=copy.deepcopy(new); mutate(changed)
            with self.assertRaises(PayloadError): verify_ad_binding(changed,old,'21',**proof)
        with self.assertRaises(PayloadError):
            verify_ad_binding(new,old,'21',old_story_id='40_50',new_story_id='41_51')

    def test_observed_generated_name_and_retired_aggregate_readback(self):
        expected = build_payload(video(), 'https://cdn.example/new.jpg')
        frozen = copy.deepcopy(expected)
        actual = creation_payload(expected)
        actual['name'] += ' 2026-10-28-9f481f0cef7f198ee5c3ccf7957fb7b4'
        verify_creative(actual, expected)
        self.assertEqual(expected, frozen)
        for field, value in [('name', 'unrelated'), ('name', expected['name'] + ' arbitrary')]:
            changed = copy.deepcopy(actual); changed[field] = value
            with self.assertRaises(PayloadError): verify_creative(changed, expected)
        for mutate in (
            lambda x: x['object_story_spec']['link_data'].update(name='changed headline'),
            lambda x: x['object_story_spec']['link_data'].update(link='https://other.example'),
            lambda x: x['object_story_spec']['link_data'].update(message='changed copy'),
            lambda x: x['object_story_spec'].update(page_id='999'),
            lambda x: x['degrees_of_freedom_spec']['creative_features_spec']['text_optimizations'].update(enroll_status='OPT_IN'),
            lambda x: x['degrees_of_freedom_spec']['creative_features_spec'].pop('text_optimizations'),
            lambda x: x['degrees_of_freedom_spec']['creative_features_spec'].update(standard_enhancements={'enroll_status':'OPT_IN'}),
        ):
            changed = copy.deepcopy(actual); mutate(changed)
            with self.assertRaises(PayloadError): verify_creative(changed, expected)

    def test_creation_projection_omits_only_readonly_and_legacy_aggregate(self):
        old = video(); old['enable_direct_install'] = False
        old['enable_launch_instant_app'] = False
        old['creative_sourcing_spec'] = {'destination_screenshot_spec': {'enroll_status': 'OPT_OUT'}}
        features = old['degrees_of_freedom_spec']['creative_features_spec']
        features.update(enhance_cta={'enroll_status': 'OPT_IN'},
                        video_auto_crop={'enroll_status': 'OPT_IN'})
        expected = build_payload(old, 'https://cdn.example/new.jpg')
        frozen = copy.deepcopy(expected); before_key = payload_key('100', expected)
        wire = creation_payload(expected)
        self.assertEqual(expected, frozen)
        self.assertEqual(payload_key('100', expected), before_key)
        wanted = copy.deepcopy(expected); wanted.pop('enable_direct_install')
        wanted['degrees_of_freedom_spec']['creative_features_spec'].pop('standard_enhancements')
        self.assertEqual(wire, wanted)
        self.assertEqual(wire['creative_sourcing_spec'], expected['creative_sourcing_spec'])
        self.assertEqual(wire['enable_launch_instant_app'], False)
        # Readback remains the complete original expectation, not the wire.
        with self.assertRaises(PayloadError): verify_creative(wire, expected)
        verify_creative(copy.deepcopy(expected), expected)

    def test_enabled_direct_install_and_umbrella_only_fail_preflight(self):
        for value in (True, 0, None, 'false'):
            old = video(); old['enable_direct_install'] = value
            if value is None:
                expected = {'enable_direct_install': value}
                with self.assertRaises(PayloadError): creation_payload(expected)
            else:
                with self.subTest(value=value), self.assertRaisesRegex(PayloadError, 'direct_install'):
                    build_payload(old, 'https://cdn.example/new.jpg')
        for individual in ({}, {'text_optimizations': {}}, {'text_optimizations': False}):
            old = video()
            old['degrees_of_freedom_spec']['creative_features_spec'] = {
                'standard_enhancements': {'enroll_status': 'OPT_IN'}, **individual}
            with self.subTest(individual=individual), self.assertRaisesRegex(PayloadError, 'legacy_enhancements'):
                build_payload(old, 'https://cdn.example/new.jpg')

    def test_video_preserves_business_fields_and_removes_actual_video(self):
        old = video(); frozen = copy.deepcopy(old)
        payload = build_payload(old, 'https://cdn.example/drama/abc.jpg')
        self.assertEqual(old, frozen)
        story = payload['object_story_spec']; link = story['link_data']
        self.assertNotIn('video_data', story)
        self.assertNotIn('video_id', payload)
        self.assertEqual(payload['name'], 'original')
        self.assertEqual(link['picture'], 'https://cdn.example/drama/abc.jpg')
        self.assertEqual(link['name'], '标题'); self.assertEqual(link['description'], '描述')
        self.assertEqual(link['message'], '正文')
        self.assertEqual(link['call_to_action'], frozen['object_story_spec']['video_data']['call_to_action'])
        self.assertEqual(link['link'], frozen['object_story_spec']['video_data']['call_to_action']['value']['link'])
        self.assertEqual(payload['url_tags'], frozen['url_tags'])
        self.assertEqual(payload['degrees_of_freedom_spec'], frozen['degrees_of_freedom_spec'])
        self.assertEqual(story['instagram_user_id'], '70')

    def test_single_image_preserves_caption_link_and_cta(self):
        old = {'object_story_spec': {'page_id': '1', 'link_data': {
            'image_hash': 'oldhash', 'picture': 'https://old.example/a.jpg',
            'link': 'https://landing.example/?x=1', 'caption': 'caption',
            'name': 'title', 'message': 'body', 'description': 'desc',
            'call_to_action': {'type': 'LEARN_MORE', 'value': {'link': 'https://landing.example/?x=1'}},
        }}}
        result = build_payload(old, 'https://cdn.example/new.jpg')
        link = result['object_story_spec']['link_data']
        self.assertNotIn('image_hash', link)
        self.assertEqual(link['caption'], 'caption')
        self.assertEqual(link['link'], old['object_story_spec']['link_data']['link'])

    def test_existing_posts_dynamic_carousel_and_unknown_fields_are_not_dropped(self):
        for mutation in (
            lambda c: c.update(object_story_id='1_2'),
            lambda c: c.update(asset_feed_spec={'images': [{'hash': 'old'}]}),
            lambda c: c.update(mystery_setting=False),
            lambda c: c['object_story_spec']['video_data'].update(targeting={'geo_locations': {'countries': ['US']}}),
            lambda c: c['object_story_spec'].update(template_data={'link': 'https://example.com'}),
        ):
            old = video(); mutation(old)
            with self.subTest(old=old), self.assertRaises(PayloadError):
                build_payload(old, 'https://cdn.example/new.jpg')
        old = {'object_story_spec': {'page_id': '1', 'link_data': {
            'link': 'https://example.com', 'child_attachments': [{'link': 'https://example.com'}]}}}
        with self.assertRaises(PayloadError): build_payload(old, 'https://cdn.example/a.jpg')

    def test_missing_real_video_destination_fails_without_default_url(self):
        old = video(); old['object_story_spec']['video_data']['call_to_action']['value'].pop('link')
        with self.assertRaisesRegex(PayloadError, 'video_destination_unavailable'):
            build_payload(old, 'https://cdn.example/new.jpg')

    def test_disabled_screenshot_sourcing_preserves_exact_config(self):
        for extra in ({}, {'enable_social_feedback_preservation': False},
                      {'enable_social_feedback_preservation': True}):
            old = video()
            spec = {'destination_screenshot_spec': {'enroll_status': 'OPT_OUT'}, **extra}
            old['creative_sourcing_spec'] = copy.deepcopy(spec)
            payload = build_payload(old, 'https://cdn.example/new.jpg')
            self.assertEqual(payload['creative_sourcing_spec'], spec)
            self.assertIsNot(payload['creative_sourcing_spec'], old['creative_sourcing_spec'])
            verify_creative(copy.deepcopy(payload), payload)

    def test_sourcing_media_and_unrecognized_modes_still_fail(self):
        for spec in (
            {'destination_screenshot_spec': {'enroll_status': 'OPT_IN'}},
            {'destination_screenshot_spec': {'enroll_status': 'OPT_OUT', 'source_url': 'https://example.com'}},
            {'destination_screenshot_spec': {}},
            {'website': 'https://example.com'},
            {'site_links': []},
            {'enable_social_feedback_preservation': 1},
            [],
        ):
            old = video(); old['creative_sourcing_spec'] = spec
            with self.subTest(spec=spec), self.assertRaises(PayloadError):
                build_payload(old, 'https://cdn.example/new.jpg')

    def test_readback_detects_missing_changed_or_added_sourcing(self):
        old = video()
        old['creative_sourcing_spec'] = {'destination_screenshot_spec': {'enroll_status': 'OPT_OUT'}}
        payload = build_payload(old, 'https://cdn.example/new.jpg')
        for change in (
            lambda c: c.pop('creative_sourcing_spec'),
            lambda c: c['creative_sourcing_spec']['destination_screenshot_spec'].update(enroll_status='OPT_IN'),
            lambda c: c['creative_sourcing_spec'].update(enable_social_feedback_preservation=True),
        ):
            actual = copy.deepcopy(payload); change(actual)
            with self.assertRaisesRegex(PayloadError, 'creative_readback_mismatch'):
                verify_creative(actual, payload)

    def test_object_id_preserves_exact_story_page_identity_and_readback(self):
        old = video(); old['object_id'] = old['object_story_spec']['page_id']
        payload = build_payload(old, 'https://cdn.example/new.jpg')
        self.assertEqual(payload['object_id'], old['object_id'])
        verify_creative(copy.deepcopy(payload), payload)
        for value in ('41', None):
            actual = copy.deepcopy(payload); actual['object_id'] = value
            with self.assertRaises(PayloadError):
                verify_creative(actual, payload)

    def test_object_id_rejects_other_object_or_malformed_identity(self):
        for value in ('41', '40.0', '040', True, {'id': '40'}):
            old = video(); old['object_id'] = value
            with self.subTest(value=value), self.assertRaisesRegex(PayloadError, 'object_id_not_story_page'):
                build_payload(old, 'https://cdn.example/new.jpg')

    def test_cover_url_rejects_credentials_local_files_and_private_literals(self):
        for url in ('file:///tmp/a.png', 'https://u:p@example.com/a.jpg', 'http://127.0.0.1/a.jpg',
                    'http://[::1]/a.jpg', 'http://localhost/a.jpg', 'https://example.com/a.jpg\n'):
            with self.subTest(url=url), self.assertRaises(PayloadError): build_payload(video(), url)

    def test_payload_key_separates_account_text_destination_and_media(self):
        original = build_payload(video(), 'https://cdn.example/a.jpg')
        digest = payload_key('100', original)
        self.assertEqual(digest, payload_key('act_100', original))
        self.assertNotEqual(digest, payload_key('100', dict(original, name='other-name')))
        self.assertNotEqual(digest, payload_key('101', original))
        for field, value in [('message', 'different'), ('link', 'https://other.example'), ('picture', 'https://cdn.example/b.jpg')]:
            changed = copy.deepcopy(original); changed['object_story_spec']['link_data'][field] = value
            self.assertNotEqual(digest, payload_key('100', changed))

    def test_readback_accepts_meta_image_identity_and_detects_business_changes(self):
        expected = build_payload(video(), 'https://cdn.example/a.jpg')
        actual = copy.deepcopy(expected)
        actual['object_story_spec']['link_data'].pop('picture')
        actual['object_story_spec']['link_data']['image_hash'] = 'meta-hash'
        verify_creative(actual, expected)
        actual['object_story_spec']['link_data']['call_to_action']['value']['app_link'] = 'wrong://'
        with self.assertRaisesRegex(PayloadError, 'readback_mismatch'): verify_creative(actual, expected)

    def test_readback_cannot_accept_retained_video_or_missing_image(self):
        expected = build_payload(video(), 'https://cdn.example/a.jpg')
        for mutation in (lambda c: c.update(video_id='30'),
                         lambda c: c['object_story_spec'].update(video_data={'video_id': '30'}),
                         lambda c: c['object_story_spec']['link_data'].pop('picture')):
            actual = copy.deepcopy(expected); mutation(actual)
            with self.assertRaises(PayloadError): verify_creative(actual, expected)

    def test_ad_binding_allows_effective_review_but_no_other_changes(self):
        before = {'id': '10', 'account_id': '100', 'name': 'Ad', 'status': 'ACTIVE',
                  'configured_status': 'ACTIVE', 'effective_status': 'ACTIVE',
                  'creative': {'id': '20'}, 'tracking_specs': [{'action.type': ['offsite_conversion']}],
                  'adset_id': '30', 'campaign_id': '40'}
        after = copy.deepcopy(before); after.update(creative={'id': '21'}, effective_status='PENDING_REVIEW')
        verify_ad_binding(after, before, '21')
        after['tracking_specs'] = []
        with self.assertRaisesRegex(PayloadError, 'ad_parameter_changed:tracking_specs'):
            verify_ad_binding(after, before, '21')

    def test_adlabels_edge_normalizes_without_losing_identity(self):
        old = video(); old['adlabels'] = {'data': [{'id': '99', 'name': 'label'}]}
        expected = build_payload(old, 'https://cdn.example/a.jpg')
        self.assertEqual(expected['adlabels'], [{'id': '99'}])
        actual = copy.deepcopy(expected); actual['adlabels'] = {'data': [{'id': '99'}]}
        verify_creative(actual, expected)
        old['adlabels']['paging'] = {'next': 'https://graph.facebook.com/...'}
        with self.assertRaisesRegex(PayloadError, 'adlabels_incomplete'):
            build_payload(old, 'https://cdn.example/a.jpg')


if __name__ == '__main__':
    unittest.main()
