"""Lossless business-field mapping for a single image/video to a cover image.

Media is the only intentionally replaced content. Unknown meaningful fields
are rejected, rather than silently omitted. No I/O or image download occurs.
"""
from copy import deepcopy
from collections import Counter
import hashlib
import ipaddress
import json
import re
from urllib.parse import urlsplit


class PayloadError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


TOP_PRESERVE = frozenset({
    'name', 'url_tags', 'applink_treatment', 'authorization_category',
    'degrees_of_freedom_spec', 'regional_regulation_disclaimer_spec',
    'ad_disclaimer_spec', 'platform_customizations', 'portrait_customizations',
    'branded_content', 'branded_content_sponsor_page_id', 'facebook_branded_content',
    'instagram_branded_content', 'instagram_user_id', 'instagram_actor_id',
    'use_page_actor_override', 'contextual_multi_ads', 'adlabels',
    'enable_direct_install', 'enable_launch_instant_app',
    'creative_sourcing_spec', 'object_id',
})
# These fields describe the existing object or duplicate values derived from
# object_story_spec. None is a writable business setting in this conversion.
TOP_DERIVED = frozenset({
    'id', 'account_id', 'name', 'status', 'object_type', 'actor_id',
    'effective_object_story_id', 'effective_instagram_media_id',
    'effective_instagram_story_id', 'effective_authorization_category',
    'instagram_permalink_url', 'image_hash', 'image_url', 'video_id',
    'thumbnail_id', 'thumbnail_url', 'body', 'title', 'call_to_action_type',
    'link_url', 'object_url', 'object_store_url', 'link_og_id',
})
TOP_UNSUPPORTED = frozenset({
    'asset_feed_spec', 'object_story_id', 'source_facebook_post_id',
    'source_instagram_media_id', 'instagram_story_id',
    'photo_album_source_object_story_id', 'product_set_id', 'product_data',
    'destination_set_id', 'destination_spec', 'template_url', 'template_url_spec',
    'playable_asset_id', 'interactive_components_spec', 'media_sourcing_spec',
    'image_crops', 'omnichannel_link_spec',
    'format_transformation_spec', 'bundle_folder_id', 'dynamic_ad_voice',
    'link_deep_link_url', 'link_destination_display_url',
})
STORY_IDENTITY = frozenset({'page_id', 'instagram_user_id', 'instagram_actor_id'})
LINK_PRESERVE = frozenset({
    'link', 'message', 'name', 'description', 'caption', 'call_to_action',
    'app_link_spec', 'attachment_style', 'branded_content_shared_to_sponsor_status',
    'branded_content_sponsor_page_id', 'force_single_link', 'offer_id',
    'page_welcome_message', 'post_click_configuration', 'retailer_item_ids',
    'show_multiple_images', 'use_flexible_image_aspect_ratio',
})
VIDEO_PRESERVE = frozenset({
    'message', 'call_to_action', 'branded_content_shared_to_sponsor_status',
    'branded_content_sponsor_page_id', 'offer_id', 'page_welcome_message',
    'post_click_configuration', 'retailer_item_ids',
})


def _meaningful(value):
    return value is not None and value != '' and value != [] and value != {}


def cover_url(value):
    if not isinstance(value, str) or value != value.strip() or len(value) > 8192:
        raise PayloadError('invalid_cover_url')
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        if (parsed.scheme not in ('http', 'https') or not host or parsed.username
                or parsed.password or parsed.fragment or parsed.port not in (None, 80, 443)
                or any(ord(c) < 33 for c in value)):
            raise ValueError()
        if host.lower() == 'localhost' or host.lower().endswith(('.localhost', '.local')):
            raise ValueError()
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError()
    except (ValueError, TypeError):
        raise PayloadError('invalid_cover_url') from None
    return value


def _unsupported(values, allowed, context):
    if not isinstance(values, dict):
        raise PayloadError(context + '_malformed')
    unsupported = sorted(k for k, v in values.items() if k not in allowed and _meaningful(v))
    if unsupported:
        # Field names come from a fixed Meta response schema, never values.
        raise PayloadError(context + '_unsupported_fields:' + ','.join(unsupported))


