"""Create reproducible, source-bound, editable timelines from server-owned evidence."""
from collections import defaultdict
import math

from .smart_common import require


METHODS = {'hook_first': '热点前置', 'highlight_montage': '高燃混剪',
           'golden_three': '黄金三段', 'cliffhanger': '爆点引流'}
VARIANT_ZOOM = (100, 104, 108, 113, 118)
PREFER = {
    '开场钩子': ('hook', 'conflict', 'turn', 'emotion'), '交代起因': ('context', 'emotion'),
    '转折收束': ('turn', 'resolution', 'cliffhanger'), '高燃片段': ('conflict', 'turn', 'emotion', 'hook'),
    '铺垫': ('context', 'emotion'), '矛盾升级': ('conflict', 'turn', 'emotion'),
    '悬念切点': ('cliffhanger', 'conflict', 'hook'),
}


def evidence(sources):
    events = []
    offset = 0
    for source in sources:
        duration = source['duration_ms']
        for candidate in source['candidates']:
            start, end = candidate['start_ms'], candidate['end_ms']
            require(type(start) is int and type(end) is int and 0 <= start < end <= duration,
                    'smart_evidence_invalid', '选段证据超出素材时间范围', 409)
            anchor = min(end - 1, max(start, candidate['anchor_ms']))
            events.append({**candidate, 'source_id': source['source_id'], 'anchor': anchor,
                           'position': offset + anchor, 'source_duration': duration})
        offset += duration
    return events


def pick(pool, used, role, script):
    available = [x for x in pool if (x['source_id'], x['id']) not in used]
    if not available:
        return None
    preferred = PREFER[role]
    def rank(event):
        tag = event['tag']
        bonus = (.62 - preferred.index(tag) * .06) if tag in preferred else 0
        return (-(event['score'] + bonus), event['position'])
    alternatives = sorted(available, key=rank)
    # Other scripts explore different high-confidence evidence for the lead
    # role. Keep secondary roles fitted to that script's remaining material.
    event = alternatives[min(script - 1, len(alternatives) - 1)]
    used.add((event['source_id'], event['id']))
    return event


def roles_for(events, method, script, target):
    used = set()
    if method == 'hook_first':
        return [(pick(events, used, '开场钩子', script), '开场钩子')]
    if method == 'golden_three':
        hook = pick(events, used, '开场钩子', script)
        setup = pick(events, used, '交代起因', 1)
        end = pick([e for e in events if setup is None or e['position'] > setup['position']],
                   used, '转折收束', 1)
        if end is None:
            end = pick(events, used, '转折收束', 1)
        return [(e, role) for e, role in [(hook, '开场钩子'), (setup, '交代起因'), (end, '转折收束')] if e]
    if method == 'cliffhanger':
        by_time = sorted(events, key=lambda e: e['position'])
        # Keep at least one earlier candidate for the build-up where available.
        end = pick(by_time[1:] or by_time, used, '悬念切点', script)
        before = [e for e in by_time if e['position'] < end['position']]
        build = []
        for role in ['铺垫', '矛盾升级']:
            event = pick(before, used, role, 1)
            if event:
                build.append(event)
        chronological = sorted(build, key=lambda e: e['position'])
        return [(e, ['铺垫', '矛盾升级'][i]) for i, e in enumerate(chronological)] + [(end, '悬念切点')]
    picked = []
    count = min(len(events), max(2, min(12, int(math.ceil(target / 6000)))))
    for i in range(count):
        event = pick(events, used, '高燃片段', script if i == 0 else 1)
        if event:
            picked.append(event)
    picked.sort(key=lambda e: e['position'])
    # Different script leads remain meaningful even when all events are used.
    if len(picked) > 1 and script > 1:
        index = (script - 1) % len(picked)
        picked = picked[index:] + picked[:index]
    return [(e, '高燃片段') for e in picked]


def exclusive_bounds(selected):
    by_source = defaultdict(list)
    for event, role in selected:
        by_source[event['source_id']].append(event)
    bounds = {}
    for group in by_source.values():
        group.sort(key=lambda e: e['anchor'])
        left = 0
        for i, event in enumerate(group):
            right = ((event['anchor'] + group[i + 1]['anchor'] + 1) // 2
                     if i + 1 < len(group) else event['source_duration'])
            bounds[(event['source_id'], event['id'])] = (left, right)
            left = right
    return bounds


def allocations(capacities, target, roles):
    # Allocate remaining time after a short source hits its range boundary.
    weights = [1.4 if role == '交代起因' else .8 if role == '开场钩子' else 1. for role in roles]
    target = min(target, sum(capacities))
    budgets = [0] * len(capacities)
    active = list(range(len(capacities)))
    while target > 0 and active:
        total = sum(weights[i] for i in active)
        for i, share in [(i, max(1, int(target * weights[i] / total))) for i in active]:
            add = min(target, share, capacities[i] - budgets[i])
            budgets[i] += add
            target -= add
        active = [i for i in active if budgets[i] < capacities[i]]
    return budgets


