"""Deterministic, side-effect-free selection for the configured Page group."""
from collections import Counter
from collections.abc import Mapping


UNKNOWN = '__unknown__'
KNOWN_AUDIENCES = frozenset(('female', 'male', 'general', 'lgbtq'))
STAGES = (
    ('language_profile', 'language', 'profile'),
    ('language_unprofiled', 'language', 'unprofiled'),
    ('fallback_profile', 'fallback', 'profile'),
    ('fallback_unprofiled', 'fallback', 'unprofiled'),
    ('language_relaxed', 'language', 'relaxed'),
    ('fallback_relaxed', 'fallback', 'relaxed'),
)


def _language(value):
    value = str(value or '').strip().lower().replace('_', '-')
    return {'jp': 'ja', 'pt': 'pt-br'}.get(value, value)


def _number(value):
    """Missing/malformed counters are unknown, never a fabricated zero."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
        if number < 0 or (not isinstance(value, str) and number != value):
            return None
        return number
    except (TypeError, ValueError, OverflowError):
        return None


def _page_order(page_id):
    value = str(page_id)
    return (0, int(value), value) if value.isascii() and value.isdigit() else (1, 0, value)


def _load_order(page, allocations):
    active, fans = page['active_ad_count'], page['fan_ccount']
    count = allocations[page['page_id']]
    return (active is None, active + count if active is not None else count,
            fans is None, fans if fans is not None else 0, _page_order(page['page_id']))


def _profile(profile):
    """Keep conflicting known portraits out of the unprofiled candidate tier."""
    if not isinstance(profile, Mapping):
        return dict(kind='unprofiled', reason='profile_missing', audience_keys=[], branches={})
    status = profile.get('status')
    if status != 'ready':
        return dict(kind='unprofiled', reason='profile_' + str(status or 'invalid'),
                    audience_keys=[], branches={})
    raw_nodes = profile.get('audiences')
    if not isinstance(raw_nodes, (list, tuple)):
        return dict(kind='unprofiled', reason='profile_invalid', audience_keys=[], branches={})
    nodes = [node for node in raw_nodes if isinstance(node, Mapping)]
    values = [(node, _number(node.get('share_bp'))) for node in nodes]
    positive = [(node, share) for node, share in values if share is not None and share > 0]
    if not positive:
        return dict(kind='unprofiled', reason='profile_no_positive_audience', audience_keys=[], branches={})
    highest = max(share for _, share in positive)
    dominant = [node for node, share in positive if share == highest]
    branches = {str(node.get('key')): node for node in dominant
                if node.get('key') in KNOWN_AUDIENCES and node.get('matchable', True)}
    if not branches:
        return dict(kind='unprofiled', reason='profile_dominant_audience_unknown',
                    audience_keys=[], branches={})
    return dict(kind='profiled', reason='', audience_keys=sorted(branches), branches=branches)


def _prepare_pages(pages, profiles):
    prepared, seen = [], set()
    for source in pages:
        if not isinstance(source, Mapping):
            continue
        pid = str(source.get('page_id') or '').strip()
        if not pid or pid in seen:
            continue
        seen.add(pid)
        reasons = []
        for field, expected in (('status', 0), ('forbidden_status', 0), ('is_permission', 1)):
            if _number(source.get(field)) != expected:
                reasons.append(field + '_unavailable')
        profile = _profile(profiles.get(pid))
        prepared.append(dict(page_id=pid, language=_language(source.get('language')),
                             available=not reasons, exclusion_reasons=reasons,
                             active_ad_count=_number(source.get('active_ad_count')),
                             fan_ccount=_number(source.get('fan_ccount')),
                             updated_at=source.get('updated_at'), profile=profile))
    return sorted(prepared, key=lambda page: _page_order(page['page_id']))


def _match(page, audience, themes):
    profile = page['profile']
    if profile['kind'] == 'unprofiled':
        return 0, [], profile['reason']
    if audience not in KNOWN_AUDIENCES:
        return 0, [], 'drama_audience_unknown'
    if audience not in profile['branches']:
        return 0, [], 'dominant_audience_mismatch'
    if not themes:
        return 0, [], 'drama_themes_unknown'
    shares = {}
    for node in profile['branches'][audience].get('children') or []:
        if not isinstance(node, Mapping):
            continue
        key, share = node.get('key'), _number(node.get('share_bp'))
        if key in themes and key != UNKNOWN and node.get('matchable', True) and share:
            # Duplicate input labels/nodes cannot increase a Page's match score.
            shares[key] = max(shares.get(key, 0), share)
    matched = [dict(key=key, share_bp=shares[key]) for key in sorted(shares)]
    return sum(shares.values()), matched, '' if shares else 'no_matching_theme'


def _failure(drama_id, code, message, diagnostics):
    return dict(drama_id=drama_id, page_id=None, status='failed', selection_stage=None,
                error_code=code, error_message=message, diagnostics=diagnostics)


def assign(drama_ids, dramas, pages, profiles):
    """Return one ordered outcome per input occurrence without reserving Pages.

    The load adjustment lives only within this call. Profile candidates are the
    three highest positive theme scores, then selected by adjusted ad load,
    followers and numeric Page ID. The caller owns snapshots, persistence and
    notifications; this module performs no I/O and never mutates its inputs.
    """
    prepared = _prepare_pages(pages, profiles)
    allocations = Counter()
    results = []
    for occurrence, raw_id in enumerate(drama_ids):
        drama_id = str(raw_id)
        diagnostics = dict(input_index=occurrence, warnings=[], stages=[], candidates=[])
        try:
            drama = dramas.get(drama_id)
            if not drama or not drama.get('exists'):
                results.append(_failure(drama_id, 'drama_not_found', '未找到对应剧目。', diagnostics))
                continue
            language = _language(drama.get('language'))
            audience = drama.get('audience') or UNKNOWN
            themes = sorted({str(theme) for theme in drama.get('themes', []) if theme and theme != UNKNOWN})
            diagnostics.update(language=language, audience=audience, themes=themes,
                               classification=drama.get('classification') or {},
                               warnings=list(drama.get('warnings') or []))
            if not language:
                diagnostics['warnings'].append('drama_language_missing')
            scores, audit = {}, {}
            for page in prepared:
                pid = page['page_id']
                score, matched, reason = _match(page, audience, themes)
                scores[pid] = score
                pool = 'fallback' if not page['language'] else 'language' if language and page['language'] == language else 'other_language'
                record = dict(page_id=pid, language=page['language'], pool=pool,
                              available=page['available'], exclusion_reasons=list(page['exclusion_reasons']),
                              profile_kind=page['profile']['kind'], dominant_audiences=page['profile']['audience_keys'],
                              match_score_bp=score, matched_themes=matched, match_reason=reason,
                              active_ad_count=page['active_ad_count'], fan_ccount=page['fan_ccount'],
                              batch_assigned_count=allocations[pid],
                              adjusted_active_ad_count=(page['active_ad_count'] + allocations[pid]
                                                        if page['active_ad_count'] is not None else None),
                              updated_at=page['updated_at'], stage_exclusions={})
                if pool == 'other_language':
                    record['exclusion_reasons'].append('language_mismatch')
                audit[pid] = record
                diagnostics['candidates'].append(record)
            selected = None
            for name, pool, kind in STAGES:
                candidates = []
                for page in prepared:
                    pid = page['page_id']
                    record = audit[pid]
                    reason = ('page_unavailable' if not page['available'] else
                              'pool_mismatch' if record['pool'] != pool else
                              'profile_not_matched' if kind == 'profile' and scores[pid] <= 0 else
                              'page_has_known_profile' if kind == 'unprofiled' and page['profile']['kind'] != 'unprofiled' else '')
                    if reason:
                        record['stage_exclusions'][name] = reason
                    else:
                        candidates.append(page)
                eligible_count = len(candidates)
                if kind == 'profile':
                    candidates.sort(key=lambda page: (-scores[page['page_id']], _load_order(page, allocations)))
                    for page in candidates[3:]:
                        audit[page['page_id']]['stage_exclusions'][name] = 'outside_top_three'
                    candidates = candidates[:3]
                candidates.sort(key=lambda page: _load_order(page, allocations))
                stage = dict(name=name, eligible_count=eligible_count, candidate_count=len(candidates),
                             candidate_page_ids=[page['page_id'] for page in candidates],
                             evaluated=selected is None)
                diagnostics['stages'].append(stage)
                if selected is None and candidates:
                    selected = (name, candidates[0])
            if selected is None:
                results.append(_failure(drama_id, 'no_available_page', '同语言池和空语言兜底池均无可用 Page。', diagnostics))
                continue
            stage, page = selected
            pid = page['page_id']
            relaxed = stage.endswith('_relaxed')
            if relaxed:
                diagnostics['warnings'].append('profile_relaxed')
            diagnostics['selected'] = dict(page_id=pid, match_score_bp=scores[pid],
                                           active_ad_count=page['active_ad_count'], fan_ccount=page['fan_ccount'],
                                           batch_assigned_count=allocations[pid],
                                           adjusted_active_ad_count=audit[pid]['adjusted_active_ad_count'],
                                           updated_at=page['updated_at'])
            allocations[pid] += 1
            results.append(dict(drama_id=drama_id, page_id=pid, status='degraded' if relaxed else 'assigned',
                                selection_stage=stage, error_code='profile_relaxed' if relaxed else None,
                                error_message='未找到画像匹配或无画像 Page，已在对应语言范围内放宽画像分配。' if relaxed else None,
                                diagnostics=diagnostics))
        except Exception as exc:
            # A malformed drama must not suppress the rest of the batch.
            diagnostics['exception_type'] = type(exc).__name__
            results.append(_failure(drama_id, 'selection_error', '该剧分配异常，请根据任务 ID 查看诊断。', diagnostics))
    return results