def _validate_sourcing(value):
    if value is None:
        return
    if not isinstance(value, dict):
        raise PayloadError('creative_sourcing_spec_malformed')
    # Meta returns this disabled screenshot default even on ordinary single
    # image/video creatives. Preserve it, without admitting sourced media.
    allowed = {'destination_screenshot_spec', 'enable_social_feedback_preservation'}
    if set(value) - allowed:
        raise PayloadError('creative_sourcing_spec_unsupported')
    if ('destination_screenshot_spec' in value and
            value['destination_screenshot_spec'] != {'enroll_status': 'OPT_OUT'}):
        raise PayloadError('destination_screenshot_sourcing_unsupported')
    if ('enable_social_feedback_preservation' in value and
            type(value['enable_social_feedback_preservation']) is not bool):
        raise PayloadError('social_feedback_preservation_malformed')


def creation_payload(expected):
    """Project writable v25 fields without changing the readback contract."""
    if not isinstance(expected, dict):
        raise PayloadError('creative_malformed')
    wire = deepcopy(expected)
    if 'enable_direct_install' in wire:
        # This is returned by GET but absent from the v25 create parameter
        # schema. An enabled value cannot safely be reproduced by omission.
        if wire['enable_direct_install'] is not False:
            raise PayloadError('direct_install_enabled_or_unverified')
        wire.pop('enable_direct_install')
    freedom = wire.get('degrees_of_freedom_spec')
    features = freedom.get('creative_features_spec') if isinstance(freedom, dict) else None
    if isinstance(features, dict) and 'standard_enhancements' in features:
        aggregate = features['standard_enhancements']
        individual = {key: value for key, value in features.items() if key != 'standard_enhancements'}
        if (not isinstance(aggregate, dict) or set(aggregate) != {'enroll_status'} or
                aggregate['enroll_status'] not in ('OPT_IN', 'OPT_OUT') or
                not any(isinstance(value, dict) and value for value in individual.values())):
            raise PayloadError('legacy_enhancements_without_individual_settings')
        # Meta rejects this deprecated aggregate on creation. All explicit
        # individual options remain verbatim, including each OPT_IN/OPT_OUT.
        features.pop('standard_enhancements')
    return wire


def build_payload(old_creative, url):
    url = cover_url(url)
    if not isinstance(old_creative, dict):
        raise PayloadError('creative_malformed')
    _unsupported(old_creative, TOP_PRESERVE | TOP_DERIVED | {'object_story_spec'}, 'creative')
    _validate_sourcing(old_creative.get('creative_sourcing_spec'))
    story = old_creative.get('object_story_spec')
    _unsupported(story, STORY_IDENTITY | {'link_data', 'video_data'}, 'story')
    page_id = story.get('page_id')
    if not str(page_id or '').isdigit() or str(page_id).startswith('0'):
        raise PayloadError('page_identity_missing')
    object_id = old_creative.get('object_id')
    if _meaningful(object_id) and (not str(object_id).isascii() or
            not str(object_id).isdigit() or str(object_id) != str(page_id)):
        raise PayloadError('creative_object_id_not_story_page')
    choices = [key for key in ('link_data', 'video_data') if _meaningful(story.get(key))]
    if len(choices) != 1:
        raise PayloadError('single_image_or_video_required')
    data = story[choices[0]]
    if choices[0] == 'link_data':
        _unsupported(data, LINK_PRESERVE | {'picture', 'image_hash'}, 'link_data')
        # Multiple-image presentation cannot be represented as one cover.
        if data.get('show_multiple_images') is True:
            raise PayloadError('multiple_images_unsupported')
        converted = {k: deepcopy(v) for k, v in data.items() if k in LINK_PRESERVE}
    else:
        _unsupported(data, VIDEO_PRESERVE | {'video_id', 'image_hash', 'image_url',
                                           'title', 'link_description'}, 'video_data')
        if not str(data.get('video_id') or '').isdigit():
            raise PayloadError('video_identity_missing')
        converted = {k: deepcopy(v) for k, v in data.items() if k in VIDEO_PRESERVE}
        if 'title' in data:
            converted['name'] = deepcopy(data['title'])
        if 'link_description' in data:
            converted['description'] = deepcopy(data['link_description'])
        cta = data.get('call_to_action')
        destination = ((cta.get('value') or {}).get('link')
                       if isinstance(cta, dict) and isinstance(cta.get('value'), dict) else None)
        if not isinstance(destination, str) or not destination.strip():
            raise PayloadError('video_destination_unavailable')
        converted['link'] = destination
    if not isinstance(converted.get('link'), str) or not converted['link'].strip():
        raise PayloadError('destination_unavailable')
    if 'call_to_action' in converted and not isinstance(converted['call_to_action'], dict):
        raise PayloadError('call_to_action_malformed')
    converted['picture'] = url
    new_story = {k: deepcopy(v) for k, v in story.items() if k in STORY_IDENTITY}
    new_story['link_data'] = converted
    result = {k: deepcopy(v) for k, v in old_creative.items()
              if k in TOP_PRESERVE and v is not None}
    if 'object_id' in result and not _meaningful(result['object_id']):
        result.pop('object_id')
    if 'adlabels' in result:
        result['adlabels'] = _labels(result['adlabels'])
    result['object_story_spec'] = new_story
    creation_payload(result)  # Preflight unsupported enabled/aggregate settings.
    return result


