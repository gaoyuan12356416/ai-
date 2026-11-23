"""Redeliver original callbacks after their coordinator reconciliation fix."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time


def main():
    p=argparse.ArgumentParser();p.add_argument('--apply',action='store_true');a=p.parse_args()
    pid=int(subprocess.check_output(['systemctl','show','pipeline-node-hk-gpu.service','-p','MainPID','--value']))
    os.environ.update(dict(x.decode().split('=',1) for x in Path('/proc/%d/environ'%pid).read_bytes().split(b'\0') if b'=' in x))
    sys.path.insert(0,'/data/social-480-20261119/hk-2a40e97d42ad/candidate')
    from features.pipeline_dispatch.receipt_outbox import ReceiptOutbox
    from features.pipeline_dispatch.client import CoordinatorClient
    from features.pipeline_dispatch.worker import configuration
    config=configuration();path=Path(config['state_root'])/'receipts.sqlite3'
    c=sqlite3.connect(str(path),timeout=20);c.row_factory=sqlite3.Row
    c.execute('BEGIN IMMEDIATE')
    rows=[dict(r) for r in c.execute("SELECT * FROM receipt_outbox WHERE acked_at IS NULL AND json_extract(payload,'$.job_id')=32153 AND json_extract(payload,'$.generation')=17 ORDER BY created_at,rowid")]
    for row in rows:
        payload=json.loads(row['payload'])
        if row['route'] not in ('jobs/checkpoint','jobs/fail') or payload.get('unknown_outcome') or row['delivery_lease_until']>time.time():
            raise RuntimeError('callback_scope_or_delivery_owner_changed')
    result={'count':len(rows),'before':[{'id':r['id'],'route':r['route'],'payload_sha256':hashlib.sha256(r['payload'].encode()).hexdigest(),'attempts':r['attempts']} for r in rows]}
    if not a.apply:c.rollback();print(json.dumps(result));return
    evidence=Path('/data/production-audit-20261123/image-receipts-before.json')
    if rows:
        with evidence.open('x') as f:os.chmod(str(evidence),0o600);json.dump(rows,f);f.flush();os.fsync(f.fileno())
        for row in rows:c.execute('UPDATE receipt_outbox SET next_attempt_at=0 WHERE id=? AND acked_at IS NULL',(row['id'],))
    c.commit()
    box=ReceiptOutbox(path);client=CoordinatorClient(config['coordinator_url'],config['node_token'])
    for row in rows:box.flush(client,only=row['id'])
    result['after']=[dict(c.execute('SELECT id,acked_at,attempts,last_error_code FROM receipt_outbox WHERE id=?',(r['id'],)).fetchone()) for r in rows]
    Path('/data/production-audit-20261123/image-receipts-result.json').write_text(json.dumps(result,indent=2))
    print(json.dumps(result))


if __name__=='__main__':main()
