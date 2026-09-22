#!/usr/bin/env python3
"""Resumable operator audit; reads on 63350, explicit fixed-table apply on 63353.

Keeps database snapshots, API evidence and a local SQLite ledger on the data disk.
This is a manual tool, not part of the daily cron. No media-channel mutations.
"""
import argparse
import collections
import concurrent.futures
import datetime
try:
    import fcntl
except ImportError:  # Pure planning/validation tests also run on Windows.
    fcntl = None
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import subprocess
import threading
import time
from decimal import Decimal

import tt_minis_bid_protection_sync as sync

FACT_FIELDS = ('product_id', 'product_name', 'minis_id', 'campaign_id', 'adgroup_id',
               'protection_status', 'status_detail', 'credit_amount_scaled', 'credit_amount', 'currency')
MONEY_FIELDS = ('credit_amount_scaled', 'credit_amount')


def dump(x):
    return json.dumps(x, ensure_ascii=False, default=str, separators=(',', ':'))


def emit(event, **fields):
    print(dump(dict(event=event, time=datetime.datetime.now(datetime.timezone.utc).isoformat(), **fields)), flush=True)


def signature(row):
    if row is None:
        return None
    return tuple(Decimal(str(row.get(k) or 0)) if k in MONEY_FIELDS else row.get(k) for k in FACT_FIELDS)


def key(row):
    return (str(row['record_date']), str(row['advertiser_id']), str(row['query_id']))


