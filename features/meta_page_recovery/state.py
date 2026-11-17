"""Durable incidents, per-Ad writes, hourly batches and notification outbox."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import uuid

from .domain import encoded, utcnow, digest, bad, page_health, FINAL, RETRY, UNCERTAIN, POOLS

DDL = '''
CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pages(page_id TEXT PRIMARY KEY,bad INTEGER NOT NULL,generation INTEGER NOT NULL,payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,started TEXT NOT NULL,finished TEXT,kind TEXT NOT NULL,status TEXT NOT NULL,error TEXT,summary TEXT);
CREATE TABLE IF NOT EXISTS incidents(id TEXT PRIMARY KEY,page_id TEXT NOT NULL,group_id INTEGER NOT NULL,generation INTEGER NOT NULL,run_id TEXT NOT NULL,state TEXT NOT NULL,cursor TEXT NOT NULL DEFAULT '',discovery_complete INTEGER NOT NULL DEFAULT 0,payload TEXT NOT NULL,UNIQUE(page_id,generation));
CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,incident_id TEXT NOT NULL,ad_id TEXT NOT NULL,account_id TEXT NOT NULL,status TEXT NOT NULL,reason TEXT,payload TEXT NOT NULL,proof TEXT NOT NULL DEFAULT '{}',updated TEXT NOT NULL,UNIQUE(incident_id,ad_id));
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status,account_id);
CREATE TABLE IF NOT EXISTS run_jobs(run_id TEXT NOT NULL,job_id TEXT NOT NULL,outcome TEXT NOT NULL,PRIMARY KEY(run_id,job_id));
CREATE TABLE IF NOT EXISTS attempts(id INTEGER PRIMARY KEY AUTOINCREMENT,job_id TEXT NOT NULL,stage TEXT NOT NULL,status TEXT NOT NULL,request TEXT NOT NULL,receipt TEXT,error TEXT,created TEXT NOT NULL,finished TEXT);
CREATE INDEX IF NOT EXISTS attempts_job ON attempts(job_id,id);
CREATE TABLE IF NOT EXISTS notifications(run_id TEXT PRIMARY KEY,status TEXT NOT NULL,uuid TEXT NOT NULL,body TEXT NOT NULL,message_id TEXT,error TEXT,attempts INTEGER NOT NULL DEFAULT 0,next_at TEXT);
'''


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.db() as db:
            db.executescript(DDL)
    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA synchronous=FULL')
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def new_run(self, kind='scan'):
        rid = uuid.uuid4().hex
        with self.db() as db:
            db.execute('INSERT INTO runs(id,started,kind,status) VALUES(?,?,?,?)', (rid, utcnow(), kind, 'scanning'))
        return rid

    def observe(self, run, pages):
        """One complete source snapshot advances detection atomically."""
        by_id = {}
        for page in pages:
            by_id.setdefault(str(page['page_id']), []).append(page)
        scope={str(g):dict(scope_pages=0,unknown_pages=0,pool_conflicts=0) for g in POOLS}
        for rows in by_id.values():
            groups={r['group_id'] for r in rows}
            for group in groups:
                values=[r for r in rows if r['group_id']==group];item=scope[str(group)]
                item['scope_pages']+=1
                item['unknown_pages']+=int(not any(bad(r) for r in values) and not all(page_health(r)=='normal' for r in values))
                item['pool_conflicts']+=int(len(groups)>1)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('UPDATE runs SET summary=? WHERE id=?',(encoded(dict(page_scope=scope)),run))
            baseline = db.execute("SELECT value FROM metadata WHERE key='baseline_at'").fetchone()
            t0 = baseline['value'] if baseline else utcnow()
            for pid, rows in by_id.items():
                old = db.execute('SELECT * FROM pages WHERE page_id=?', (pid,)).fetchone()
                page = rows[0]
                current_bad = any(bad(r) for r in rows)
                confirmed_normal=all(page_health(r)=='normal' for r in rows)
                remembered_bad=1 if current_bad else 0 if confirmed_normal else old['bad'] if old else -1
                generation = old['generation'] if old else 0
                # A newly joined but already abnormal Page is a baseline unless
                # its actual status transition is known to be after activation.
                new_after_t0 = (page.get('status_at') or 0) >= __import__('datetime').datetime.fromisoformat(t0).timestamp()
                trigger = bool(baseline and current_bad and ((old and old['bad']==0) or ((not old or old['bad']==-1) and new_after_t0)))
                if trigger:
                    generation += 1
                    incident = digest([pid, generation])[:32]
                    state = 'open' if len({r['group_id'] for r in rows}) == 1 else 'ambiguous_pool'
                    db.execute('INSERT INTO incidents(id,page_id,group_id,generation,run_id,state,payload) VALUES(?,?,?,?,?,?,?)',
                               (incident, pid, page['group_id'], generation, run, state, encoded(page)))
                if current_bad and len({r['group_id'] for r in rows})==1:
                    db.execute("UPDATE incidents SET state='open',group_id=?,payload=? WHERE page_id=? AND generation=? AND state='ambiguous_pool'",
                               (page['group_id'],encoded(page),pid,generation))
                if confirmed_normal:
                    db.execute("UPDATE incidents SET state='recovered_source' WHERE page_id=? AND state='open'", (pid,))
                db.execute('INSERT OR REPLACE INTO pages VALUES(?,?,?,?)', (pid, remembered_bad, generation, encoded(page)))
            if not baseline:
                db.execute("INSERT INTO metadata VALUES('baseline_at',?)", (t0,))
                db.execute("UPDATE runs SET kind='baseline' WHERE id=?", (run,))
            return not bool(baseline)

    def incidents(self):
        with self.db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM incidents WHERE state='open' AND discovery_complete=0 ORDER BY rowid")]

    def add_jobs(self, run, incident, rows, cursor, complete):
        with self.db() as db:
            for row in rows:
                jid = digest([incident['id'], str(row['ad_id'])])[:32]
                payload = dict(row, origin_page_id=incident['page_id'], group_id=incident['group_id'], incident_id=incident['id'])
                db.execute('INSERT OR IGNORE INTO jobs(id,incident_id,ad_id,account_id,status,payload,updated) VALUES(?,?,?,?,?,?,?)',
                           (jid, incident['id'], str(row['ad_id']), str(row['ad_account_id']), 'queued', encoded(payload), utcnow()))
                job = db.execute('SELECT status FROM jobs WHERE id=?', (jid,)).fetchone()
                outcome = 'pending' if job['status'] == 'queued' else ('waiting_previous_run' if job['status'] == 'running' else job['status'])
                db.execute('INSERT OR IGNORE INTO run_jobs VALUES(?,?,?)', (run, jid, outcome))
            db.execute('UPDATE incidents SET cursor=?,discovery_complete=? WHERE id=?', (cursor, int(complete), incident['id']))

    def schedule_retries(self, run):
        with self.db() as db:
            for row in db.execute("SELECT j.id,j.status FROM jobs j JOIN incidents i ON i.id=j.incident_id WHERE i.state='open' AND j.status IN ('retry_wait','waiting_no_candidate')").fetchall():
                db.execute("UPDATE jobs SET status='queued',updated=? WHERE id=?", (utcnow(), row['id']))
                db.execute('INSERT OR IGNORE INTO run_jobs VALUES(?,?,?)', (run, row['id'], 'pending'))
            # Unknown or earlier still-running work is visible in this hour's
            # report, but cannot acquire another mutation attempt.
            for row in db.execute("SELECT id,status FROM jobs WHERE status IN ('running','unknown_creative','unknown_ad','unknown_media')").fetchall():
                db.execute('INSERT OR IGNORE INTO run_jobs VALUES(?,?,?)', (run, row['id'], 'waiting_previous_run' if row['status']=='running' else row['status']))

    def scan_done(self, run, error=None):
        with self.db() as db:
            db.execute('UPDATE runs SET status=?,error=? WHERE id=?', ('scan_failed' if error else 'working', error, run))

    def claim(self, account):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM jobs WHERE status='queued' AND account_id=? ORDER BY rowid LIMIT 1", (account,)).fetchone()
            if not row:
                return None
            db.execute("UPDATE jobs SET status='running',updated=? WHERE id=?", (utcnow(), row['id']))
            return self.decode(row)

    @staticmethod
    def decode(row):
        item = dict(row)
        item['payload'] = json.loads(item['payload'])
        item['proof'] = json.loads(item['proof'])
        return item

    def jobs(self, states):
        if not states:
            return []
        with self.db() as db:
            return [self.decode(r) for r in db.execute('SELECT * FROM jobs WHERE status IN ('+','.join('?' for _ in states)+') ORDER BY rowid', list(states))]

    def result(self, job, status, reason='', proof=None):
        with self.db() as db:
            if proof is None:
                row = db.execute('SELECT proof FROM jobs WHERE id=?', (job,)).fetchone()
                proof = json.loads(row['proof'])
            db.execute('UPDATE jobs SET status=?,reason=?,proof=?,updated=? WHERE id=?', (status, reason, encoded(proof), utcnow(), job))
            db.execute("UPDATE run_jobs SET outcome=? WHERE job_id=? AND outcome='pending'", (status, job))

    def intent(self, job, stage, payload):
        with self.db() as db:
            row = db.execute("INSERT INTO attempts(job_id,stage,status,request,created) VALUES(?,?,'intent',?,?)", (job, stage, encoded(payload), utcnow()))
            return row.lastrowid

    def save_proof(self, job, proof):
        with self.db() as db:
            db.execute('UPDATE jobs SET proof=?,updated=? WHERE id=?',(encoded(proof),utcnow(),job))

    def touch(self,job):
        with self.db() as db:db.execute('UPDATE jobs SET updated=? WHERE id=?',(utcnow(),job))

    def receipt(self, attempt, status, receipt=None, error=None):
        with self.db() as db:
            db.execute('UPDATE attempts SET status=?,receipt=?,error=?,finished=? WHERE id=?', (status, encoded(receipt) if receipt is not None else None, error, utcnow(), attempt))

    def attempts(self, job):
        with self.db() as db:
            return [dict(r) for r in db.execute('SELECT * FROM attempts WHERE job_id=? ORDER BY id', (job,))]

    def recover_process(self):
        with self.db() as db:
            db.execute("UPDATE notifications SET status='unknown',error='process_interrupted_after_send_intent' WHERE status='sending'")
        for job in self.jobs({'running'}):
            pending = [a for a in self.attempts(job['id']) if a['status'] in ('intent', 'unknown')]
            if pending:
                stage = pending[-1]['stage']
                status = {'creative_create':'unknown_creative', 'ad_update':'unknown_ad'}.get(stage, 'unknown_media')
                self.result(job['id'], status, 'process_interrupted_after_intent')
            else:
                # A known created creative must be resumed, never re-created.
                self.result(job['id'], 'retry_wait', 'process_interrupted_before_write')

    def close_ready_runs(self):
        with self.db() as db:
            runs = db.execute("SELECT * FROM runs WHERE status IN ('working','scan_failed')").fetchall()
            for run in runs:
                outcomes = [dict(r) for r in db.execute('SELECT j.*,rj.outcome FROM run_jobs rj JOIN jobs j ON j.id=rj.job_id WHERE rj.run_id=?', (run['id'],))]
                if any(r['outcome'] == 'pending' for r in outcomes):
                    continue
                pools = {str(g):dict(name=v[1],new_abnormal_pages=0,scanned_ads=0,pur_eligible=0,changed=0,pending_review=0,deliverable=0,failed=0,unknown=0,waiting=0,skipped=0) for g,v in POOLS.items()}
                scope=(json.loads(run['summary']) if run['summary'] else {}).get('page_scope',{})
                for group,pool in pools.items():pool.update(scope.get(group,dict(scope_pages=None,unknown_pages=None,pool_conflicts=None)))
                for i in db.execute('SELECT group_id,state FROM incidents WHERE run_id=?', (run['id'],)):
                    pools[str(i['group_id'])]['new_abnormal_pages'] += 1
                details = []
                for row in outcomes:
                    data, proof = json.loads(row['payload']), json.loads(row['proof'])
                    p = pools[str(data['group_id'])]
                    p['scanned_ads'] += 1
                    if DecimalSafe(proof.get('pur')) > 0:p['pur_eligible'] += 1
                    state = row['outcome']
                    category = 'unknown' if state in UNCERTAIN else 'waiting' if state in RETRY or state=='waiting_previous_run' else state
                    if category not in p:category='skipped'
                    p[category] += 1
                    if len(details)<8 and (proof.get('new_page_id') or row['reason']):
                        details.append(dict(ad_id=row['ad_id'],origin_page_id=data['origin_page_id'],new_page_id=proof.get('new_page_id'),status=state,reason=row['reason']))
                summary=dict(run_id=run['id'],kind=run['kind'],started=run['started'],finished=utcnow(),error=run['error'],pools=pools,details=details)
                db.execute('UPDATE runs SET status=?,finished=?,summary=? WHERE id=?', ('failed' if run['error'] else 'complete',summary['finished'],encoded(summary),run['id']))
                db.execute("INSERT OR IGNORE INTO notifications(run_id,status,uuid,body) VALUES(?,'pending',?,?)",(run['id'],'mpr-'+run['id'],encoded(summary)))

    def notifications(self):
        with self.db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM notifications WHERE status IN ('pending','failed') AND (next_at IS NULL OR next_at<=?) ORDER BY rowid",(utcnow(),))]

    def notification_result(self, run, status, message_id=None, error=None, next_at=None):
        with self.db() as db:
            db.execute('UPDATE notifications SET status=?,message_id=?,error=?,attempts=attempts+1,next_at=? WHERE run_id=?',(status,message_id,error,next_at,run))

    def snapshot(self):
        with self.db() as db:
            return dict(baseline=dict(db.execute('SELECT key,value FROM metadata').fetchall()),
                        jobs=dict(db.execute('SELECT status,COUNT(*) FROM jobs GROUP BY status').fetchall()),
                        notifications=dict(db.execute('SELECT status,COUNT(*) FROM notifications GROUP BY status').fetchall()),
                        runs=[dict(r) for r in db.execute('SELECT id,kind,status,started,finished,error FROM runs ORDER BY rowid DESC LIMIT 12')])


def DecimalSafe(value):
    from decimal import Decimal, InvalidOperation
    try:return Decimal(str(value or 0))
    except InvalidOperation:return Decimal(0)
