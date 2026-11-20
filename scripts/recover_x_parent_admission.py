"""Restore auto-parent admission for explicitly audited unstarted recoveries.

The generic run counter sync changes an untouched auto parent to queued, while
the native auto bridge requires running. Restore only those audited parents and
requeue only acknowledged reservations rejected before any native binding.
"""
import argparse
import contextlib
import datetime as dt
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.recover_x_unstarted_media import connect, rows, require, digest
from scripts.recover_x_media_interrupted import backup

TABLE = 'pipeline_x_parent_admission_recovery'
SCHEMA = 'CREATE TABLE IF NOT EXISTS ' + TABLE + '(source_key TEXT PRIMARY KEY, proof_json TEXT NOT NULL, updated_at TEXT NOT NULL)'


def validate_parent(p):
    q, parent = p['queue'], p['parent']
    require(p['audited_recovery'], 'not_audited_unstarted_recovery')
    require(q['status'] == 'queued' and parent['trigger_source'] == 'auto_template'
            and parent['status'] == 'queued' and not parent['error_code']
            and not parent['unknown_count'] and not parent['failed_count']
            and parent['expected_count'] == 1 and p['sibling_count'] == 1,
            'parent_not_unstarted_auto_queue')
    require(q['account_id'] in json.loads(parent['account_ids_json']), 'account_changed')
    require(not p['operations'] and not p['publications'] and not p['holds'], 'external_fact_present')
    require(all(l['status'] == 'reserved' and not any(l[k] for k in
                ('attempt_count', 'unknown_outcome', 'x_media_id', 'x_post_id', 'published_at'))
                for l in p['logs']), 'publish_attempt_present')


def validate_reservation(p):
    j = p['job']
    require(j['channel'] == 'x' and j['status'] == 'failed' and j['generation'] > 0
            and j['error_code'] == 'x_manual_parent_not_publishable'
            and not j['external_intent'] and not j['canonical_receipt'] and not j['hold_reason'],
            'not_failed_parent_reservation')
    require(not any(p[k] for k in ('operations', 'checkpoints', 'receipts', 'resources',
                                   'legacy_resources', 'current_native_claims')), 'execution_already_bound')
    matching = [d for d in p['deliveries'] if d['generation'] == j['generation']
                and d['claim_hash'] == j['claim_hash']
                and json.loads(d['response_json']).get('status') == 'failed'
                and json.loads(d['response_json']).get('accepted') is True]
    require(bool(matching), 'terminal_failure_not_acknowledged')


def parent_proof(n, c, key):
    q = dict(n.execute('SELECT * FROM x_post_queue WHERE id=?', (int(key.split(':')[-1]),)).fetchone())
    parent = n.execute('SELECT * FROM x_post_manual_run WHERE id=?', (q['manual_run_id'],)).fetchone()
    if not parent or parent['status'] != 'queued':
        return None
    return {'queue': q, 'parent': dict(parent),
            'audited_recovery': bool(n.execute('SELECT 1 FROM pipeline_x_unstarted_media_recovery WHERE source_key=?', (key,)).fetchone()),
            'sibling_count': n.execute('SELECT count(*) FROM x_post_queue WHERE manual_run_id=?', (q['manual_run_id'],)).fetchone()[0],
            'logs': rows(n, 'SELECT * FROM x_post_publish_log WHERE queue_id=?', (q['id'],)),
            'operations': rows(n, 'SELECT * FROM pipeline_x_operation WHERE source_key=?', (key,)),
            'holds': rows(n, 'SELECT * FROM pipeline_canonical_hold WHERE source_key=?', (key,)),
            'publications': rows(c, 'SELECT * FROM pipeline_publication WHERE source_key=?', (key,))}