def evidence(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    with gzip.open(str(tmp), 'wt', encoding='utf-8') as f:
        json.dump(value, f, ensure_ascii=False, default=str)
    with tmp.open('rb') as f:
        os.fsync(f.fileno())
    os.replace(str(tmp), str(path))


def read_evidence(path):
    with gzip.open(str(path), 'rt', encoding='utf-8') as f:
        return json.load(f)


def read_connection():
    import pymysql
    c = pymysql.connect(**sync.mysql_connection_settings(port=63350), charset='utf8mb4',
        connect_timeout=5, read_timeout=180, write_timeout=5,
        cursorclass=pymysql.cursors.DictCursor, autocommit=True)
    with c.cursor() as q:
        q.execute('SELECT @@read_only AS ro')
        if q.fetchone()['ro'] != 1:
            c.close()
            raise RuntimeError('read-only endpoint guard failed')
        q.execute('SET SESSION MAX_EXECUTION_TIME=150000')
    return c


def query(sql, params=()):
    c = read_connection()
    try:
        with c.cursor() as q:
            q.execute(sql, params or None)
            return q.fetchall()
    finally:
        c.close()


def metadata():
    rows = query("""SELECT CAST(account_id AS CHAR) advertiser_id,
        JSON_UNQUOTE(JSON_EXTRACT(account_stats,'$.minis_id')) minis_id
        FROM kunlunads_dev.ads_accounts_setting
        WHERE account_stats LIKE '%minis_id%' AND platform_id='3'""")
    out = {}
    for row in rows:
        aid = sync.normalize_id(row['advertiser_id'])
        mid = row['minis_id']
        if mid not in sync.MINIS_PRODUCT_MAP:
            raise RuntimeError('unknown minis mapping for account ' + aid)
        item = dict(sync.MINIS_PRODUCT_MAP[mid], minis_id=mid)
        if aid in out and out[aid] != item:
            raise RuntimeError('conflicting account mapping')
        out[aid] = item
    if not out:
        raise RuntimeError('empty approved account pool')
    return out


def table_day(day):
    rows = query('SELECT * FROM ' + sync.TARGET_TABLE + ' WHERE record_date=%s ORDER BY advertiser_id,query_id', (day,))
    return {key(r): json.loads(dump(r)) for r in rows}


def source_day(day, accounts):
    # Exact frozen advertiser IDs; IN avoids the expensive casted derived-table join.
    marks = ','.join(['%s'] * len(accounts))
    return query("""SELECT CAST(advertiser_id AS CHAR) advertiser_id,
        CAST(campaign_id AS CHAR) query_id,ROUND(SUM(stat_cost),6) spend
        FROM kunlunads_dev.ads_tiktok_insights FORCE INDEX (dt)
        WHERE dt=%s AND category=0 AND campaign_id<>0 AND advertiser_id IN (""" + marks + """)
        GROUP BY advertiser_id,campaign_id HAVING SUM(stat_cost)>0""", (day,) + tuple(accounts))


def candidate(day, aid, qid, meta):
    return dict(meta, record_date=day, advertiser_id=aid, data_level='CAMPAIGN',
                query_id=qid, campaign_id=qid, adgroup_id=None, source_adgroup_id='')


def collect(db, root, meta, dates):
    for day in dates:
        if db.execute('SELECT 1 FROM days WHERE day=?', (day,)).fetchone():
            continue
        old = table_day(day)
        evidence(root / 'baseline' / (day + '.json.gz'), list(old.values()))
        source = source_day(day, meta)
        candidates = {}
        for r in source:
            aid, qid = sync.normalize_id(r['advertiser_id']), sync.normalize_id(r['query_id'])
            candidates[(day, aid, qid)] = candidate(day, aid, qid, meta[aid])
        for k, r in old.items():
            if r['data_level'] != 'CAMPAIGN' or r['campaign_id'] != r['query_id'] or r['adgroup_id'] is not None:
                raise RuntimeError('stored row violates campaign-only contract')
            if r['advertiser_id'] not in meta or any(r[x] != meta[r['advertiser_id']][x] for x in ('product_id', 'minis_id')):
                raise RuntimeError('historical account mapping needs review: ' + r['advertiser_id'])
            candidates.setdefault(k, candidate(*k, meta[k[1]]))
        db.executemany('INSERT INTO facts(day,account,qid,old_json,candidate_json) VALUES(?,?,?,?,?)',
            [(k[0], k[1], k[2], dump(old[k]) if k in old else None, dump(v)) for k, v in candidates.items()])
        db.execute('INSERT INTO days VALUES(?,?,?,?)', (day, len(source), len(old), len(candidates)))
        db.commit()
        emit('collected', day=day, source=len(source), baseline=len(old), candidates=len(candidates),
             missing_from_table=len(set(candidates) - set(old)))


def plan_windows(day_ids):
    """Minimum request count with TikTok's 200 / calendar-day-count limit."""
    dates = sorted(day_ids)
    n = len(dates)
    cost, choice = [0] * (n + 1), {}
    for i in range(n - 1, -1, -1):
        ids = set()
        best = None
        for j in range(i, n):
            span = (sync.parse_day(dates[j]) - sync.parse_day(dates[i])).days + 1
            if span > 60:
                break
            ids.update(day_ids[dates[j]])
            cap = 200 // span
            count = (len(ids) + cap - 1) // cap
            value = count + cost[j + 1]
            if best is None or value < best[0]:
                best = (value, j, cap, tuple(sorted(ids)))
        cost[i], choice[i] = best[0], best[1:]
    windows, i = [], 0
    while i < n:
        j, cap, ids = choice[i]
        for part in sync.chunks(list(ids), cap):
            windows.append((dates[i], dates[j], part))
        i = j + 1
    return windows


def plan(db):
    if db.execute("SELECT COUNT(*) FROM tasks WHERE id NOT LIKE 'seed-%'").fetchone()[0]:
        return
    total = 0
    accounts = [r[0] for r in db.execute('SELECT DISTINCT account FROM facts WHERE checked_at IS NULL ORDER BY account')]
    for aid in accounts:
        day_ids = collections.defaultdict(set)
        for day, qid in db.execute('SELECT day,qid FROM facts WHERE account=? AND checked_at IS NULL', (aid,)):
            day_ids[day].add(qid)
        for start, end, ids in plan_windows(day_ids):
            params = dict(advertiser_id=aid, data_level='CAMPAIGN', query_ids=dump(ids), start_date=start, end_date=end)
            tid = hashlib.sha256(dump(params).encode()).hexdigest()[:24]
            db.execute('INSERT INTO tasks(id,params,status) VALUES(?,?,?)', (tid, dump(params), 'pending'))
            marks = ','.join(['?'] * len(ids))
            db.execute('UPDATE facts SET task_id=? WHERE account=? AND day BETWEEN ? AND ? AND checked_at IS NULL AND qid IN (' + marks + ')',
                       (tid, aid, start, end) + tuple(ids))
            total += 1
    db.commit()
    assert db.execute('SELECT COUNT(*) FROM facts WHERE checked_at IS NULL AND task_id IS NULL').fetchone()[0] == 0
    emit('planned', tasks=total, accounts=len(accounts))


def normalize_response(params, payload, expected):
    if payload.get('code') not in (0, '0'):
        raise RuntimeError('unsuccessful API evidence')
    ids = set(json.loads(params['query_ids']))
    found, extras, seen = {}, [], set()
    for raw in (payload.get('data') or {}).get('bid_protection_records', []):
        day, qid = str(raw.get('record_date')), str(raw.get('query_id'))
        k = (day, params['advertiser_id'], qid)
        if k in seen or qid not in ids or not params['start_date'] <= day <= params['end_date']:
            raise RuntimeError('duplicate or out-of-scope API response')
        seen.add(k)
        if raw.get('data_level') != 'CAMPAIGN':
            raise RuntimeError('non-campaign API response')
        if k not in expected:
            extras.append(raw)
            if Decimal(str(raw.get('credit_amount') or '0')) != 0:
                raise RuntimeError('positive compensation outside daily candidate set needs review')
            continue
        found[k] = sync.normalize_history_records([raw], {qid: expected[k]}, day, 'CAMPAIGN')[0]
    return found, extras


def record_result(db, root, tid, params, payload, checked):
    evidence(root / 'api' / (tid + '.json.gz'), dict(params=params, response=payload, checked_at=checked))
    rows = db.execute('SELECT day,account,qid,candidate_json,old_json FROM facts WHERE task_id=?', (tid,)).fetchall()
    expected = {(r[0], r[1], r[2]): json.loads(r[3]) for r in rows}
    normalized, extras = normalize_response(params, payload, expected)
    absent_existing = [r[:3] for r in rows if r[4] and tuple(r[:3]) not in normalized]
    if absent_existing:
        raise RuntimeError('API omitted %d existing records; retain originals' % len(absent_existing))
    for r in rows:
        k = tuple(r[:3])
        db.execute('UPDATE facts SET new_json=?,checked_at=?,request_id=?,api_missing=? WHERE day=? AND account=? AND qid=?',
            (dump(normalized[k]) if k in normalized else None, checked, payload.get('request_id'), int(k not in normalized)) + k)
    db.execute("UPDATE tasks SET status='done',error=NULL WHERE id=?", (tid,))
    db.commit()
    return len(normalized), len(rows) - len(normalized), len(extras)


def audit(db, root, workers):
    jobs = [(r[0], json.loads(r[1])) for r in db.execute("SELECT id,params FROM tasks WHERE status<>'done' ORDER BY id")]
    client = sync.TikTokBidProtectionClient(sync.load_access_token(), timeout=35, max_retries=2)
    limiter, next_time = threading.Lock(), [0.0]
    def fetch(job):
        tid, params = job
        with limiter:
            time.sleep(max(0, next_time[0] - time.monotonic()))
            next_time[0] = time.monotonic() + 0.12
        checked = datetime.datetime.now(datetime.timezone.utc).isoformat()
        return tid, params, client.get_json(sync.HISTORY_URL, params), checked
    complete = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch, j): j[0] for j in jobs}
        for future in concurrent.futures.as_completed(futures):
            tid = futures[future]
            try:
                args = future.result()
                record_result(db, root, *args)
            except Exception as exc:
                error = sync.redact_error(exc)
                db.execute("UPDATE tasks SET status='failed',error=? WHERE id=?", (error, tid))
                db.commit()
                emit('task_failed', task=tid, error=error)
            complete += 1
            if complete % 100 == 0:
                emit('audit_progress', completed_this_run=complete, total_this_run=len(jobs))
    emit('audit_complete', task_status=dict(db.execute('SELECT status,COUNT(*) FROM tasks GROUP BY status').fetchall()))


