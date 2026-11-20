"""Discover Mobo language editions ahead of release, then check only those editions."""
from dataclasses import dataclass
import re
from typing import Optional

from features.drama_launch_window import bounds, normalize
from features.hit_drama_target import target_language
from features.moboreels_hit import institution_id, LANGUAGES, MOBO_APP_ID, MOBO_PRODUCTS


POLICY = 'mobo_upcoming_v1'
_BATCH = 250


def applies(window):
    if not isinstance(window, str):
        return False
    try:
        value = normalize(window)
        return isinstance(value, str) and int(value.split('~')[0]) < 0
    except ValueError:
        return False


def language_key(value):
    try:
        lang = target_language(value)
    except (TypeError, ValueError):
        return ''
    return {'zh-tw': 'zh-hant', 'tl': 'fil'}.get(lang, lang)


def _table(schema, name):
    if not re.fullmatch(r'[A-Za-z0-9_]+', schema):
        raise ValueError('invalid Mobo catalog schema')
    return f'`{schema}`.{name}'


def _chunks(items):
    for offset in range(0, len(items), _BATCH):
        yield items[offset:offset + _BATCH]


def _packages(schema, identities=()):
    # Multiple packages can point at one language edition. Read the entire
    # active identity before testing its time, including rows outside the range.
    table = _table(schema, 'ads_drama_info')
    scope = ''
    if identities:
        scope = ' AND content_id IN (' + ','.join(['%s'] * len(identities)) + ')'
    sql = f"""
        SELECT content_id, MIN(NULLIF(LOWER(TRIM(language)),'')) lang_min,
               MAX(NULLIF(LOWER(TRIM(language)),'')) lang_max,
               MIN(NULLIF(deploy_time,0)) deploy_min, MAX(NULLIF(deploy_time,0)) deploy_max,
               MIN(NULLIF(published_at,0)) live_min, MAX(NULLIF(published_at,0)) live_max,
               MIN(NULLIF(scheduled_publish_time,0)) plan_min,
               MAX(NULLIF(scheduled_publish_time,0)) plan_max
          FROM {table}
         WHERE app_id=%s AND release_status=1 {scope}
         GROUP BY content_id"""
    return sql, (MOBO_APP_ID, *identities)


def _time(row, *, keep_schedule=False):
    # A real first publication supersedes a stale plan when discovering
    # titles. For a previously scheduled title, a later current plan still
    # protects against an early send. An explicit delivery time takes priority.
    for kind in ('deploy', 'live', 'plan'):
        last = int(row[kind + '_max'] or 0)
        if not last:
            continue
        if last < 0 or int(row[kind + '_min'] or 0) != last:
            return 0
        if keep_schedule and kind == 'live' and row['plan_max']:
            if row['plan_min'] != row['plan_max']:
                return 0
            last = max(last, int(row['plan_max']))
        return last
    return 0


def _note_materials(select, schema, stories, config):
    # These are early availability hints, never material reservations. Read
    # current media, duration, eligibility and history again at final selection.
    from features.material_products import selected
    products = [p for p in selected(config) if p.casefold() in MOBO_PRODUCTS]
    if not products:
        return
    for story in stories.values():
        story['source_material_count'] = 0
    rule = config.get('material_rule', {})
    predicates = []
    durations = []
    for key, op in [('duration_min_seconds', '>='), ('duration_max_seconds', '<=')]:
        if rule.get(key) is not None:
            predicates.append('s.video_duration' + op + '%s')
            durations.append(rule[key])
    table = _table(schema, 'ads_custom_source')
    filters = ''.join(' AND ' + field for field in predicates)
    product_slots = ','.join(['%s'] * len(products))
    for ids in _chunks(list(stories)):
        marks = ','.join(['%s'] * len(ids))
        sql = f"""SELECT s.data_source_id content_id, LOWER(TRIM(s.language)) lang,
                         COUNT(*) n, MIN(s.id) example_id
                    FROM {table} s FORCE INDEX(idx_source_type_source_id)
                   WHERE s.data_source=%s AND s.data_source_id IN ({marks})
                     AND s.product IN ({product_slots}) AND s.type=2 AND s.is_delete=0
                     AND s.url<>'' {filters}
                   GROUP BY s.data_source_id,LOWER(TRIM(s.language))"""
        params = (int(config.get('material_data_source', 6)), *ids, *products, *durations)
        for row in select(sql, params):
            story = stories.get(str(row['content_id']))
            if story and language_key(row['lang']) == story['language']:
                story['source_material_count'] = story.get('source_material_count', 0) + int(row['n'])
                story['example_material_id'] = str(row['example_id'])


