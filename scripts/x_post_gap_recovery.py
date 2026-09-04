#!/usr/bin/env python3
"""Audited operator recovery; never calls X or selects replacement content.

Call under the shared runner lock after a backup and current account verify.
All operations default to read-only and permit one transition per identity.
"""
import contextlib
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from features.x_posts.service import XPostStore, utc_now
from features.x_posts.selector import shanghai_now

TABLE = "x_post_operator_gap_recovery_audit"


@contextlib.contextmanager
def transaction(db_path, action, identity, actor, apply):
    if not actor.strip() or len(actor) > 100:
        raise ValueError("Operator actor required")
    uri = Path(db_path).resolve().as_uri() + ("?mode=rw" if apply else "?mode=ro")
    with contextlib.closing(sqlite3.connect(uri, uri=True, timeout=10)) as c:
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
        if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone():
            if c.execute(f"SELECT 1 FROM {TABLE} WHERE action=? AND identity=?", (action, identity)).fetchone():
                raise ValueError("Recovery already attempted; inspect existing result")
        yield c
        if apply:
            c.commit()
        else:
            c.rollback()


def audit(c, action, identity, actor, previous, evidence):
    c.execute(f"CREATE TABLE IF NOT EXISTS {TABLE}(id INTEGER PRIMARY KEY, action TEXT NOT NULL, identity INTEGER NOT NULL, actor TEXT NOT NULL, previous_state_json TEXT NOT NULL, evidence_json TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(action,identity))")
    c.execute(f"INSERT INTO {TABLE}(action,identity,actor,previous_state_json,evidence_json,created_at) VALUES(?,?,?,?,?,?)", (action, identity, actor, json.dumps(previous, ensure_ascii=False), json.dumps(evidence, ensure_ascii=False), utc_now()))


def expanded_post_text(post):
    text = str(post.get("text") or "")
    for url in post.get("entities", {}).get("urls", []):
        short = str(url.get("url") or "")
        if not short:
            raise ValueError("Incomplete URL evidence")
        if url.get("media_key"):
            if not text.endswith(" " + short):
                raise ValueError("Unexpected media link position")
            text = text[:-(len(short) + 1)]
        else:
            expanded = str(url.get("expanded_url") or "")
            if not expanded:
                raise ValueError("Unresolved text URL")
            text = text.replace(short, expanded)
    return text


def reconcile_direct_post(db_path, queue_id, post, *, actor, apply=False):
    action = "confirmed_direct_post_v1"
    with transaction(db_path, action, queue_id, actor, apply) as c:
        q = c.execute("SELECT * FROM x_post_queue WHERE id=?", (queue_id,)).fetchone()
        l = c.execute("SELECT * FROM x_post_publish_log WHERE queue_id=?", (queue_id,)).fetchone()
        if not q or not l or q['source_type'] != 'material' or q['delivery_mode'] != 'direct' or q['status'] != 'failed' or l['status'] != 'failed' or l['unknown_outcome'] != 1 or not l['x_media_id']:
            raise ValueError("Expected unresolved direct material Post")
        a = c.execute("SELECT x_user_id FROM x_authorized_account WHERE id=?", (q['account_id'],)).fetchone()
        post_id = str(post.get('id') or '')
        published = datetime.fromisoformat(str(post.get('created_at', '')).replace('Z', '+00:00'))
        started = datetime.fromisoformat(l['started_at'].replace('Z', '+00:00'))
        failed = datetime.fromisoformat(l['updated_at'].replace('Z', '+00:00'))
        if (not re.fullmatch(r'[0-9]{1,32}', post_id) or post.get('author_id') != a['x_user_id']
            or post.get('attachments', {}).get('media_keys') != ['7_' + l['x_media_id']]
            or expanded_post_text(post) != l['post_text'] or not started <= published <= failed
            or (l['x_post_id'] and l['x_post_id'] != post_id)):
            raise ValueError("Author, body, media, time or Post identity mismatch")
        if c.execute("SELECT 1 FROM x_post_publish_log WHERE x_post_id=? AND queue_id<>?", (post_id, queue_id)).fetchone():
            raise ValueError("Post ID already belongs to another queue")
        pool = c.execute("SELECT * FROM x_post_material_pool WHERE id=?", (q['pool_item_id'],)).fetchone()
        if not pool or pool['material_key'] != q['material_key'] or pool['status'] != 'unpublished':
            raise ValueError("Material pool binding changed")
        url = 'https://x.com/%s/status/%s' % (q['account_username'], post_id)
        if apply:
            now = utc_now()
            audit(c, action, queue_id, actor, {'queue': dict(q), 'log': dict(l)}, post)
            c.execute("UPDATE x_post_publish_log SET status='published',x_post_id=?,x_post_url=?,published_at=?,unknown_outcome=0,error_code='',error_message='',updated_at=? WHERE id=?", (post_id, url, published.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), now, l['id']))
            c.execute("UPDATE x_post_queue SET status='published',updated_at=? WHERE id=?", (now, queue_id))
            XPostStore._mark_pool_published(c, queue_id, now)
            XPostStore._sync_run(c, queue_id, now)
        return {'queue_id': queue_id, 'post_id': post_id, 'post_url': url, 'applied': apply, 'x_write_attempted': False}