def seed(db, root, source):
    """Reuse same-day raw read-only evidence; never accept a computed summary."""
    with gzip.open(source, 'rt', encoding='utf-8-sig') as f:
        for line in f:
            event = json.loads(line)
            if event.get('event') != 'api_response':
                continue
            params, payload, checked = event['params'], event['response'], event['started_at_utc']
            if checked[:10] != datetime.datetime.now(datetime.timezone.utc).date().isoformat():
                raise RuntimeError('seed evidence is not from today')
            start, end, aid = params['start_date'], params['end_date'], params['advertiser_id']
            if start != end or not db.execute('SELECT 1 FROM days WHERE day=?', (start,)).fetchone():
                raise RuntimeError('seed day has not been collected')
            ids = json.loads(params['query_ids'])
            tid = 'seed-' + hashlib.sha256(dump(params).encode()).hexdigest()[:24]
            db.execute('INSERT OR IGNORE INTO tasks(id,params,status) VALUES(?,?,?)', (tid, dump(params), 'pending'))
            marks = ','.join(['?'] * len(ids))
            db.execute('UPDATE facts SET task_id=? WHERE day=? AND account=? AND qid IN (' + marks + ')', (tid,start,aid) + tuple(ids))
            record_result(db, root, tid, params, payload, checked)
    emit('seed_complete', checked=db.execute('SELECT COUNT(*) FROM facts WHERE checked_at IS NOT NULL').fetchone()[0])