def discover(select, schema, window, now_epoch, *, material_config=None):
    now = int(now_epoch)
    if not applies(window):
        raise ValueError('Moboreels 预扫仅适用于含未来天数的模板')
    lower, upper = bounds(window, now)
    grouped, params = _packages(schema)
    sql = f"""SELECT p.* FROM ({grouped}) p
               WHERE COALESCE(p.deploy_max,p.live_max,p.plan_max) BETWEEN %s AND %s"""
    stories = {}
    for row in select(sql, (*params, lower, upper)):
        story_id = str(row['content_id'] or '').strip()
        language = language_key(row['lang_min'])
        at = _time(row)
        if not story_id or not language or language_key(row['lang_max']) != language or not lower <= at <= upper:
            continue
        stories[story_id] = {'language': language, 'at': at,
                             'scheduled': not bool(row['live_max'])}
    plan = {'policy': POLICY, 'scanned_at': now, 'stories': stories}
    if material_config is not None and stories:
        try:
            _note_materials(select, schema, stories, material_config)
        except Exception as exc:
            # A material hint is advisory. A failed hint must not discard the
            # schedule snapshot or prevent independent profiles from proceeding.
            import logging
            plan['material_scan'] = 'unavailable'
            logging.getLogger(__name__).warning('mobo.upcoming.material_hints %s', type(exc).__name__)
            for story in stories.values():
                story.pop('source_material_count', None)
                story.pop('example_material_id', None)
    return plan


@dataclass
class Availability:
    ready: dict
    pending: dict
    retry_at: Optional[int]


def assess(select, schema, plan, language, now_epoch):
    """Consult current times and provider visibility, retaining frozen story identities."""
    if plan.get('policy') != POLICY:
        raise ValueError('Moboreels 预扫计划版本无效')
    lang = language_key(language)
    frozen = plan['stories']
    ids = [sid for sid, data in frozen.items() if lang and data['language'] == lang]
    now = int(now_epoch)
    ready, pending = {}, {}
    next_hour = (now // 3600 + 1) * 3600
    near_retry = now + 300
    if ids:
        institution = institution_id()
    for chunk in _chunks(ids):
        grouped, params = _packages(schema, chunk)
        sql = f"""SELECT p.*,d.is_show,d.lang_id,d.story_name
                    FROM ({grouped}) p LEFT JOIN ads_ai.cdreader_koc_drama d
                      ON d.institution_user_id=%s AND d.project_type=2 AND d.story_id=p.content_id"""
        for row in select(sql, (*params, institution)):
            sid = str(row['content_id'])
            original = frozen.get(sid)
            if not original or language_key(row['lang_min']) != lang or language_key(row['lang_max']) != lang:
                continue
            at = _time(row, keep_schedule=bool(original.get('scheduled')))
            if not at:
                continue  # withdrawn/ambiguous schedule is never a release
            if at > now:
                pending[sid] = min(at, next_hour)
                continue
            if row['is_show'] != 1:
                # Upstream first-publish timestamps can arrive before the hourly
                # supplier catalog. A scheduled, as-yet-unlisted item still waits.
                if original.get('scheduled') or not row['live_max']:
                    pending[sid] = near_retry
                continue
            # Provider language 23 is Polish in this catalog (588 listed editions
            # cross-checked against their product-scoped language). Require both
            # independent sources to agree as for every other language.
            provider_language = language_key('pl' if row['lang_id'] == 23 else LANGUAGES.get(row['lang_id']))
            if provider_language == lang:
                ready[sid] = {'name': str(row['story_name'] or ''), 'at': at}
    wakeups = list(pending.values())
    if ready:
        wakeups.append(near_retry)  # a same-language source video may arrive after listing
    return Availability(ready, pending, min(wakeups, default=None))