def rearm_material_upload(db_path, queue_id, *, actor, apply=False):
    action = 'known_token_upload_failure_v1'
    with transaction(db_path, action, queue_id, actor, apply) as c:
        q = c.execute('SELECT * FROM x_post_queue WHERE id=?', (queue_id,)).fetchone()
        l = c.execute('SELECT * FROM x_post_publish_log WHERE queue_id=?', (queue_id,)).fetchone()
        if (not q or not l or q['source_type'] != 'material' or q['status'] != 'failed'
            or q['delivery_mode'] not in ('direct', 'premium_relay_repost')
            or l['status'] != 'failed' or l['error_code'] != 'x_token_invalid' or l['unknown_outcome']
            or l['attempt_count'] != 1 or l['x_media_id'] or l['x_post_id'] or l['published_at']
            or not l['post_text'] or not l['short_url'] or not l['long_url']):
            raise ValueError('Only one explicit Token failure before Post creation can be recovered')
        pool = c.execute('SELECT * FROM x_post_material_pool WHERE id=?', (q['pool_item_id'],)).fetchone()
        if not pool or pool['status'] != 'unpublished' or pool['material_key'] != q['material_key']:
            raise ValueError('Frozen material binding changed')
        relay = None
        ids = [q['account_id']]
        if q['delivery_mode'] == 'premium_relay_repost':
            relay = XPostStore._assert_relay_queue_binding(c, q)
            ids.append(q['relay_account_id'])
            if (relay['status'] != 'failed' or relay['error_code'] != 'x_token_invalid'
                or relay['unknown_outcome'] or relay['source_post_id'] or relay['repost_id']
                or relay['source_attempt_count'] != 1 or relay['repost_attempt_count'] != 0):
                raise ValueError('Relay source must have no Post or target attempt')
        for aid in ids:
            a = c.execute('SELECT status,publish_approved,access_expires_at FROM x_authorized_account WHERE id=?', (aid,)).fetchone()
            if not a or a['status'] != 'active' or a['publish_approved'] != 1 or a['access_expires_at'] <= utc_now():
                raise ValueError('Account must be currently verified and approved')
        XPostStore._assert_account_publish_fence(c, q)
        if apply:
            now = utc_now()
            audit(c, action, queue_id, actor, {'queue': dict(q), 'log': dict(l), 'relay': dict(relay) if relay else None}, {'preserved_attempts': True})
            c.execute("UPDATE x_post_queue SET status='queued',updated_at=? WHERE id=?", (now, queue_id))
            c.execute("UPDATE x_post_publish_log SET status='reserved',updated_at=? WHERE id=?", (now, l['id']))
            if relay:
                c.execute("UPDATE x_post_repost_ledger SET status='reserved',updated_at=? WHERE queue_id=?", (now, queue_id))
            XPostStore._sync_run(c, queue_id, now)
        return {'queue_id': queue_id, 'applied': apply, 'x_write_attempted': False}


def rearm_today_assignment_failure(db_path, run_id, *, actor, deployed_commit, apply=False):
    action = 'same_day_language_transaction_fix_v1'
    if not re.fullmatch(r'[a-f0-9]{40}', deployed_commit):
        raise ValueError('Deployed fix commit required')
    with transaction(db_path, action, run_id, actor, apply) as c:
        r = c.execute('SELECT * FROM x_post_schedule_run WHERE id=?', (run_id,)).fetchone()
        if (not r or r['source_type'] != 'drama' or r['status'] != 'failed_preflight'
            or r['run_date'] != shanghai_now().date().isoformat()
            or r['publish_time'] > shanghai_now().strftime('%H:%M')
            or r['error_code'] != 'x_post_drama_assignment_conflict'
            or r['error_message'] != '短剧候选与当前账号固定归属或入池顺序不一致'
            or any(r[k] for k in ['queued_count','published_count','failed_count','unknown_count'])
            or r['expected_count'] != len(json.loads(r['account_ids_json']))
            or c.execute('SELECT 1 FROM x_post_queue WHERE schedule_run_id=?', (run_id,)).fetchone()):
            raise ValueError('Expected exact same-day zero-queue assignment rejection')
        if apply:
            audit(c, action, run_id, actor, dict(r), {'deployed_commit': deployed_commit})
            # The exact transaction rejection proves no queue was committed.
            # Preserve the frozen date, account scope, slot and body template.
            c.execute("UPDATE x_post_schedule_run SET status='claimed',error_code='',error_message='',started_at='',finished_at='',lease_heartbeat_at='',plan_attempted_at='',updated_at=? WHERE id=?", (utc_now(), run_id))
        return {'run_id': run_id, 'applied': apply, 'x_write_attempted': False}


