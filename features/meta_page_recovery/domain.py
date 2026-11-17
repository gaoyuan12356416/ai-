"""Pure selection, identity and creative reconstruction rules."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from zoneinfo import ZoneInfo

POOLS = {24: (1479, 'Dramawave'), 25: (979, 'FreeReels'), 57: (3302, 'HotDrama')}
CHAT_ID = 'oc_b633b6fba2c78f43ef1ebacd314d518c'
UNKNOWN = '__unknown__'
AUDIENCES = {'female', 'male', 'general', 'lgbtq'}
STAGES = [('language_profile', False, 'profile'), ('language_unprofiled', False, 'unprofiled'),
          ('fallback_profile', True, 'profile'), ('fallback_unprofiled', True, 'unprofiled'),
          ('language_relaxed', False, 'relaxed'), ('fallback_relaxed', True, 'relaxed')]
FINAL = {'skipped', 'failed', 'changed', 'pending_review', 'deliverable', 'superseded'}
RETRY = {'retry_wait', 'waiting_no_candidate'}
UNCERTAIN = {'unknown_creative', 'unknown_ad', 'unknown_media'}


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def language(value):
    value = str(value or '').strip().lower().replace('_', '-')
    return {'jp': 'ja', 'pt': 'pt-br'}.get(value, value)


def bad(page):
    return page.get('status') == 1 or page.get('forbidden_status') == 1


def page_health(page):
    if bad(page):return 'abnormal'
    if page.get('status')==0 and page.get('forbidden_status')==0:return 'normal'
    return 'unknown'


def available(page):
    return (page.get('status') == 0 and page.get('forbidden_status') == 0
            and page.get('is_permission') == 1)


def account_dates(account, instant=None):
    instant = instant or datetime.now(timezone.utc)
    # An unknown timezone cannot silently become the CPU's calendar day.
    zone = ZoneInfo(account['timezone_name'])
    today = instant.astimezone(zone).date()
    return dict(since=(today - timedelta(days=1)).isoformat(), until=today.isoformat())


def purchases(rows):
    """None is no data; missing purchase actions in a real row means zero."""
    if not rows:
        return None
    value = Decimal(0)
    for row in rows:
        exact = [a for a in row.get('actions', []) if a.get('action_type') == 'purchase']
        if len(exact) > 1:
            raise ValueError('duplicate_purchase_action')
        for action in exact:
            count = Decimal(str(action['value']))
            if not count.is_finite() or count < 0:
                raise ValueError('invalid_purchase_value')
            value += count
    return str(value)


def active_ad(ad):
    return (ad.get('configured_status', ad.get('status')) == 'ACTIVE'
            and ad.get('campaign', {}).get('configured_status', ad.get('campaign', {}).get('status')) == 'ACTIVE'
            and ad.get('adset', {}).get('configured_status', ad.get('adset', {}).get('status')) == 'ACTIVE')


def creative_page(creative):
    spec = creative.get('object_story_spec') or {}
    if spec.get('page_id'):
        return str(spec['page_id'])
    story = creative.get('effective_object_story_id') or creative.get('object_story_id') or ''
    return str(story).split('_')[0] if '_' in str(story) else ''


def profile_match(profile, target):
    if not profile or profile.get('status') != 'ready':
        return 'unprofiled', 0, 'missing_or_no_income'
    branches = [r for r in profile.get('audiences', []) if r.get('share_bp') is not None and r['share_bp'] > 0]
    if not branches:
        return 'unprofiled', 0, 'no_positive_profile'
    themes = set(target.get('themes') or []) - {UNKNOWN}
    highest = max(r['share_bp'] for r in branches)
    dominant = [r for r in branches if r['share_bp'] == highest]
    known = {r['key'] for r in dominant if r.get('key') in AUDIENCES}
    audience = target.get('audience')
    if audience in AUDIENCES and known:
        if audience not in known:
            return 'profiled', 0, 'known_audience_mismatch'
        nodes = [r for r in dominant if r['key'] == audience]
        shares = {}
        for branch in nodes:
            for node in branch.get('children', []):
                if node.get('key') in themes and node.get('matchable', True):
                    shares[node['key']] = max(shares.get(node['key'], 0), node.get('share_bp') or 0)
        return 'profiled', sum(shares.values()), 'audience_themes'
    # Unknown direction is still a usable topic profile. Use Page-wide shares,
    # not conditional percentages in tiny audience branches.
    score = 0
    known_themes = False
    for branch in branches:
        seen = set()
        for node in branch.get('children', []):
            key = node.get('key')
            if key and key != UNKNOWN and node.get('matchable', True):
                known_themes = True
                if key in themes and key not in seen:
                    score += node.get('channel_share_bp') or (branch['share_bp'] * (node.get('share_bp') or 0) // 10000)
                    seen.add(key)
    return ('profiled' if known_themes else 'unprofiled'), score, 'themes_only'


def select_page(pages, profiles, target, group_id, allocations=None, excluded=()):
    allocations = allocations or Counter()
    excluded = set(excluded)
    lang = language(target.get('language'))
    candidates = []
    for page in pages:
        pid = str(page['page_id'])
        if page['group_id'] != group_id or pid in excluded or not available(page):
            continue
        plang = language(page.get('language'))
        if plang and (not lang or plang != lang):
            continue
        kind, score, basis = profile_match(profiles.get(pid), target)
        item = dict(page, page_id=pid, match_kind=kind, match_score=score, match_basis=basis)
        candidates.append(item)
    def order(page):
        active, fans = page.get('active_ad_count'), page.get('fan_ccount')
        return (active is None, (active or 0) + allocations[page['page_id']],
                fans is None, fans or 0, int(page['page_id']))
    for stage, fallback, kind in STAGES:
        rows = [p for p in candidates if (not language(p.get('language'))) == fallback
                and (kind == 'relaxed' or (kind == 'profile' and p['match_score'] > 0)
                     or (kind == 'unprofiled' and p['match_kind'] == 'unprofiled'))]
        if kind == 'profile':
            rows = sorted(rows, key=lambda p: (-p['match_score'], order(p)))[:3]
        if rows:
            selected = min(rows, key=order)
            return dict(selected, selection_stage=stage)
    return None


CREATE_FIELDS = {'object_story_spec', 'asset_feed_spec', 'url_tags', 'applink_treatment',
                 'object_store_url', 'destination_set_id', 'template_url', 'template_url_spec',
                 'product_set_id', 'platform_customizations', 'degrees_of_freedom_spec',
                 'contextual_multi_ads', 'authorization_category', 'instagram_branded_content',
                 'facebook_branded_content', 'omnichannel_link_spec', 'destination_spec',
                 'dynamic_ad_voice', 'categorization_criteria', 'ad_disclaimer_spec',
                 'interactive_components_spec'}
CREATE_IDENTITY_FIELDS = {'instagram_actor_id', 'instagram_user_id', 'instagram_story_id',
                          'source_instagram_media_id', 'object_story_id', 'effective_object_story_id'}


def build_creative(original, page_id, operation, instagram_id=None):
    payload = {k: deepcopy(v) for k, v in original.items() if k in CREATE_FIELDS and v is not None}
    spec = payload.get('object_story_spec') or {}
    if not any(spec.get(k) for k in ('video_data', 'link_data', 'photo_data', 'template_data', 'text_data')) and not payload.get('asset_feed_spec'):
        raise ValueError('original_story_content_missing')
    for field in CREATE_IDENTITY_FIELDS:
        spec.pop(field, None)
    spec['page_id'] = str(page_id)
    if instagram_id:
        spec['instagram_user_id'] = str(instagram_id)
    payload['object_story_spec'] = spec
    payload['name'] = 'mpr-' + operation
    # Returned video specs can contain both the persistent hash and derived URL.
    video = spec.get('video_data') or {}
    if video.get('image_hash'):
        video.pop('image_url', None)
    enhancements = payload.get('degrees_of_freedom_spec', {}).get('creative_features_spec', {})
    # Removed by Meta; replaying it produces 3858504 even when OPT_OUT.
    enhancements.pop('standard_enhancements', None)
    return payload


def semantic_payload(payload):
    value = deepcopy(payload)
    value.pop('name', None)
    return value


def video_ids(payload):
    result = set()
    def visit(value):
        if isinstance(value, dict):
            for k, v in value.items():
                if k == 'video_id' and v:
                    result.add(str(v))
                else:
                    visit(v)
        elif isinstance(value, list):
            for v in value:
                visit(v)
    visit(payload)
    return sorted(result)


def replace_video(payload, old, new):
    result = deepcopy(payload)
    def visit(value):
        if isinstance(value, dict):
            for k, v in value.items():
                if k == 'video_id' and str(v) == str(old):
                    value[k] = str(new)
                else:
                    visit(v)
        elif isinstance(value, list):
            for item in value:
                visit(item)
    visit(result)
    return result
