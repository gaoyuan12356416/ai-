"""Temporarily park two paid CI readbacks; preserve jobs, generations and receipts."""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

JOBS = (63812, 64267)
DISPATCH = '/mnt/data-disk/pipeline-dispatch/dispatch.sqlite3'
LEDGER = '/mnt/data-disk/pipeline-cos-runtime/state/actions.sqlite3'
DIRECTORY = Path('/mnt/data-disk/moboreels-library/deployments/drama-catalog-complete-20261117T094503Z/private')
JOURNAL = DIRECTORY / 'parked-ci-readbacks.json'


def conn(path, write=False):
    c = sqlite3.connect('file:'+path+'?mode='+('rw' if write else 'ro'), uri=True, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def saved(value):
    temporary = JOURNAL.with_suffix('.next')
    temporary.write_text(json.dumps(value, indent=2)+'\n')
    os.chmod(temporary, 0o600)
    temporary.replace(JOURNAL)


def work(key):
    with contextlib.closing(conn(LEDGER)) as c:
        row = c.execute('SELECT * FROM work WHERE identity=?', (key,)).fetchone()
        return dict(row) if row else None


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def inspect():
    evidence = []
    with contextlib.closing(conn(DISPATCH)) as c, contextlib.closing(conn(LEDGER)) as l:
        cfg = json.loads(c.execute('SELECT config_json FROM pipeline_config WHERE id=1').fetchone()[0])
        assert 'x' not in cfg['enabled_channels'], 'X admission must remain held'
        for job_id in JOBS:
            job = dict(c.execute('SELECT * FROM pipeline_job WHERE id=?', (job_id,)).fetchone())
            assert job['channel']=='x' and job['stage']=='prepare' and job['node_id']=='cos-cloud'
            assert job['status']=='running' and job['generation']==1 and not job['external_intent']
            key = '%s:%s' % (job_id, job['generation'])
            row = dict(l.execute('SELECT * FROM work WHERE identity=?', (key,)).fetchone())
            assert row['paid']==1 and not row['receipt'] and not row['route']
            action_keys = [r[0] for r in c.execute('SELECT action_key FROM pipeline_cloud_intent WHERE job_id=? AND generation=?', (job_id,job['generation']))]
            actions = [dict(l.execute('SELECT identity,provider_id,state FROM action WHERE identity=?', (k,)).fetchone()) for k in action_keys]
            assert actions and all(a['provider_id'] and a['state'] in ('Running','Submitted','Success') for a in actions)
            assert any(a['state'] in ('Running','Submitted') for a in actions), 'No pending paid CI operation'
            native = json.loads(row['job'])
            assert native['id']==job_id and native['generation']==job['generation']
            assert native['node_id']==job['node_id'] and native['worker_id']==job['worker_id']
            assert digest(native['claim_token'])==job['claim_hash'], 'Claim identity changed'
            evidence.append(dict(job_id=job_id, key=key, job_hash=digest(row['job']),
                original_next_poll=row['next_poll'], providers=actions, status='verified'))
    return evidence


def main():
    p = argparse.ArgumentParser()
    p.add_argument('action', choices=('check','park','resume'))
    p.add_argument('--source', required=True, type=Path)
    p.add_argument('--operation-sha', required=True)
    args = p.parse_args()
    blob = subprocess.check_output(['git','-C',str(args.source),'show',args.operation_sha+':ops/drama-catalog-complete-20261117/park_cloud_readbacks.py'])
    assert blob==Path(__file__).read_bytes(), 'Use the exact fetched operation'
    assert subprocess.check_output(['git','-C',str(args.source),'for-each-ref','--contains='+args.operation_sha,'--format=%(refname)','refs/remotes/origin']).strip()
    if args.action=='check':
        print(json.dumps({'verified': inspect()})); return
    if args.action=='resume':
        journal = json.loads(JOURNAL.read_text())
        for item in journal['jobs']:
            if item['status']!='parked': continue
            with contextlib.closing(conn(LEDGER, True)) as c:
                c.execute('BEGIN IMMEDIATE')
                row = c.execute('SELECT * FROM work WHERE identity=?', (item['key'],)).fetchone()
                if not row or row['receipt']:
                    item['status']='receipted_or_completed'
                else:
                    assert digest(row['job'])==item['job_hash']
                    assert row['next_poll']==item['held_next_poll'], 'Another owner changed retry timing'
                    c.execute('UPDATE work SET next_poll=? WHERE identity=?', (time.time(),item['key']))
                    item['status']='resumed_original_work'
                c.commit()
            saved(journal)
        print(json.dumps({'jobs':[{'id':r['job_id'],'status':r['status']} for r in journal['jobs']]})); return
    assert not JOURNAL.exists(), 'Reconcile the existing journal before any retry'
    journal = dict(operation_sha=args.operation_sha, created=time.time(), jobs=inspect())
    saved(journal)
    pid = subprocess.check_output(['systemctl','show','pipeline-cos-node.service','-p','MainPID','--value'],text=True).strip()
    runtime = (Path('/proc')/pid/'cwd').resolve()
    assert str(runtime)=='/mnt/data-disk/pipeline-cos-runtime/worker-versions/88d64c6afc60bc95501fd61ece140cb6a44e9b94'
    sys.path.insert(0,str(runtime))
    from features.pipeline_dispatch.cloud_node import CloudClient
    from features.pipeline_dispatch.worker import ExecutionContext
    cfg = json.loads(Path('/etc/pipeline-cos-cloud/worker.json').read_text())
    credentials = json.loads(Path(cfg['node_config']).read_text())
    assert credentials['node_id']=='cos-cloud' and cfg['state_root']==str(Path(LEDGER).parent)
    client = CloudClient(cfg.get('coordinator_url','http://127.0.0.1:18846'), credentials['node_token'],timeout=30)
    for item in journal['jobs']:
        row = work(item['key']); assert digest(row['job'])==item['job_hash'] and not row['receipt']
        identity = ExecutionContext(json.loads(row['job']), client, None, '').identity
        response = client.post('jobs/cos-park', identity)
        assert response.get('accepted')
        item['status']='waiting_for_owner_to_postpone'; saved(journal)
    deadline = time.monotonic()+60
    while any(i['status']=='waiting_for_owner_to_postpone' for i in journal['jobs']):
        for item in journal['jobs']:
            if item['status']!='waiting_for_owner_to_postpone': continue
            with contextlib.closing(conn(LEDGER, True)) as c:
                c.execute('BEGIN IMMEDIATE')
                row = c.execute('SELECT * FROM work WHERE identity=?', (item['key'],)).fetchone()
                if not row or row['receipt']:
                    item['status']='receipted_or_completed'
                elif row['next_poll'] > time.time()+5 and row['next_poll'] != item['original_next_poll']:
                    assert digest(row['job'])==item['job_hash'] and row['paid']==1
                    # The owning worker has exited its readback and postponed it.
                    # Delay only that retry; paid actions/claims/locks are intact.
                    until = time.time()+900
                    c.execute('UPDATE work SET next_poll=? WHERE identity=?', (until,item['key']))
                    item.update(status='parked', held_next_poll=until)
                c.commit()
            saved(journal)
        if time.monotonic()>deadline:
            raise RuntimeError('Owner did not yield; keep receipts and inspect journal')
        time.sleep(1)
    print(json.dumps({'jobs':[{'id':r['job_id'],'status':r['status'],'automatic_retry_deadline':r.get('held_next_poll')} for r in journal['jobs']]}))


if __name__=='__main__':
    main()
