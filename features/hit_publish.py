"""Shared explicit hit-publishing assignment and release-time policy."""
from datetime import datetime, timezone
from decimal import Decimal
import re
from contextvars import ContextVar
from contextlib import contextmanager

POLICY = 'profile_one_to_one_release_minus_30m_v1'
FUTURE_TIMES = ContextVar('hit_publish_future_times', default={})


def target_language(value):
    from features.x_accounts.language import canonical_drama_language
    value = canonical_drama_language(value, default='')
    return 'pt-br' if value == 'pt' else value


@contextmanager
def future_scope(times):
    token = FUTURE_TIMES.set(dict(times))
    try:
        yield
    finally:
        FUTURE_TIMES.reset(token)


def future_allowed(content_id, language, release):
    return 0 < int(release) <= FUTURE_TIMES.get().get(str(content_id)+':'+target_language(language), 0)


def material_ids(value):
    values = re.split(r'[\s,，;；]+', value.strip()) if isinstance(value, str) else value
    if not isinstance(values, (list, tuple)) or not values or len(values) > 200:
        raise ValueError('请输入 1–200 条素材 ID，可用换行、逗号或空格分隔')
    result = []
    for item in values:
        if isinstance(item, bool) or not re.fullmatch(r'[1-9][0-9]{0,30}', str(item)):
            raise ValueError('素材 ID 须为正整数，可用换行、逗号或空格分隔')
        if str(item) not in result:
            result.append(str(item))
    return result


def profile_score(decision, metadata):
    audience = metadata.get('audience')
    roots = decision.weights
    themes = decision.node_weights.get((audience,), {})
    matching = [themes.get(theme, 0) for theme in metadata.get('themes', [])]
    if decision.reason or not roots.get(audience) or not matching or not max(matching):
        return 0
    score = (Decimal(roots[audience]) / sum(roots.values())
             * Decimal(max(matching)) / sum(themes.values()))
    return max(1, int(score * 1_000_000_000))


def assign(materials, accounts, scores):
    """Maximum-cardinality, then maximum-profile-score deterministic matching.

    Hungarian assignment with one dummy per material. Missing edges cannot
    win; cardinality dominates the sum of all normalized profile scores.
    """
    materials, accounts = list(materials), list(accounts)
    n, real = len(materials), len(accounts)
    if not n or not real:
        return {}
    m, bonus = real + n, (n + 1) * 1_000_000_000
    u, v, p, way = [0]*(n+1), [0]*(m+1), [0]*(m+1), [0]*(m+1)
    def cost(i, j):
        if j > real:
            return 0
        score = scores.get((materials[i-1], accounts[j-1]), 0)
        return -bonus-score if score > 0 else bonus
    for i in range(1, n+1):
        p[0], j0 = i, 0
        minimum, used = [10**30]*(m+1), [False]*(m+1)
        while True:
            used[j0], i0, delta, j1 = True, p[j0], 10**30, 0
            for j in range(1, m+1):
                if used[j]:
                    continue
                current = cost(i0,j)-u[i0]-v[j]
                if current < minimum[j]:
                    minimum[j], way[j] = current, j0
                if minimum[j] < delta:
                    delta, j1 = minimum[j], j
            for j in range(m+1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minimum[j] -= delta
            j0 = j1
            if not p[j0]:
                break
        while j0:
            j1 = way[j0]
            p[j0], j0 = p[j1], j1
    return {materials[p[j]-1]: accounts[j-1] for j in range(1, real+1)
            if p[j] and scores.get((materials[p[j]-1], accounts[j-1]), 0) > 0}


def release_times(query, schema, app_id, ids):
    """Freeze positive catalog launch times, keeping language versions separate."""
    result = {}
    ids = list(dict.fromkeys(ids))
    for offset in range(0, len(ids), 100):
        batch = ids[offset:offset+100]
        rows = query(f'SELECT content_id,language,deploy_time FROM `{schema}`.ads_drama_info '
                     f'WHERE app_id=%s AND content_id IN ({",".join("%s" for _ in batch)}) '
                     'AND release_status=1 AND deploy_time>0', (app_id, *batch))
        for row in rows:
            cid = str(row.get('content_id') or '')
            if cid not in batch:
                continue
            try:
                lang, value = target_language(row.get('language')), int(row['deploy_time'])
                datetime.fromtimestamp(value, timezone.utc)
            except (KeyError, ValueError, TypeError, OverflowError, OSError):
                continue
            key = cid + ':' + lang
            result[key] = max(result.get(key, 0), value)
    return result


def timing(times, content_id, language, now):
    release = int(times.get(str(content_id)+':'+target_language(language), 0))
    if release <= 0:
        raise ValueError('目标剧缺少有效可投放时间')
    now = now.astimezone(timezone.utc).replace(microsecond=0)
    publish = max(now, datetime.fromtimestamp(release-1800, timezone.utc))
    return dict(policy=POLICY, release_at_utc=datetime.fromtimestamp(release, timezone.utc).isoformat(),
                publish_at_utc=publish.isoformat(), prepare_at_utc=now.isoformat(),
                advance_seconds=1800, future_release=release > int(now.timestamp()))