def create_today_material_gap_child(db_path, parent_id, publish_time, *, actor, deployed_commit, apply=False):
    """Compensate only accounts with no queue in one completed same-day batch."""
    action = 'same_day_skipped_material_accounts_v1'
    current = shanghai_now()
    if not re.fullmatch(r'[a-f0-9]{40}', deployed_commit) or not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', publish_time) or publish_time > current.strftime('%H:%M'):
        raise ValueError('Deployed commit and an already due same-day time required')
    with transaction(db_path, action, parent_id, actor, apply) as c:
        parent = c.execute('SELECT * FROM x_post_schedule_run WHERE id=?', (parent_id,)).fetchone()
        if (not parent or parent['source_type'] != 'material' or parent['status'] != 'completed'
            or parent['run_date'] != current.date().isoformat() or parent['failed_count'] or parent['unknown_count']):
            raise ValueError('Only a completed same-day material parent can receive a child')
        rows = c.execute('SELECT q.id,q.account_id,q.status,l.status AS log_status,l.unknown_outcome,l.x_post_id FROM x_post_queue q LEFT JOIN x_post_publish_log l ON l.queue_id=q.id WHERE q.schedule_run_id=?', (parent_id,)).fetchall()
        if (not rows or len(rows) != parent['expected_count'] or len(rows) != parent['published_count']
            or any(r['status'] != 'published' or r['log_status'] != 'published' or r['unknown_outcome'] or not r['x_post_id'] for r in rows)):
            raise ValueError('Every parent queue must have confirmed final delivery')
        configured = json.loads(parent['account_ids_json'])
        plan = c.execute("SELECT * FROM x_post_schedule_random_plan WHERE source_type='material' AND run_date=?", (parent['run_date'],)).fetchone()
        if (not plan or configured != json.loads(plan['account_ids_json'])
            or parent['publish_time'] not in json.loads(plan['publish_times_json'])
            or parent['config_version'] != plan['config_version']):
            raise ValueError('Parent must belong to the original frozen daily plan')
        occupied = {r['account_id'] for r in rows}
        missing = [aid for aid in configured if aid not in occupied]
        if not occupied.issubset(set(configured)) or not missing:
            raise ValueError('No exact missing account scope')
        settings = c.execute("SELECT enabled,account_ids_json FROM x_post_schedule_config WHERE source_type='material'").fetchone()
        if not settings or not settings['enabled'] or not set(missing).issubset(set(json.loads(settings['account_ids_json']))):
            raise ValueError('Missing accounts are no longer configured')
        for aid in missing:
            account = c.execute('SELECT status,publish_approved,access_expires_at FROM x_authorized_account WHERE id=?', (aid,)).fetchone()
            if not account or account['status'] != 'active' or not account['publish_approved'] or account['access_expires_at'] <= utc_now():
                raise ValueError('Missing account is not currently verified')
        if c.execute('SELECT 1 FROM x_post_schedule_run WHERE run_date=? AND publish_time=?', (parent['run_date'], publish_time)).fetchone():
            raise ValueError('Compensation time is already occupied')
        for plan_row in c.execute('SELECT publish_times_json FROM x_post_schedule_random_plan WHERE run_date=?', (parent['run_date'],)):
            if publish_time in json.loads(plan_row['publish_times_json']):
                raise ValueError('Compensation time collides with a frozen schedule')
        child_id = None
        if apply:
            now = utc_now()
            cursor = c.execute("INSERT INTO x_post_schedule_run(slot_key,source_type,run_date,publish_time,timezone,config_version,account_ids_json,schedule_mode,body_template,status,expected_count,created_at,updated_at) VALUES(?,'material',?,?,?,?,?,?,?,'claimed',?,?,?)", (
                'xpost:schedule:material-gap:v1:%s' % parent_id, parent['run_date'], publish_time,
                parent['timezone'], parent['config_version'], json.dumps(missing), parent['schedule_mode'],
                parent['body_template'], len(missing), now, now))
            child_id = cursor.lastrowid
            audit(c, action, parent_id, actor, dict(parent), {'deployed_commit': deployed_commit, 'child_run_id': child_id, 'missing_account_ids': missing, 'parent_queue_ids': [r['id'] for r in rows]})
        return {'parent_run_id': parent_id, 'child_run_id': child_id, 'missing_account_ids': missing, 'publish_time': publish_time, 'applied': apply, 'x_write_attempted': False}