def apply_day(db, root, day):
    expected_count = db.execute('SELECT COUNT(*) FROM facts WHERE day=? AND checked_at IS NULL', (day,)).fetchone()[0]
    if expected_count:
        raise RuntimeError('unverified candidate rows on ' + day)
    current = table_day(day)
    wanted, changes, before, missing = [], [], [], []
    for aid, qid, old_json, new_json, checked in db.execute('SELECT account,qid,old_json,new_json,checked_at FROM facts WHERE day=?', (day,)):
        k = (day, aid, qid)
        old = json.loads(old_json) if old_json else None
        new = json.loads(new_json) if new_json else None
        actual = current.get(k)
        if new is None:
            if actual:
                missing.append(k)
            continue
        wanted.append(new)
        if signature(actual) == signature(new):
            continue
        if signature(actual) != signature(old):
            raise RuntimeError('concurrent change for ' + str(k))
        before.append(dict(key=k, row=actual))
        changes.append(new)
    if missing:
        raise RuntimeError('current database contains omitted API records')
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    evidence(root / 'apply-backups' / (day + '_' + stamp + '.json.gz'), before)
    evidence(root / 'apply-plans' / (day + '_' + stamp + '.json.gz'), changes)
    # Serialized fixed-table writer, small batches; no DDL, deletes or SQL reads on 63353.
    for batch in sync.chunks(changes, 100):
        sync.write_history_rows(batch)
        time.sleep(0.1)
    for attempt in range(4):
        actual = table_day(day)
        mismatches = [key(r) for r in wanted if signature(actual.get(key(r))) != signature(r)]
        if not mismatches:
            break
        time.sleep(2)
    if mismatches:
        raise RuntimeError('readback mismatches: %d on %s' % (len(mismatches), day))
    db.execute('INSERT OR REPLACE INTO applied(day,changed,verified,at) VALUES(?,?,?,?)',
        (day, len(changes), len(wanted), datetime.datetime.now(datetime.timezone.utc).isoformat()))
    db.commit()
    emit('day_applied', day=day, changed=len(changes), verified=len(wanted), readback_mismatches=0)


def summary(db, root):
    groups = {}
    for day, old_j, new_j, checked, missing in db.execute('SELECT day,old_json,new_json,checked_at,api_missing FROM facts'):
        old = json.loads(old_j) if old_j else None
        new = json.loads(new_j) if new_j else None
        row = new or old
        if row is None:
            continue
        gkey = (day, row['product_name'], row.get('currency') or '')
        g = groups.setdefault(gkey, dict(day=day, product=row['product_name'], currency=row.get('currency') or '',
            old_rows=0, new_rows=0, changed=0, inserted=0, old_paid=0, new_paid=0,
            old_amount=Decimal(0), new_amount=Decimal(0), pending=0, unchecked=0))
        if old:
            g['old_rows'] += 1
            g['old_amount'] += Decimal(str(old['credit_amount']))
            g['old_paid'] += old['protection_status'] == 'PAYMENT_COMPLETE'
        if new:
            g['new_rows'] += 1
            g['new_amount'] += Decimal(str(new['credit_amount']))
            g['new_paid'] += new['protection_status'] == 'PAYMENT_COMPLETE'
            g['pending'] += new['protection_status'] in sync.NON_TERMINAL_STATUSES
            g['changed'] += signature(old) != signature(new)
            g['inserted'] += old is None
        g['unchecked'] += checked is None
    data = dict(groups=list(groups.values()), days=list(db.execute('SELECT * FROM days ORDER BY day')),
        applied=list(db.execute('SELECT * FROM applied ORDER BY day')),
        task_status=dict(db.execute('SELECT status,COUNT(*) FROM tasks GROUP BY status')),
        rows=db.execute('SELECT COUNT(*) FROM facts').fetchone()[0],
        unchecked=db.execute('SELECT COUNT(*) FROM facts WHERE checked_at IS NULL').fetchone()[0],
        sparse_missing=db.execute('SELECT COUNT(*) FROM facts WHERE api_missing=1').fetchone()[0])
    (root / 'summary.json').write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    emit('summary', rows=data['rows'], unchecked=data['unchecked'], task_status=data['task_status'], applied_days=len(data['applied']))