def segments_for(sources, method, selected, target):
    bounds = exclusive_bounds(selected)
    capacities = []
    for event, role in selected:
        key = (event['source_id'], event['id'])
        left, right = bounds[key]
        if role == '悬念切点':
            # Cut close to the visual event, without adding its later outcome.
            right = min(right, max(left + 1, event['end_ms']))
            bounds[key] = left, right
        capacities.append(right - left)
    if method == 'hook_first':
        e = selected[0][0]
        target = min(e['source_duration'], max(500, e['end_ms'] - e['start_ms'] + 1800))
    budgets = allocations(capacities, target, [role for _, role in selected])
    result = []
    for (event, role), size in zip(selected, budgets):
        left, right = bounds[(event['source_id'], event['id'])]
        start = right - size if role == '悬念切点' else max(left, min(right - size, event['anchor'] - int(size * .48)))
        end = start + size
        if end <= start or (end - start) < 34:
            continue
        result.append({'source_id': event['source_id'], 'start_ms': start, 'end_ms': end, 'role': role,
                       'candidate_id': event['id'], 'reason': event['reason']})
    if method == 'hook_first':
        for source in sources:
            result.append({'source_id': source['source_id'], 'start_ms': 0, 'end_ms': source['duration_ms'],
                           'role': '原片顺播', 'candidate_id': '', 'reason': '按用户选入顺序保留本条完整原片。'})
    return result


def build_plans(sources, payload):
    require(isinstance(payload, dict) and not set(payload) - {'analysis_id', 'method', 'merge', 'target_duration_ms',
             'script_count', 'variant_count'}, 'smart_plan_invalid', '剪辑方案参数含未知字段')
    method, merge = payload.get('method'), payload.get('merge', True)
    scripts, variants = payload.get('script_count', 1), payload.get('variant_count', 1)
    target = payload.get('target_duration_ms')
    require(isinstance(method, str) and method in METHODS and type(merge) is bool,
            'smart_plan_invalid', '剪辑方式或合并选项无效')
    require(type(scripts) is int and 1 <= scripts <= 3 and type(variants) is int and 1 <= variants <= 5,
            'smart_count_invalid', '请选择 1–3 种脚本和 1–5 个裂变版本')
    require(target is None or type(target) is int and 1000 <= target <= 2147483647,
            'smart_duration_invalid', '目标时长应为至少一秒的整数毫秒')
    require(not (method == 'hook_first' and target is not None), 'smart_duration_invalid', '热点前置保留完整原片，无需设置目标时长')
    require(sources, 'smart_sources_empty', '请先选择并分析素材')
    groups = [sources] if merge else [[s] for s in sources]
    units, outputs, warnings = [], [], []
    for group_index, group in enumerate(groups, 1):
        events = evidence(group)
        require(events, 'smart_evidence_empty', '本组没有可用的时间码证据', 409)
        goal = target or min(60000, max(12000, sum(e['end_ms'] - e['start_ms'] for e in events)))
        seen = set()
        for s in range(1, scripts + 1):
            selected = roles_for(events, method, s, goal)
            segs = segments_for(group, method, selected, goal)
            require(segs, 'smart_timeline_empty', '选段没有满一帧的可用画面', 409)
            signature = tuple((seg['source_id'], seg['start_ms'], seg['end_ms']) for seg in segs)
            if signature in seen:
                warnings.append('素材较短或可用证据有限，部分脚本的选段相同。')
            seen.add(signature)
            duration = sum(y - x for _, x, y in signature)
            if target and duration < target - 40:
                warnings.append('部分方案的可用源区间短于目标，已保留实际可用时长。')
            for i, seg in enumerate(segs, 1):
                seg['clip_id'] = 'clip_%03d' % i
            for v, zoom in enumerate(VARIANT_ZOOM[:variants], 1):
                uid = 'smart_g%d_s%d_v%d' % (group_index, s, v)
                title = '%s · %s · 脚本%d版本%d' % (group[0]['title'][:85], METHODS[method], s, v)
                clips = []
                for seg in segs:
                    clips.append({'id': seg['clip_id'], 'source_id': seg['source_id'], 'start_ms': seg['start_ms'],
                                  'end_ms': seg['end_ms'], 'fit': 'contain', 'mute': False, 'mirror': False,
                                  'transition': 'cut', 'zoom_percent': zoom})
                units.append({'id': uid, 'title': title, 'series_code': group[0]['series_code'], 'clips': clips})
                outputs.append({'unit_id': uid, 'title': title, 'script_index': s, 'variant_index': v,
                                'duration_ms': duration, 'zoom_percent': zoom, 'segments': [dict(seg) for seg in segs]})
    drafts = [{'schema_version': 2, 'name': '智能剪辑 · ' + METHODS[method],
                               'units': units[i:i + 20]} for i in range(0, len(units), 20)]
    return {'drafts': drafts, 'outputs': outputs, 'warnings': list(dict.fromkeys(warnings))}