def failed_proof(n, c, j):
    p = {'job': j}
    for field, table in [('operations', 'pipeline_operation'), ('checkpoints', 'pipeline_checkpoint'),
                         ('receipts', 'pipeline_receipt'), ('resources', 'pipeline_resource'),
                         ('legacy_resources', 'pipeline_legacy_resource'), ('deliveries', 'pipeline_delivery')]:
        p[field] = rows(c, 'SELECT * FROM '+table+' WHERE job_id=?', (j['id'],))
    p['current_native_claims'] = rows(n, 'SELECT * FROM pipeline_x_claim WHERE source_key=? AND stage=? AND generation=?',
                                    (j['source_key'], j['stage'], j['generation']))
    return p


def run(scope, central, natives, directory, apply=False):
    # Short admission hold; existing jobs and their receipts continue normally.
    if apply:
        require(Path('/run/pipeline-service-drain/x-post-automation.service.json').exists(), 'admission_not_held')
        for label, path in [('central', central), *natives.items()]:
            if not (Path(directory)/(label+'.sqlite3')).exists():
                backup(path, Path(directory), label)
    result = {'parents': [], 'reservations': [], 'excluded': [], 'platform_called': False}
    for s in scope:
        key = s['source_key']; native = natives['tag' if int(s['source_id']) >= 9000000000000 else 'x']
        with contextlib.closing(connect(central, not apply)) as c, contextlib.closing(connect(native, not apply)) as n:
            if apply:
                n.execute(SCHEMA); n.commit(); c.execute(SCHEMA); c.commit()
                n.execute('BEGIN IMMEDIATE'); c.execute('BEGIN IMMEDIATE')
            p = parent_proof(n, c, key)
            if p:
                try: validate_parent(p)
                except ValueError as exc:
                    result['excluded'].append({'source_key': key, 'reason': str(exc)})
                    continue
                if apply:
                    now = dt.datetime.now(dt.timezone.utc).isoformat()
                    n.execute('INSERT OR IGNORE INTO '+TABLE+' VALUES(?,?,?)', (key, json.dumps(p), now))
                    n.execute("UPDATE x_post_manual_run SET status='running',updated_at=? WHERE id=? AND status='queued'",
                              (now, p['parent']['id']))
                result['parents'].append(key)
            # Only this recovery's parents may authorize a second reservation.
            authorized = p is not None or (apply and n.execute('SELECT 1 FROM '+TABLE+' WHERE source_key=?', (key,)).fetchone())
            if authorized:
                jobs = rows(c, "SELECT * FROM pipeline_job WHERE source_key=? AND status='failed'", (key,))
                for j in jobs:
                    proof = failed_proof(n, c, j)
                    try: validate_reservation(proof)
                    except ValueError as exc:
                        result['excluded'].append({'job_id': j['id'], 'reason': str(exc)})
                        continue
                    if apply:
                        now = dt.datetime.now(dt.timezone.utc).isoformat()
                        c.execute('INSERT OR IGNORE INTO '+TABLE+' VALUES(?,?,?)', (key, json.dumps(proof), now))
                        c.execute("UPDATE pipeline_job SET status=?,hold_reason='',error_code='',error_message='',started_at='',finished_at='',lease_expires_at='',claim_hash='',node_id='',worker_id='',updated_at=? WHERE id=?",
                                  ('queued' if j['stage']=='prepare' else 'ready', now, j['id']))
                        c.execute('INSERT INTO pipeline_event(job_id,event_type,details_json,created_at) VALUES(?,?,?,?)',
                                  (j['id'], 'audited_parent_admission_recovery', json.dumps({'prior_generation':j['generation'],'proof_sha256':digest(proof)}), now))
                    result['reservations'].append(j['id'])
            if apply: n.commit(); c.commit()
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--scope', required=True); p.add_argument('--central', required=True)
    p.add_argument('--x', required=True); p.add_argument('--tag', required=True)
    p.add_argument('--backup-dir', required=True); p.add_argument('--output', required=True)
    p.add_argument('--apply', action='store_true'); a = p.parse_args()
    result = run(json.loads(Path(a.scope).read_text()), a.central, {'x':a.x,'tag':a.tag}, a.backup_dir, a.apply)
    Path(a.output).write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result))

if __name__ == '__main__': main()