def main():
    p = argparse.ArgumentParser()
    p.add_argument('action', choices=('collect','plan','audit','seed','apply','summary'))
    p.add_argument('--audit-dir', required=True)
    p.add_argument('--start-date', default='2026-07-24')
    p.add_argument('--end-date', default='2026-09-21')
    p.add_argument('--dates', help='optional comma-separated subset for collect/apply')
    p.add_argument('--seed-file')
    p.add_argument('--workers', type=int, default=6)
    args = p.parse_args()
    root = Path(args.audit_dir).resolve()
    if not str(root).startswith('/mnt/data-disk/tt-minis-bid-protection/audits/'):
        raise RuntimeError('audit directory must be below the service data-disk audits directory')
    uuid = subprocess.check_output(['findmnt','-no','UUID','/mnt/data-disk'], universal_newlines=True).strip()
    if uuid != '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':
        raise RuntimeError('data disk mount guard failed')
    dates = list(sync.each_day(args.start_date,args.end_date))
    if len(dates)>60 or sync.parse_day(dates[-1]) >= sync.beijing_today():
        raise RuntimeError('audit range must be at most 60 completed days')
    chosen = args.dates.split(',') if args.dates else dates
    if not set(chosen).issubset(dates) or not 1<=args.workers<=6:
        raise RuntimeError('invalid scope or concurrency')
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(str(root),0o700)
    with (root/'operator.lock').open('a') as run_lock:
        fcntl.flock(run_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        scope = root/'scope.json'
        if scope.exists():
            frozen=json.loads(scope.read_text())
            if frozen['dates']!=dates: raise RuntimeError('audit scope changed')
            meta=frozen['metadata']
        else:
            meta=metadata();scope.write_text(dump(dict(dates=dates,metadata=meta)),encoding='utf-8')
        db=sqlite3.connect(str(root/'ledger.sqlite3'))
        db.executescript('''
        CREATE TABLE IF NOT EXISTS facts(day TEXT,account TEXT,qid TEXT,old_json TEXT,candidate_json TEXT,
          new_json TEXT,checked_at TEXT,request_id TEXT,api_missing INTEGER DEFAULT 0,task_id TEXT,
          PRIMARY KEY(day,account,qid));
        CREATE INDEX IF NOT EXISTS facts_account ON facts(account);
        CREATE INDEX IF NOT EXISTS facts_task ON facts(task_id);
        CREATE TABLE IF NOT EXISTS days(day TEXT PRIMARY KEY,source INTEGER,baseline INTEGER,candidates INTEGER);
        CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY,params TEXT,status TEXT,error TEXT);
        CREATE TABLE IF NOT EXISTS applied(day TEXT PRIMARY KEY,changed INTEGER,verified INTEGER,at TEXT);
        ''')
        if args.action=='collect': collect(db,root,meta,chosen)
        elif args.action=='plan':
            if db.execute('SELECT COUNT(*) FROM days').fetchone()[0]!=len(dates): raise RuntimeError('collect all dates first')
            plan(db)
        elif args.action=='audit': audit(db,root,args.workers)
        elif args.action=='seed': seed(db,root,args.seed_file)
        elif args.action=='apply':
            with open('/tmp/tt_minis_bid_protection_sync.lock','a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                for day in chosen: apply_day(db,root,day)
        summary(db,root)
        db.close()


if __name__=='__main__':
    main()
