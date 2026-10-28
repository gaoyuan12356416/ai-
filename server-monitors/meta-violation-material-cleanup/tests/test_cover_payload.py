import copy
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from meta_violation_cleanup.cover_payload import (
    PayloadError, build_payload, payload_key, verify_creative, verify_ad_binding,
)


def video():
    return {
        'id': '20', 'account_id': '100', 'name': 'original', 'object_type': 'VIDEO',
        'status': 'ACTIVE', 'video_id': '30', 'effective_object_story_id': '40_50',
        'url_tags': 'utm_source=a&campaign={{campaign.id}}',
        'degrees_of_freedom_spec': {'creative_features_spec': {'standard_enhancements': {'enroll_status': 'OPT_OUT'}}},
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
