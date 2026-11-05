"""Audited child plans for only unqueued accounts of an explicitly scoped day.

No queue or platform write occurs here. Original batches, failed queues and
their reservations remain intact; normal planners perform current preflight.
"""
import contextlib
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.pipeline_dispatch.x_schedule_recovery import SCHEMA as ADMISSION_SCHEMA, identity, fingerprint
from scripts.recover_x_prepost_failures import backup, writable
from scripts.recover_x_unstarted_manual import connect, require, rows

TABLE = 'x_post_missing_scope_child_audit'
SCHEMA = '''CREATE TABLE IF NOT EXISTS x_post_missing_scope_child_audit (
 parent_run_id INTEGER PRIMARY KEY,child_run_id INTEGER NOT NULL UNIQUE,
 plan_sha256 TEXT NOT NULL,actor TEXT NOT NULL,created_at TEXT NOT NULL,
 backup_path TEXT NOT NULL,evidence_json TEXT NOT NULL)'''

def snapshot(c, parent_id, day, minute):
    require(dt.date.fromisoformat(day).isoformat() == day and
        re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', minute), 'exact_date_time_required')
    found = rows(c, 'SELECT * FROM x_post_schedule_run WHERE id=?', (parent_id,))
    require(len(found) == 1, 'parent_missing'); parent = found[0]
    require(parent['run_date'] == day and parent['source_type'] in ('material','drama')
        and parent['status'] in ('completed','completed_with_errors') and not parent['unknown_count'], 'parent_not_settled')
    queues = rows(c, 'SELECT q.id,q.account_id,q.status,l.status AS log_status,l.unknown_outcome,l.x_post_id '
        'FROM x_post_queue q LEFT JOIN x_post_publish_log l ON l.queue_id=q.id WHERE q.schedule_run_id=? ORDER BY q.id', (parent_id,))
    require(queues and len(queues) == parent['queued_count'] and all(
        q['status'] in ('published','failed') and q['log_status'] == q['status'] and q['unknown_outcome'] == 0
        and (q['status'] != 'published' or q['x_post_id']) for q in queues), 'parent_has_unresolved_queue')
    scope = json.loads(parent['account_ids_json'])
    random = rows(c, 'SELECT * FROM x_post_schedule_random_plan WHERE source_type=? AND run_date=?', (parent['source_type'],day))
    require(len(random) == 1 and random[0]['config_version'] == parent['config_version']
        and json.loads(random[0]['account_ids_json']) == scope
        and parent['publish_time'] in json.loads(random[0]['publish_times_json']), 'original_scope_not_proven')
    occupied = {q['account_id'] for q in queues}
    require(len(occupied) == len(queues) and occupied.issubset(scope), 'parent_pairing_invalid')
    missing = [a for a in scope if a not in occupied]
    require(missing, 'no_missing_accounts')
    for legacy in ('x_post_schedule_codefix_compensation_audit','x_post_schedule_drama_scope_compensation_audit'):
        if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(legacy,)).fetchone():
            require(not c.execute('SELECT 1 FROM '+legacy+' WHERE original_schedule_run_id=?',(parent_id,)).fetchone(),'prior_compensation_exists')
    if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='x_post_operator_gap_recovery_audit'").fetchone():
        require(not c.execute("SELECT 1 FROM x_post_operator_gap_recovery_audit WHERE action='same_day_skipped_material_accounts_v1' AND identity=?",(parent_id,)).fetchone(),'prior_compensation_exists')
    require(not c.execute('SELECT 1 FROM x_post_schedule_run WHERE source_type=? AND run_date=? AND publish_time=?',
        (parent['source_type'],day,minute)).fetchone() and minute not in json.loads(random[0]['publish_times_json']), 'child_slot_occupied')
    if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone():
        require(not c.execute('SELECT 1 FROM '+TABLE+' WHERE parent_run_id=?', (parent_id,)).fetchone(), 'child_already_exists')
    return dict(parent=parent,queues=queues,random_plan=random[0],missing_account_ids=missing,publish_time=minute)

def plan(path,parent_id,day,minute):
    with contextlib.closing(connect(path)) as c:
        c.execute('BEGIN'); result=dict(kind='x_missing_scope_child',version=1,db_path=str(Path(path).resolve()),
            parent_id=parent_id,day=day,minute=minute,proof=snapshot(c,parent_id,day,minute))
    result['plan_sha256']=fingerprint(result);return result

def apply(saved,folder,actor):
    require(saved.get('kind')=='x_missing_scope_child' and saved.get('version')==1
        and saved['plan_sha256']==fingerprint({k:v for k,v in saved.items() if k!='plan_sha256'}) and str(actor).strip(), 'plan_or_actor_invalid')
    path=saved['db_path']
    with contextlib.closing(connect(path)) as c:
        if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone():
            prior=c.execute('SELECT child_run_id,plan_sha256 FROM '+TABLE+' WHERE parent_run_id=?',(saved['parent_id'],)).fetchone()
            if prior:
                require(prior['plan_sha256']==saved['plan_sha256'],'different_child_already_exists')
                return dict(already_applied=True,child_run_id=prior['child_run_id'],platform_called=False)
        require(snapshot(c,saved['parent_id'],saved['day'],saved['minute'])==saved['proof'],'scope_drift')
    target=backup(path,folder,'missing-scope')
    backup_sha=hashlib.sha256(Path(target).read_bytes()).hexdigest()
    with contextlib.closing(writable(path)) as c:
        c.execute('BEGIN IMMEDIATE')
        try:
            proof=snapshot(c,saved['parent_id'],saved['day'],saved['minute'])
            require(proof==saved['proof'],'scope_drift');c.execute(SCHEMA);c.execute(ADMISSION_SCHEMA)
            parent=proof['parent'];now=dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
            child={k:parent[k] for k in ('source_type','run_date','timezone','config_version','body_template','schedule_mode','random_template_enabled','duration_policy_version','material_pool_snapshot_max_id')}
            child.update(slot_key='xpost:schedule:missing-scope:v1:'+str(parent['id']),publish_time=saved['minute'],
                account_ids_json=json.dumps(proof['missing_account_ids']),expected_count=len(proof['missing_account_ids']),
                status='claimed',created_at=now,updated_at=now,lease_heartbeat_at=now)
            cols=list(child)
            rid=c.execute('INSERT INTO x_post_schedule_run('+','.join(cols)+') VALUES('+','.join('?' for _ in cols)+')',[child[k] for k in cols]).lastrowid
            current=dict(c.execute('SELECT * FROM x_post_schedule_run WHERE id=?',(rid,)).fetchone())
            c.execute('INSERT INTO '+TABLE+' VALUES(?,?,?,?,?,?,?)',(parent['id'],rid,saved['plan_sha256'],actor,now,target,json.dumps(proof,ensure_ascii=False)))
            c.execute('INSERT INTO x_post_schedule_zero_write_resume_audit VALUES(?,?,?,?,?,?,?,?)',
                (rid,fingerprint(identity(current)),saved['plan_sha256'],actor,'explicit_missing_account_child',now,
                 backup_sha,json.dumps(dict(parent_run_id=parent['id'],proof=proof),ensure_ascii=False)))
            c.commit()
        except BaseException:c.rollback();raise
    return dict(applied=True,parent_run_id=parent['id'],child_run_id=rid,account_ids=proof['missing_account_ids'],
        backup_path=target,platform_called=False,queues_created=0)
