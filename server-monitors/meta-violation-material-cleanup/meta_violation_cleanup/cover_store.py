"""Durable, per-operation SQLite queue for the cover replacement workflow.

Only the caller holding the exclusive worker process lock may recover work.
Every network-write intent is committed first. Unknown writes never replay.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import sqlite3


class CoverStoreError(RuntimeError):
    pass


_SECRET = {'access_token', 'refresh_token', 'token', 'password', 'app_secret',
           'client_secret', 'authorization', 'cookie', 'page_access_token'}
_TERMINAL = {'succeeded', 'failed', 'unknown', 'obsolete', 'blocked'}


def now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def _json(value):
    def check(item):
        if isinstance(item, dict):
            for key, child in item.items():
                if str(key).lower() in _SECRET:
                    raise CoverStoreError('secret_metadata_forbidden')
                check(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                check(child)
    check(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _id(value):
    value = str(value or '').removeprefix('act_')
    if not re.fullmatch(r'[1-9][0-9]*', value):
        raise CoverStoreError('invalid_positive_id')
    return value


def _row(row):
    if row is None:
        return None
    result = dict(row)
    for key in ('candidate', 'result', 'payload', 'meta', 'evidence', 'basis', 'summary'):
        if key in result:
            result[key] = json.loads(result[key])
    return result


_SCHEMA = (
    '''CREATE TABLE IF NOT EXISTS materials (
        root_id TEXT NOT NULL, material_id TEXT NOT NULL, first_seen TEXT NOT NULL,
        evidence TEXT NOT NULL, PRIMARY KEY(root_id, material_id))''',
    '''CREATE TABLE IF NOT EXISTS batches (
        id TEXT PRIMARY KEY, status TEXT NOT NULL, created_at TEXT NOT NULL,
        sealed_at TEXT, meta TEXT NOT NULL, notified INTEGER NOT NULL DEFAULT 0,
        summary TEXT NOT NULL DEFAULT '{}')''',
    '''CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id TEXT NOT NULL REFERENCES batches(id),
        account_id TEXT NOT NULL, ad_id TEXT NOT NULL, old_creative_id TEXT NOT NULL,
        status TEXT NOT NULL, stage TEXT NOT NULL DEFAULT 'queued',
        request_sent INTEGER NOT NULL DEFAULT 0, candidate TEXT NOT NULL,
        result TEXT NOT NULL DEFAULT '{}', next_check_at TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE(account_id, ad_id, old_creative_id))''',
    '''CREATE TABLE IF NOT EXISTS resources (
        key TEXT PRIMARY KEY, account_id TEXT NOT NULL, batch_id TEXT NOT NULL,
        status TEXT NOT NULL, request_sent INTEGER NOT NULL DEFAULT 0,
        payload TEXT NOT NULL, result TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS deletions (
        account_id TEXT NOT NULL, creative_id TEXT NOT NULL, batch_id TEXT NOT NULL,
        user_id TEXT NOT NULL, status TEXT NOT NULL, request_sent INTEGER NOT NULL DEFAULT 0,
        basis TEXT NOT NULL, result TEXT NOT NULL DEFAULT '{}', next_check_at TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        PRIMARY KEY(account_id, creative_id))''',
    '''CREATE TABLE IF NOT EXISTS cover_creatives (
        account_id TEXT NOT NULL, creative_id TEXT NOT NULL,
        created_at TEXT NOT NULL, PRIMARY KEY(account_id,creative_id))''',
    '''CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id TEXT, object_key TEXT NOT NULL,
        action TEXT NOT NULL, created_at TEXT NOT NULL, details TEXT NOT NULL)''',
    'CREATE INDEX IF NOT EXISTS tasks_ready ON tasks(status,batch_id,id)',
    'CREATE INDEX IF NOT EXISTS tasks_waiting ON tasks(status,next_check_at,id)',
    'CREATE INDEX IF NOT EXISTS tasks_creative_status ON tasks(account_id,old_creative_id,status)',
    'CREATE INDEX IF NOT EXISTS deletions_ready ON deletions(status,next_check_at)',
    'CREATE INDEX IF NOT EXISTS events_batch ON events(batch_id,id)',
)


class CoverStore:
    def __init__(self, path):
        if str(path) == ':memory:':
            raise CoverStoreError('durable_database_required')
        self.path = str(Path(path).resolve())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._tx() as db:
            cover_index_exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cover_creatives'").fetchone()
            for sql in _SCHEMA:
                db.execute(sql)
            if not cover_index_exists:
                for table, status in (('resources','accepted'),('tasks','succeeded')):
                    for row in db.execute('SELECT account_id,result FROM '+table+' WHERE status=?',(status,)).fetchall():
                        result = json.loads(row['result'])
                        if result.get('new_creative_id'):
                            self._remember_cover(db,row['account_id'],result['new_creative_id'])

    @contextmanager
    def _tx(self, write=True):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA busy_timeout=30000')
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _event(db, batch_id, key, action, value):
        db.execute('INSERT INTO events(batch_id,object_key,action,created_at,details) VALUES(?,?,?,?,?)',
                   (batch_id, key, action, now(), _json(value)))

    @staticmethod
    def _remember_cover(db, account_id, creative_id):
        db.execute('INSERT OR IGNORE INTO cover_creatives VALUES(?,?,?)',
                   (_id(account_id),_id(creative_id),now()))

    def is_cover_creative(self, account_id, creative_id):
        """An indexed accepted cover must not become a new hourly input."""
        with self._tx(False) as db:
            return db.execute('SELECT 1 FROM cover_creatives WHERE account_id=? AND creative_id=?',
                              (_id(account_id),_id(creative_id))).fetchone() is not None

    def add_triggers(self, evidence):
        materials = evidence.get('materials', evidence)
        added = 0
        with self._tx() as db:
            for mid, value in materials.items():
                mid = _id(mid)
                ids = {_id(a) for a in value.get('violating_ad_ids', [])}
                if len(ids) < 3 or value.get('count', len(ids)) != len(ids):
                    continue
                roots = value.get('root_ids') or [value.get('root_id')]
                for root in roots:
                    root = _id(root)
                    if root not in ('1479', '3302'):
                        raise CoverStoreError('trigger_product_out_of_scope')
                    saved = dict(value, material_id=mid, root_id=root)
                    for key in ('window_start', 'window_end', 'policy_version'):
                        if key in evidence:
                            saved[key] = evidence[key]
                    added += db.execute('INSERT OR IGNORE INTO materials VALUES(?,?,?,?)',
                                        (root, mid, now(), _json(saved))).rowcount
        return added

    def triggered_materials(self):
        result = {}
        with self._tx(False) as db:
            for row in db.execute('SELECT * FROM materials ORDER BY first_seen,material_id,root_id'):
                value = json.loads(row['evidence'])
                prior = result.setdefault(row['material_id'], dict(value, root_ids=[], triggers=[]))
                prior['root_ids'].append(row['root_id'])
                prior['triggers'].append(dict(value, first_seen=row['first_seen']))
        return result

    def begin_batch(self, batch_id, meta):
        with self._tx() as db:
            existing = db.execute('SELECT * FROM batches WHERE id=?', (batch_id,)).fetchone()
            if existing:
                if existing['meta'] != _json(meta):
                    raise CoverStoreError('batch_identity_conflict')
                return False
            db.execute('INSERT INTO batches(id,status,created_at,meta) VALUES(?,?,?,?)',
                       (batch_id, 'scanning', now(), _json(meta)))
            return True

    def seal_batch(self, batch_id, meta=None):
        with self._tx() as db:
            row = db.execute('SELECT * FROM batches WHERE id=?', (batch_id,)).fetchone()
            if not row:
                raise CoverStoreError('batch_not_found')
            if row['status'] != 'scanning':
                return
            values = json.loads(row['meta'])
            values.update(meta or {})
            db.execute('UPDATE batches SET status=?,sealed_at=?,meta=? WHERE id=?',
                       ('sealed', now(), _json(values), batch_id))

    def fail_batch(self, batch_id, result):
        with self._tx() as db:
            if db.execute("UPDATE batches SET status=?,sealed_at=?,summary=? WHERE id=? AND status='scanning'",
                          ('failed', now(), _json(result), batch_id)).rowcount != 1:
                raise CoverStoreError('only_open_scan_can_fail')

    def enqueue(self, batch_id, candidate):
        account = _id(candidate['account_id'])
        ad = _id(candidate['ad_id'])
        creative = _id(candidate.get('old_creative_id') or candidate.get('creative_id'))
        candidate = dict(candidate, account_id=account, ad_id=ad, old_creative_id=creative)
        with self._tx() as db:
            batch = db.execute('SELECT status FROM batches WHERE id=?', (batch_id,)).fetchone()
            if not batch or batch['status'] != 'scanning':
                raise CoverStoreError('batch_not_open')
            at = now()
            existing = db.execute('''SELECT t.*,b.status batch_status FROM tasks t JOIN batches b ON b.id=t.batch_id
                WHERE t.account_id=? AND t.ad_id=? AND t.old_creative_id=?''',(account,ad,creative)).fetchone()
            if existing and existing['batch_status']=='failed' and existing['status']=='pending' and not existing['request_sent'] and existing['stage']=='queued':
                db.execute("UPDATE tasks SET batch_id=?,candidate=?,result='{}',updated_at=? WHERE id=?",(batch_id,_json(candidate),at,existing['id']))
                self._event(db,batch_id,'task:'+str(existing['id']),'rehome_unsent_scan',{'previous_batch_id':existing['batch_id']})
                return True
            cursor = db.execute('''INSERT OR IGNORE INTO tasks
                (batch_id,account_id,ad_id,old_creative_id,status,candidate,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?)''',
                (batch_id, account, ad, creative, 'pending', _json(candidate), at, at))
            return cursor.rowcount == 1

    def known_keys(self):
        with self._tx(False) as db:
            return {(r['account_id'], r['ad_id'], r['old_creative_id'])
                    for r in db.execute('''SELECT t.account_id,t.ad_id,t.old_creative_id FROM tasks t JOIN batches b ON b.id=t.batch_id
                        WHERE NOT (b.status='failed' AND t.status='pending' AND t.request_sent=0 AND t.stage='queued')''')}

    existing_pairs = known_keys

    def get_batches(self, limit=20):
        with self._tx(False) as db:
            return [_row(r) for r in db.execute('SELECT * FROM batches ORDER BY created_at DESC LIMIT ?', (int(limit),))]

    def batch_details(self, batch_id):
        with self._tx(False) as db:
            batch = _row(db.execute('SELECT * FROM batches WHERE id=?',(batch_id,)).fetchone())
        if batch:
            batch['counts'] = self.stats(batch_id)
        return batch

    def ready_tasks(self, limit=64):
        with self._tx(False) as db:
            return [_row(r) for r in db.execute('''SELECT t.* FROM tasks t JOIN batches b ON b.id=t.batch_id
                WHERE t.status='pending' AND b.status='sealed' ORDER BY b.created_at,t.id LIMIT ?''', (int(limit),))]

    def get_task(self, task_id):
        with self._tx(False) as db:
            return _row(db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone())

    def claim_task(self, task_id):
        with self._tx() as db:
            return db.execute("UPDATE tasks SET status='in_progress',stage='preflight',updated_at=? WHERE id=? AND status='pending'",
                              (now(), task_id)).rowcount == 1

    def mark_task_write(self, task_id, stage='binding', intent=None):
        with self._tx() as db:
            changed = db.execute("UPDATE tasks SET request_sent=1,stage=?,result=?,updated_at=? WHERE id=? AND status='in_progress' AND request_sent=0",
                                 (stage, _json(dict(intent or {}, request_sent=True)), now(), task_id)).rowcount
            if changed != 1:
                raise CoverStoreError('task_write_already_attempted')
            row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            self._event(db, row['batch_id'], 'task:'+str(task_id), 'write_intent', dict(intent or {}, stage=stage))

    def finish_task(self, task_id, status, result):
        if status not in _TERMINAL | {'waiting_review'}:
            raise CoverStoreError('invalid_task_status')
        with self._tx() as db:
            row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            if not row:
                raise CoverStoreError('task_not_found')
            reconciling = row['status'] == 'unknown' and status == 'succeeded' and result.get('confirmed_binding') is True
            if row['status'] in _TERMINAL and not reconciling:
                if row['status'] == status and row['result'] == _json(result):
                    return
                raise CoverStoreError('terminal_task_immutable')
            if row['status'] not in ('pending', 'in_progress', 'waiting_review') and not reconciling:
                raise CoverStoreError('task_not_active')
            if status == 'waiting_review' and row['request_sent']:
                raise CoverStoreError('written_task_cannot_wait_review')
            if status == 'succeeded' and (not result.get('new_creative_id') or result.get('confirmed_binding') is not True):
                raise CoverStoreError('replacement_proof_missing')
            next_at = (datetime.now(timezone.utc)+timedelta(hours=1)).isoformat() if status == 'waiting_review' else None
            db.execute('UPDATE tasks SET status=?,result=?,next_check_at=?,updated_at=? WHERE id=?',
                       (status, _json(result), next_at, now(), task_id))
            if status == 'succeeded':
                self._remember_cover(db,row['account_id'],result['new_creative_id'])
                self._enqueue_delete(db, db.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone())
            self._event(db, row['batch_id'], 'task:'+str(task_id), status, result)

    def defer_task(self, task_id, result):
        with self._tx() as db:
            changed = db.execute("UPDATE tasks SET status='pending',stage='queued',result=?,updated_at=? WHERE id=? AND status='in_progress' AND request_sent=0",
                                 (_json(result), now(), task_id)).rowcount
            if changed != 1:
                raise CoverStoreError('cannot_defer_written_task')

    def due_waiting(self, now=None, limit=1000):
        cutoff = now or globals()['now']()
        with self._tx(False) as db:
            return [_row(r) for r in db.execute("SELECT * FROM tasks WHERE status='waiting_review' AND (next_check_at IS NULL OR next_check_at<=?) ORDER BY id LIMIT ?", (cutoff, int(limit)))]

    def update_waiting(self, task_id, result, next_check_at=None):
        due = next_check_at or (datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()
        with self._tx() as db:
            if db.execute("UPDATE tasks SET result=?,next_check_at=?,updated_at=? WHERE id=? AND status='waiting_review'",
                          (_json(result), due, now(), task_id)).rowcount != 1:
                raise CoverStoreError('task_not_waiting')

    def requeue_waiting(self, task_id, batch_id, candidate=None):
        with self._tx() as db:
            row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            if not row or row['status'] != 'waiting_review' or row['request_sent']:
                return False
            saved = json.loads(row['candidate'])
            if candidate:
                if any(str(candidate.get(k, row[k])) != str(row[k]) for k in ('account_id','ad_id','old_creative_id')):
                    raise CoverStoreError('waiting_identity_conflict')
                saved.update(candidate)
            db.execute("UPDATE tasks SET batch_id=?,status='pending',stage='queued',candidate=?,next_check_at=NULL,updated_at=? WHERE id=?",
                       (batch_id, _json(saved), now(), task_id))
            return True

    def claim_resource(self, key, account_id, batch_id, payload):
        account_id = _id(account_id)
        encoded = _json(payload)
        with self._tx() as db:
            row = db.execute('SELECT * FROM resources WHERE key=?', (key,)).fetchone()
            if row:
                if row['account_id'] != account_id or row['payload'] != encoded:
                    raise CoverStoreError('resource_identity_conflict')
                if row['status'] == 'pending' and not row['request_sent']:
                    db.execute("UPDATE resources SET status='in_progress',updated_at=? WHERE key=?", (now(),key))
                    return dict(_row(db.execute('SELECT * FROM resources WHERE key=?',(key,)).fetchone()),claimed=True)
                return dict(_row(row), claimed=False)
            at = now()
            db.execute('INSERT INTO resources(key,account_id,batch_id,status,payload,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                       (key, account_id, batch_id, 'in_progress', encoded, at, at))
            return dict(_row(db.execute('SELECT * FROM resources WHERE key=?',(key,)).fetchone()), claimed=True)

    def get_resource(self, key):
        with self._tx(False) as db:
            return _row(db.execute('SELECT * FROM resources WHERE key=?', (key,)).fetchone())

    def mark_resource_write(self, key):
        with self._tx() as db:
            if db.execute("UPDATE resources SET request_sent=1,updated_at=? WHERE key=? AND status='in_progress' AND request_sent=0", (now(), key)).rowcount != 1:
                raise CoverStoreError('resource_write_already_attempted')

    def finish_resource(self, key, status, result):
        if status == 'succeeded':
            status = 'accepted'
        if status not in ('accepted', 'failed', 'unknown'):
            raise CoverStoreError('invalid_resource_status')
        with self._tx() as db:
            row = db.execute('SELECT * FROM resources WHERE key=?', (key,)).fetchone()
            if not row:
                raise CoverStoreError('resource_not_found')
            if row['status'] != 'in_progress':
                if row['status'] == status and row['result'] == _json(result):
                    return
                raise CoverStoreError('terminal_resource_immutable')
            db.execute('UPDATE resources SET status=?,result=?,updated_at=? WHERE key=?', (status, _json(result), now(), key))
            if status == 'accepted':
                self._remember_cover(db,row['account_id'],result.get('new_creative_id'))

    def defer_resource(self, key, result):
        with self._tx() as db:
            if db.execute("UPDATE resources SET status='pending',result=?,updated_at=? WHERE key=? AND status='in_progress' AND request_sent=0",(_json(result),now(),key)).rowcount != 1:
                raise CoverStoreError('cannot_defer_written_resource')

    def resolve_resource(self, key, result):
        """Accept exact read-only proof; never replay the uncertain CREATE."""
        proof = result.get('readback')
        new_id = _id(result.get('new_creative_id'))
        if result.get('verified') is not True or not isinstance(proof, dict) or str(proof.get('id')) != new_id:
            raise CoverStoreError('resource_readback_proof_required')
        with self._tx() as db:
            row = db.execute('SELECT * FROM resources WHERE key=?',(key,)).fetchone()
            if not row or row['status'] != 'unknown':
                raise CoverStoreError('resource_not_unknown')
            saved = json.loads(row['result'])
            if str(saved.get('new_creative_id') or '') != new_id or str(proof.get('account_id') or '').removeprefix('act_') != row['account_id']:
                raise CoverStoreError('resource_readback_identity_mismatch')
            db.execute("UPDATE resources SET status='accepted',result=?,updated_at=? WHERE key=?",
                       (_json(result),now(),key))
            self._remember_cover(db,row['account_id'],new_id)
            resumed = 0
            for task in db.execute("SELECT * FROM tasks WHERE status='unknown' AND request_sent=0 AND stage!='binding'").fetchall():
                outcome = json.loads(task['result'])
                resource = outcome.get('resource') or {}
                if (task['account_id'] == row['account_id'] and outcome.get('reason') == 'creative_dependency_unknown'
                        and resource.get('resource_key') == key):
                    db.execute("UPDATE tasks SET status='pending',stage='queued',result=?,updated_at=? WHERE id=?",
                               (_json({'reason':'resource_readonly_verified','resource_key':key,'request_sent':False}),now(),task['id']))
                    self._event(db,task['batch_id'],'task:'+str(task['id']),'resource_readonly_verified',{'resource_key':key,'new_creative_id':new_id})
                    resumed += 1
            self._event(db,row['batch_id'],'resource:'+key,'readonly_verified',result)
            return resumed

    @staticmethod
    def _enqueue_delete(db, task):
        if not task or task['status'] != 'succeeded':
            raise CoverStoreError('confirmed_replacement_required')
        result = json.loads(task['result'])
        if result.get('confirmed_binding') is not True:
            raise CoverStoreError('confirmed_replacement_required')
        new_id = _id(result.get('new_creative_id'))
        if new_id == task['old_creative_id']:
            raise CoverStoreError('replacement_creative_unchanged')
        candidate = json.loads(task['candidate'])
        user = _id(result.get('user_id') or candidate['user_id'])
        at = now()
        basis = {'task_id': task['id'], 'ad_id': task['ad_id'], 'new_creative_id': new_id,
                 'replacement_result': result, 'candidate': candidate}
        return db.execute('''INSERT OR IGNORE INTO deletions
            (account_id,creative_id,batch_id,user_id,status,basis,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?)''', (task['account_id'],task['old_creative_id'],task['batch_id'],user,'pending',_json(basis),at,at)).rowcount == 1

    def enqueue_delete(self, task_id):
        with self._tx() as db:
            return self._enqueue_delete(db,db.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone())

    def pending_deletions(self, limit=1000):
        with self._tx(False) as db:
            # The independent cleanup lane must not repeatedly scan an account
            # while known replacements of the same Creative are still pending.
            # This is only a scheduling filter; complete Meta reference checks,
            # reservations and the once-delete intent remain mandatory.
            return [_row(r) for r in db.execute('''SELECT d.* FROM deletions d
                WHERE d.status IN ('pending','waiting_references','reference_scan_incomplete')
                  AND (d.next_check_at IS NULL OR d.next_check_at<=?)
                  AND NOT EXISTS (
                    SELECT 1 FROM tasks t JOIN batches b ON b.id=t.batch_id
                    WHERE t.account_id=d.account_id AND t.old_creative_id=d.creative_id
                      AND t.status IN ('pending','in_progress','waiting_review','unknown')
                      AND NOT (b.status='failed' AND t.status='pending'
                               AND t.request_sent=0 AND t.stage='queued'))
                ORDER BY d.created_at LIMIT ?''', (now(), int(limit)))]

    def get_deletion(self, account_id, creative_id):
        with self._tx(False) as db:
            return _row(db.execute('SELECT * FROM deletions WHERE account_id=? AND creative_id=?', (_id(account_id),_id(creative_id))).fetchone())

    get_delete = get_deletion

    def unknown_tasks(self, limit=1000):
        with self._tx(False) as db:
            return [_row(r) for r in db.execute("SELECT * FROM tasks WHERE status='unknown' ORDER BY id LIMIT ?",(int(limit),))]

    def unknown_deletions(self, limit=1000):
        with self._tx(False) as db:
            return [_row(r) for r in db.execute("SELECT * FROM deletions WHERE status='unknown' ORDER BY updated_at LIMIT ?",(int(limit),))]

    def unknown_resources(self, limit=1000):
        with self._tx(False) as db:
            return [_row(r) for r in db.execute("SELECT * FROM resources WHERE status='unknown' ORDER BY updated_at LIMIT ?",(int(limit),))]

    def set_delete_waiting(self, account_id, creative_id, result, batch_id=None):
        status = 'reference_scan_incomplete' if result.get('complete') is False else 'waiting_references'
        due = (datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()
        with self._tx() as db:
            sql = 'UPDATE deletions SET status=?,result=?,next_check_at=?,updated_at=?'
            args = [status,_json(result),due,now()]
            if batch_id:
                sql += ',batch_id=?'; args.append(batch_id)
            sql += " WHERE account_id=? AND creative_id=? AND request_sent=0 AND status IN ('pending','waiting_references','reference_scan_incomplete')"
            args.extend((_id(account_id),_id(creative_id)))
            if db.execute(sql,args).rowcount != 1:
                raise CoverStoreError('delete_not_waitable')

    def claim_delete_once(self, account_id, creative_id):
        with self._tx() as db:
            return db.execute("UPDATE deletions SET status='in_progress',request_sent=1,updated_at=? WHERE account_id=? AND creative_id=? AND request_sent=0 AND status IN ('pending','waiting_references','reference_scan_incomplete')",
                              (now(),_id(account_id),_id(creative_id))).rowcount == 1

    def finish_delete(self, account_id, creative_id, status, result):
        if status not in ('deleted_verified','previous_deleted','failed','unknown','blocked'):
            raise CoverStoreError('invalid_delete_status')
        with self._tx() as db:
            account_id,creative_id = _id(account_id),_id(creative_id)
            row = db.execute('SELECT * FROM deletions WHERE account_id=? AND creative_id=?',(account_id,creative_id)).fetchone()
            if not row:
                raise CoverStoreError('delete_not_found')
            if row['status'] in ('deleted_verified','previous_deleted','failed','blocked'):
                if row['status'] == status and row['result'] == _json(result):
                    return
                raise CoverStoreError('terminal_delete_immutable')
            if row['status'] == 'unknown' and status not in ('deleted_verified','unknown'):
                raise CoverStoreError('unknown_delete_readonly_reconciliation_required')
            if status in ('deleted_verified','previous_deleted') and result.get('confirmed_deleted') is not True:
                raise CoverStoreError('deletion_proof_required')
            db.execute('UPDATE deletions SET status=?,result=?,updated_at=? WHERE account_id=? AND creative_id=?',
                       (status,_json(result),now(),account_id,creative_id))
            self._event(db,row['batch_id'],'creative:'+creative_id,status,result)

    def recover_inflight(self):
        """Caller must exclusively own worker lock and prove old worker exited."""
        counts = {}
        with self._tx() as db:
            # Tasks with no Ad-binding intent can be re-prepared: resource
            # intents below remain fenced and will never recreate a Creative.
            counts['tasks_pending'] = db.execute("UPDATE tasks SET status='pending',stage='queued',updated_at=? WHERE status='in_progress' AND request_sent=0",(now(),)).rowcount
            for table in ('tasks','resources','deletions'):
                rows = db.execute("SELECT rowid,* FROM "+table+" WHERE status='in_progress' AND request_sent=1").fetchall()
                counts[table] = len(rows)
                for row in rows:
                    saved = json.loads(row['result'])
                    saved.update(reason='worker_interrupted',request_sent=True)
                    db.execute("UPDATE "+table+" SET status='unknown',result=?,updated_at=? WHERE rowid=?",(_json(saved),now(),row['rowid'] if 'rowid' in row.keys() else row['id']))
            counts['resources_unsent'] = db.execute("UPDATE resources SET status='pending',result=?,updated_at=? WHERE status='in_progress' AND request_sent=0",(_json({'reason':'worker_interrupted_before_create','request_sent':False}),now())).rowcount
        return counts

    def stats(self, batch_id=None):
        result = {}
        with self._tx(False) as db:
            for table in ('tasks','resources','deletions'):
                query = 'SELECT status,COUNT(*) n FROM '+table
                args = ()
                if batch_id:
                    query += ' WHERE batch_id=?'; args=(batch_id,)
                result[table] = {r['status']:r['n'] for r in db.execute(query+' GROUP BY status',args)}
        return result

    def finishable_batches(self):
        with self._tx(False) as db:
            return [_row(r) for r in db.execute('''SELECT b.* FROM batches b WHERE b.notified=0
                AND (b.status='failed' OR (b.status='sealed' AND NOT EXISTS
                (SELECT 1 FROM tasks t WHERE t.batch_id=b.id AND t.status IN ('pending','in_progress'))
                AND NOT EXISTS (SELECT 1 FROM deletions d WHERE d.batch_id=b.id AND d.status IN ('pending','in_progress'))))
                ORDER BY b.created_at''')]

    def notification_candidates(self, include_pending=False):
        """Include meaningful late deletion/review changes after an earlier notice."""
        with self._tx(False) as db:
            sql = '''SELECT b.* FROM batches b WHERE
                b.status='failed' OR (b.status='sealed' AND NOT EXISTS
                (SELECT 1 FROM tasks t WHERE t.batch_id=b.id AND t.status IN ('pending','in_progress'))
                AND NOT EXISTS (SELECT 1 FROM deletions d WHERE d.batch_id=b.id AND d.status IN ('pending','in_progress')))
                '''
            if include_pending:
                sql += ''' OR (b.status='sealed' AND EXISTS
                    (SELECT 1 FROM tasks t WHERE t.batch_id=b.id AND t.status NOT IN ('pending','in_progress')))'''
            rows = [_row(r) for r in db.execute(sql + ' ORDER BY b.created_at')]
        result=[]
        for row in rows:
            counts=self.stats(row['id'])
            previous = row['summary'].get('counts') or {}
            old_tasks, tasks = previous.get('tasks',{}), counts['tasks']
            # Moving an awaiting-review task into the next hour changes queue
            # counters but is not a new outcome for its already-notified batch.
            changed = (any(old_tasks.get(k,0) != tasks.get(k,0) for k in _TERMINAL)
                       or tasks.get('waiting_review',0) > old_tasks.get('waiting_review',0)
                       or counts['deletions'] != previous.get('deletions',{}))
            if not row['notified'] or changed:
                row['counts']=counts
                row['notification_attempt']=int(row['summary'].get('notification_attempt',0))+1
                result.append(row)
        return result

    def mark_batch_notified(self, batch_id, summary):
        with self._tx() as db:
            row=db.execute('SELECT * FROM batches WHERE id=?',(batch_id,)).fetchone()
            if not row:
                raise CoverStoreError('batch_not_found')
            previous=json.loads(row['summary'])
            saved=dict(summary)
            if 'counts' not in saved:
                saved['counts']={table:{r['status']:r['n'] for r in db.execute('SELECT status,COUNT(*) n FROM '+table+' WHERE batch_id=? GROUP BY status',(batch_id,))} for table in ('tasks','resources','deletions')}
            saved.setdefault('notification_attempt',int(previous.get('notification_attempt',0))+1)
            db.execute('UPDATE batches SET notified=1,summary=? WHERE id=?',(_json(saved),batch_id))

    def close(self):
        pass
