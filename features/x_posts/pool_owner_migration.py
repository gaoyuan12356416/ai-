"""Audited owner-only correction; publication rows and existing URLs never change."""
import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

from .attribution import readonly, validate_context, AttributionError
from .pool_ownership import identity, POLICY

AUDIT = 'x_post_pool_owner_correction'
FIELDS = {'owner', 'tenant', 'owner_name', 'source_owner', 'source_tenant',
          'fallback_reason', 'owner_basis', 'pool_source_type', 'pool_item_id', 'pool_created_at'}


def compact(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def plan(db_path, users_db):
    with closing(readonly(users_db)) as u:
        users = [dict(r) for r in u.execute('SELECT user_id,tenant_key,name FROM drama_admin_user')]
    result = []
    with closing(readonly(db_path)) as c:
        c.execute('BEGIN')
        for row in c.execute('SELECT a.log_id,a.queue_id,a.context_json FROM x_post_link_attribution a ORDER BY log_id'):
            before = validate_context(json.loads(row['context_json']))
            if before.get('link_type') != 'system':
                continue
            queue = dict(c.execute('SELECT * FROM x_post_queue WHERE id=?', (row['queue_id'],)).fetchone())
            owner = identity(c, queue, users, unresolved_tenant=before['tenant'])
            if not owner:
                continue
            after = dict(before, **owner)
            validate_context(after)
            assert all(before.get(k) == after.get(k) for k in set(before) | set(after) if k not in FIELDS)
            encoded = compact(after)
            if json.loads(encoded) == before:
                continue
            result.append(dict(log_id=row['log_id'],queue_id=row['queue_id'],
                               before=row['context_json'],after=encoded,identity=owner))
    payload = dict(policy=POLICY,rows=result)
    return dict(payload,sha256=hashlib.sha256(compact(payload).encode()).hexdigest())


def apply(db_path, manifest, *, rollback=False):
    payload = {k:manifest[k] for k in ('policy','rows')}
    if manifest['policy'] != POLICY or hashlib.sha256(compact(payload).encode()).hexdigest()!=manifest['sha256']:
        raise AttributionError('pool_owner_manifest_changed')
    with closing(sqlite3.connect(str(db_path),timeout=10)) as c:
        c.row_factory=sqlite3.Row
        c.execute('BEGIN IMMEDIATE')
        try:
            c.execute('CREATE TABLE IF NOT EXISTS '+AUDIT+'(manifest_sha256 TEXT NOT NULL,log_id INTEGER NOT NULL,queue_id INTEGER NOT NULL,before_json TEXT NOT NULL,after_json TEXT NOT NULL,applied_at TEXT NOT NULL,rolled_back_at TEXT NOT NULL DEFAULT \'\',PRIMARY KEY(manifest_sha256,log_id))')
            changed=0
            for row in manifest['rows']:
                before,after=json.loads(row['before']),json.loads(row['after'])
                validate_context(before);validate_context(after)
                if any(before.get(k)!=after.get(k) for k in set(before)|set(after) if k not in FIELDS):
                    raise AttributionError('pool_owner_nonownership_change')
                current=c.execute('SELECT queue_id,context_json FROM x_post_link_attribution WHERE log_id=?',(row['log_id'],)).fetchone()
                expected,target=(row['after'],row['before']) if rollback else (row['before'],row['after'])
                audit=c.execute('SELECT * FROM '+AUDIT+' WHERE manifest_sha256=? AND log_id=?',(manifest['sha256'],row['log_id'])).fetchone()
                if not current or current['queue_id']!=row['queue_id']:
                    raise AttributionError('pool_owner_queue_changed')
                if current['context_json']==target and audit:
                    continue
                if current['context_json']!=expected:
                    raise AttributionError('pool_owner_context_changed')
                if rollback and not audit:
                    raise AttributionError('pool_owner_audit_missing')
                if not rollback:
                    q=c.execute('SELECT * FROM x_post_queue WHERE id=?',(row['queue_id'],)).fetchone()
                    kind=row['identity']['pool_source_type'];fk='pool_item_id' if kind=='material' else 'drama_pool_item_id'
                    identity_col='material_id' if kind=='material' else 'content_id'
                    pool=c.execute('SELECT * FROM x_post_'+kind+'_pool WHERE id=?',(row['identity']['pool_item_id'],)).fetchone()
                    if (not pool or not q or q['manual_run_id'] or q[fk]!=pool['id']
                            or str(q[identity_col])!=str(pool[identity_col])
                            or pool['created_by_user_id']!=row['identity']['source_owner']
                            or pool['created_at']!=row['identity']['pool_created_at']):
                        raise AttributionError('pool_owner_source_changed')
                c.execute('UPDATE x_post_link_attribution SET context_json=? WHERE log_id=? AND context_json=?',(target,row['log_id'],expected))
                stamp=datetime.now(timezone.utc).isoformat()
                if rollback:
                    c.execute('UPDATE '+AUDIT+' SET rolled_back_at=? WHERE manifest_sha256=? AND log_id=?',(stamp,manifest['sha256'],row['log_id']))
                else:
                    c.execute('INSERT OR REPLACE INTO '+AUDIT+' VALUES(?,?,?,?,?,?,?)',(manifest['sha256'],row['log_id'],row['queue_id'],row['before'],row['after'],stamp,''))
                changed+=1
            c.commit()
            return dict(status='rolled_back' if rollback else 'applied',changed=changed,sha256=manifest['sha256'])
        except BaseException:
            c.rollback()
            raise
