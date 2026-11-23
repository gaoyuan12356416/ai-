"""Resume the audited TT preflight tickets after fixing their read-only spool."""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import sqlite3

ROOT=Path('/mnt/data-disk/production-audit-20261123')


def main():
    p=argparse.ArgumentParser();p.add_argument('--apply',action='store_true');a=p.parse_args()
    scope=json.loads((ROOT/'historical-publish-holds.json').read_text())['tasks']
    c=sqlite3.connect('/mnt/data-disk/pipeline-dispatch/dispatch.sqlite3',timeout=20,isolation_level=None);c.row_factory=sqlite3.Row
    c.execute('BEGIN IMMEDIATE');before=[]
    for item in scope:
        if not item['source_key'].startswith('tt:auto:'):continue
        j=c.execute('SELECT * FROM pipeline_job WHERE id=?',(item['prepare_id'],)).fetchone()
        g=c.execute('SELECT * FROM pipeline_media_gate WHERE source_key=?',(item['source_key'],)).fetchone()
        if j['status']!='queued' or j['hold_reason']!='media_decision_pending' or not g or g['phase']!='pending' or g['error']!='media_preflight_unavailable':continue
        pub=c.execute("SELECT status,hold_reason,external_intent FROM pipeline_job WHERE source_key=? AND stage='publish'",(item['source_key'],)).fetchone()
        if tuple(pub)!=('backlog_paused','historical_preparation_only',0) or j['external_intent'] or j['claim_hash'] or j['lease_expires_at']:
            raise RuntimeError('historical_preflight_scope_changed')
        before.append({'job_id':j['id'],'source_key':j['source_key'],'gate':dict(g)})
        # Keep the same decision, ticket and original task. An empty retry time
        # lets these now-fixed tickets join the next normal dispatcher batch.
        c.execute("UPDATE pipeline_media_gate SET retry_at='' WHERE source_key=? AND ticket=?",(j['source_key'],g['ticket']))
        c.execute('INSERT INTO pipeline_event(job_id,event_type,details_json,created_at) VALUES(?,?,?,?)',(j['id'],'media_preflight_environment_repaired',json.dumps({'decision_id':g['ticket'],'historical_publish_held':True}),dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')))
    if a.apply:
        path=ROOT/'tt-fixed-preflight-before.json'
        with path.open('x') as f:os.chmod(str(path),0o600);json.dump(before,f);f.flush();os.fsync(f.fileno())
        c.commit()
    else:c.rollback()
    print(json.dumps({'resumed':len(before),'applied':a.apply,'ids':[r['job_id'] for r in before]}))


if __name__=='__main__':main()
