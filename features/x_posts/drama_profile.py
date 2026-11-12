"""Freeze new drama-to-channel matches before individual daily slots run.

Portrait reads happen outside SQLite transactions.  The durable mapping keeps
later slots from losing their drama when an earlier slot or a portrait refresh
changes the surrounding state.  Actual episode reservations remain in the
existing publisher transaction and never reassign unfinished dramas.
"""
import hashlib
import json
import os

from features.x_tag_revenue.taxonomy import classify, UNKNOWN
from features.x_tag_revenue.tags import VERSION as TAG_VERSION
from features.x_tag_revenue.hierarchy import VERSION as HIERARCHY_VERSION
from features.x_accounts.language import canonical_drama_language

VERSION = 'drama-channel-audience-theme-v1'
TABLE = 'x_post_drama_profile_assignment'
DDL = '''CREATE TABLE IF NOT EXISTS x_post_drama_profile_assignment (
 scope_key TEXT NOT NULL, run_date TEXT NOT NULL, config_version INTEGER NOT NULL,
 pool_item_id INTEGER NOT NULL, replay_generation INTEGER NOT NULL,
 account_id INTEGER NOT NULL, content_id TEXT NOT NULL, pool_created_at TEXT NOT NULL,
 language TEXT NOT NULL, labels_sha256 TEXT NOT NULL, selection_json TEXT NOT NULL,
 created_at TEXT NOT NULL, queue_id INTEGER,
 released_at TEXT NOT NULL DEFAULT '', release_reason TEXT NOT NULL DEFAULT '',
 PRIMARY KEY(scope_key,pool_item_id,replay_generation))'''


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def scope_key(run_date, config_version, account_ids):
    value = [VERSION, str(run_date), int(config_version), [int(v) for v in account_ids]]
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def portrait_snapshot(accounts):
    from features.x_tag_publish.transport import rpc, Error
    token = os.environ.get('X_DRAMA_PROFILE_INTERNAL_TOKEN', '')
    if not token:
        return {'accounts': {}}, {'unavailable': True, 'version': ''}
    try:
        result = rpc('http://127.0.0.1:8896', '/internal/x-channel-tag-revenue', token,
                     payload={'x_user_ids': [str(a['x_user_id']) for a in accounts]}, timeout=25)
        return result['data'], result['meta']
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, Error):
        return {'accounts': {}}, {'unavailable': True, 'version': ''}


def channel_score(account, classification, snapshot):
    data, meta = snapshot
    profile = data.get('accounts', {}).get(str(account.get('x_user_id', '')), {})
    reason = ''
    if meta.get('unavailable'): reason = 'portrait_unavailable'
    elif meta.get('missing_dates'): reason = 'portrait_incomplete'
    elif meta.get('tag_mapping_version') != TAG_VERSION: reason = 'portrait_mapping_outdated'
    elif meta.get('hierarchy_version') != HIERARCHY_VERSION or profile.get('hierarchy_version') != HIERARCHY_VERSION: reason = 'portrait_hierarchy_outdated'
    elif not profile or profile.get('revenue_cents', 0) <= 0: reason = 'no_positive_revenue'
    elif classification.get('audience') == UNKNOWN: reason = 'drama_audience_unlabeled'
    audiences = profile.get('audiences') or []
    audience = next((v for v in audiences if v.get('key') == classification.get('audience') and v.get('matchable')), {})
    if not reason and (audience.get('revenue_cents', 0) <= 0 or audience.get('share_bp') is None):
        reason = 'no_matching_audience_revenue'
    if reason:
        return (0.0, 0.0), reason
    total = float(profile['revenue_cents'])
    audience_income = float(audience['revenue_cents'])
    themes = set(classification.get('themes') or []) - {UNKNOWN}
    matched_income = max((float(v.get('revenue_cents', 0)) for v in audience.get('children', [])
                          if v.get('key') in themes and v.get('matchable') and v.get('share_bp') is not None), default=0.0)
    return (audience_income / total, matched_income / audience_income), '' if matched_income else 'audience_only_no_matching_theme'


def pool_identity(row):
    return (int(row['id']), int(row['replay_generation']), str(row['content_id']),
            str(row['created_at']), canonical_drama_language(row['language']))


def assignment_matches(assignment, pool):
    return (int(assignment['pool_item_id']), int(assignment['replay_generation']),
            str(assignment['content_id']), str(assignment['pool_created_at']),
            str(assignment['language'])) == pool_identity(pool)


def load_assignments(conn, key):
    # Existing schemas intentionally have no rows until a scheduler prepares a
    # profile plan. Avoid turning a read-only legacy path into a migration.
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone():
        return []
    return [dict(r) for r in conn.execute('SELECT * FROM '+TABLE+" WHERE scope_key=? AND released_at='' ORDER BY created_at,pool_item_id", (key,))]