def _labels(value):
    if isinstance(value, dict):
        if value.get('paging', {}).get('next'):
            raise PayloadError('adlabels_incomplete')
        value = value.get('data')
    if not isinstance(value, list) or any(not isinstance(row, dict) or not str(row.get('id', '')).isdigit() for row in value):
        raise PayloadError('adlabels_malformed')
    return [{'id': str(row['id'])} for row in value]


def payload_key(account_id, payload):
    encoded = json.dumps({'account_id': str(account_id).removeprefix('act_'), 'payload': payload},
                         sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest()


def _assert_subset(actual, expected, path):
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            raise PayloadError('creative_readback_mismatch:' + path)
        for key, value in expected.items():
            if key not in actual:
                if value in (None, '', [], {}):
                    continue
                raise PayloadError('creative_readback_missing:' + path + '.' + key)
            _assert_subset(actual[key], value, path + '.' + key)
    elif actual != expected:
        raise PayloadError('creative_readback_mismatch:' + path)


def verify_creative(actual, expected_payload):
    if not isinstance(actual, dict) or not isinstance(expected_payload, dict):
        raise PayloadError('creative_readback_malformed')
    story = actual.get('object_story_spec')
    if (not isinstance(story, dict) or _meaningful(story.get('video_data'))
            or _meaningful(actual.get('video_id')) or _meaningful(actual.get('asset_feed_spec'))):
        raise PayloadError('creative_readback_not_single_image')
    if any(_meaningful(story.get(key)) for key in ('photo_data', 'template_data', 'text_data', 'product_data')):
        raise PayloadError('creative_readback_not_single_image')
    link = story.get('link_data')
    if not isinstance(link, dict) or not (link.get('picture') or link.get('image_hash')):
        raise PayloadError('creative_readback_image_missing')
    if _meaningful(link.get('child_attachments')):
        raise PayloadError('creative_readback_not_single_image')
    expected = deepcopy(expected_payload)
    actual = deepcopy(actual)
    # Meta appends this observed generated suffix to Creative's administrative
    # name. Never normalize the displayed link_data.name (the ad headline).
    name, returned_name = expected.get('name'), actual.get('name')
    if (isinstance(name, str) and name and isinstance(returned_name, str)
            and returned_name.startswith(name + ' ')
            and re.fullmatch(r'\d{4}-\d{2}-\d{2}-[0-9a-f]{32}', returned_name[len(name) + 1:])):
        actual['name'] = name
    if isinstance(name, str) and isinstance(returned_name, str):
        automatic = r'(?:video|image) Auto Creative \d{4}-\d{2}-\d{2}-[0-9a-f]{32}(?: \d{4}-\d{2}-\d{2}-[0-9a-f]{32})*'
        if re.fullmatch(automatic, name) and re.fullmatch(automatic, returned_name):
            # Observed Meta response can regenerate the hash in its own
            # administrative Auto Creative label. Media/date/hash are label
            # metadata, not creative content. This is not the Ad name
            # or displayed headline, both of which remain strict.
            actual['name'] = name
    # The create projection explicitly omits Meta's retired aggregate only
    # when individual feature settings exist. Accept its absence on GET, but
    # keep strict comparison of every individual option and any returned aggregate.
    wire = creation_payload(expected)
    features = (expected.get('degrees_of_freedom_spec') or {}).get('creative_features_spec')
    wire_features = (wire.get('degrees_of_freedom_spec') or {}).get('creative_features_spec')
    actual_features = (actual.get('degrees_of_freedom_spec') or {}).get('creative_features_spec')
    if (isinstance(features, dict) and isinstance(wire_features, dict)
            and isinstance(actual_features, dict) and 'standard_enhancements' in features
            and 'standard_enhancements' not in wire_features
            and 'standard_enhancements' not in actual_features):
        features.pop('standard_enhancements')
    # Explicit operator approval on 2026-10-28: after image binding Meta no
    # longer returns the video-only crop option. Keep submitting its original
    # setting; accept only its absence, never a changed value or another loss.
    if (isinstance(features, dict) and isinstance(actual_features, dict)
            and features.get('video_auto_crop') in (
                {'enroll_status': 'OPT_IN'}, {'enroll_status': 'OPT_OUT'})
            and 'video_auto_crop' not in actual_features):
        features.pop('video_auto_crop')
    if 'adlabels' in expected and 'adlabels' in actual:
        actual['adlabels'] = _labels(actual['adlabels'])
    if _meaningful(expected.get('creative_sourcing_spec')):
        if actual.get('creative_sourcing_spec') != expected['creative_sourcing_spec']:
            raise PayloadError('creative_readback_mismatch:creative.creative_sourcing_spec')
    # Meta fetches picture and may return its CDN URL and/or generated image hash.
    # This verifies Media identity exists, not a hash of un-downloaded input bytes.
    expected['object_story_spec']['link_data'].pop('picture', None)
    _assert_subset(actual, expected, 'creative')


def _tracking_after_story_change(actual, expected, old_story_id, new_story_id, *, pending=False):
    """Only Meta-derived post references may change; preserve the full multiset."""
    old = str(old_story_id or '').split('_')
    new = str(new_story_id or '').split('_')
    if (len(old) != 2 or len(new) != 2 or not all(x.isdigit() for x in old + new)
            or old[0] != new[0] or not isinstance(actual, list) or not isinstance(expected, list)):
        return False
    def normalize(rows, posts):
        result = deepcopy(rows)
        for row in result:
            if not isinstance(row, dict):
                raise PayloadError('tracking_specs_malformed')
            if 'post' not in row:
                continue
            action = row.get('action.type')
            page_key = 'post.wall' if action == ['link_click'] else 'page'
            if (action not in (['post_engagement'], ['post_interaction_gross'], ['link_click'])
                    or row.get('post') not in [[post] for post in posts] or row.get(page_key) != [old[0]]):
                raise PayloadError('tracking_post_identity_unverified')
            row['post'] = ['verified-story-post']
        return sorted(json.dumps(r, sort_keys=True, ensure_ascii=False) for r in result)
    # Ad and Creative GETs are not an atomic snapshot. Meta may still return
    # the original post on some derived tracking actions during propagation.
    # Both IDs are proven Creative identities on the same Page; no third ID,
    # feature, action, conversion or Pixel change is accepted.
    observed = normalize(actual, {old[1], new[1]})
    wanted = normalize(expected, {old[1]})
    if not pending:
        return observed == wanted
    # Meta can briefly omit derived post tracking rows altogether after the
    # binding. This is not success: retain an unknown binding for GET-only
    # reconciliation while all conversion/Pixel tracking remains identical.
    business = lambda rows: sorted(json.dumps(r, sort_keys=True, ensure_ascii=False)
                                   for r in rows if 'post' not in r)
    return (observed != wanted and business(actual) == business(expected)
            and not (Counter(observed) - Counter(wanted)))


def verify_ad_binding(actual, expected_ad, new_creative_id, *, old_story_id=None, new_story_id=None):
    if not isinstance(actual, dict) or not isinstance(expected_ad, dict):
        raise PayloadError('ad_readback_malformed')
    if str((actual.get('creative') or {}).get('id')) != str(new_creative_id):
        raise PayloadError('ad_binding_not_confirmed')
    tracking_pending = False
    for key in ('id', 'account_id', 'name', 'status', 'configured_status',
                'adset_id', 'campaign_id', 'tracking_specs', 'conversion_specs', 'bid_amount'):
        if key in expected_ad and actual.get(key) != expected_ad[key]:
            if key == 'tracking_specs' and _tracking_after_story_change(
                    actual.get(key), expected_ad[key], old_story_id, new_story_id):
                continue
            if key == 'tracking_specs' and _tracking_after_story_change(
                    actual.get(key), expected_ad[key], old_story_id, new_story_id, pending=True):
                tracking_pending = True
                continue
            raise PayloadError('ad_parameter_changed:' + key)
    for key in ('adset', 'campaign'):
        if key in expected_ad and str((actual.get(key) or {}).get('id')) != str((expected_ad[key] or {}).get('id')):
            raise PayloadError('ad_parameter_changed:' + key)
    if tracking_pending:
        raise PayloadError('ad_tracking_materialization_pending')