def freeze_assignments(conn, *, run_date, config_version, configured_account_ids,
                       accounts, snapshot, blocked_accounts, timestamp, limit=1000):
    """Called inside the owner's transaction, with all network data preloaded."""
    conn.execute(DDL)
    key = scope_key(run_date, config_version, configured_account_ids)
    account_by_id = {int(a['id']):a for a in accounts if int(a['id']) in configured_account_ids
                     and int(a['id']) not in blocked_accounts}
    rows = [dict(r) for r in conn.execute("SELECT * FROM x_post_drama_pool WHERE status IN ('pending','active') AND free_episode_count>0 AND next_sub_number<=free_episode_count ORDER BY CASE WHEN priority_at<>'' THEN 0 ELSE 1 END,priority_at DESC,created_at DESC,id DESC")]
    by_id = {int(r['id']):r for r in rows}
    occupied = {int(r['assigned_account_id']) for r in conn.execute("SELECT assigned_account_id FROM x_post_drama_pool WHERE status<>'completed' AND assigned_account_id>0 AND next_sub_number<=free_episode_count")}
    frozen_pool_ids = set()
    for saved in load_assignments(conn, key):
        row = by_id.get(int(saved['pool_item_id']))
        if not row or not assignment_matches(saved, row):
            continue
        # A bound or attempted identity never becomes available elsewhere.
        if int(row['assigned_account_id']):
            continue
        if row['last_error_code'] not in ('', 'x_long_video_requires_premium'):
            history = conn.execute('SELECT 1 FROM x_post_queue WHERE drama_pool_item_id=? AND drama_replay_generation=? LIMIT 1',
                                   (row['id'], row['replay_generation'])).fetchone()
            if not history:
                conn.execute('UPDATE '+TABLE+" SET released_at=?,release_reason=? WHERE scope_key=? AND pool_item_id=? AND replay_generation=? AND released_at=''",
                             (timestamp, 'unattempted_pool_validation_failed:'+str(row['last_error_code'])[:96], key, row['id'], row['replay_generation']))
                continue
        frozen_pool_ids.add(int(row['id']))
        occupied.add(int(saved['account_id']))
    remaining = [v for v in configured_account_ids if v in account_by_id and v not in occupied]
    unassigned = [r for r in rows if not int(r['assigned_account_id']) and
                  r['last_error_code'] in ('', 'x_long_video_requires_premium')][:limit]
    for row in unassigned:
        if not remaining: break
        if int(row['id']) in frozen_pool_ids: continue
        episode_key = str(row['content_id']) + (':replay'+str(row['replay_generation']) if int(row['replay_generation']) > 1 else '') + ':' + str(row['next_sub_number'])
        if conn.execute('SELECT 1 FROM x_post_queue WHERE episode_key=? LIMIT 1', (episode_key,)).fetchone():
            continue
        # Never overwrite a previous freeze, including a repaired pool row.
        if conn.execute('SELECT 1 FROM '+TABLE+' WHERE scope_key=? AND pool_item_id=? AND replay_generation=?',
                        (key, row['id'], row['replay_generation'])).fetchone():
            continue
        language = canonical_drama_language(row['language'])
        eligible = [v for v in remaining if canonical_drama_language(account_by_id[v].get('drama_language')) == language]
        if not eligible: continue
        classification = classify(row['labels'])
        scores = {v:channel_score(account_by_id[v], classification, snapshot) for v in eligible}
        selected = max(eligible, key=lambda v:scores[v][0])
        score, reason = scores[selected]
        audit = dict(policy_version=VERSION, scope_key=key, configured_account_ids=list(configured_account_ids),
                     account_id=selected, x_user_id=str(account_by_id[selected].get('x_user_id', '')),
                     profile_version=snapshot[1].get('version', ''), profile_generated_at=snapshot[1].get('generated_at', ''),
                     hierarchy_version=HIERARCHY_VERSION, classification=classification,
                     audience_share=score[0], theme_conditional_share=score[1], fallback=bool(reason), fallback_reason=reason,
                     candidate_account_ids=eligible)
        conn.execute('INSERT INTO '+TABLE+'(scope_key,run_date,config_version,pool_item_id,replay_generation,account_id,content_id,pool_created_at,language,labels_sha256,selection_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                     (key, run_date, int(config_version), int(row['id']), int(row['replay_generation']), selected,
                      row['content_id'], row['created_at'], language, hashlib.sha256(str(row['labels']).encode()).hexdigest(),
                      canonical(audit), timestamp))
        remaining.remove(selected)
    return key
