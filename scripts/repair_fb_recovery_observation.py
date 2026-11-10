"""Repair only a false historical-attempt observation after the approved recovery.

No native task is reset. Every original attempt/permit must still be a saved
rejection, and no operation may have occurred since that recovery authorization.
"""
import argparse
import contextlib
import datetime as dt
import json
from pathlib import Path
import sqlite3
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.recover_fb_unpublished import rows, require, sha, backup, dumps, EVENT, PREWRITE, REJECTED


def plan(c, ids):
    items = []
    for identity in ids:
        j = rows(c, 'SELECT * FROM pipeline_job WHERE id=?', (identity,))[0]
        e = rows(c, 'SELECT details_json FROM pipeline_event WHERE job_id=? AND event_type=?', (identity, EVENT))
        require(len(e) == 1, 'recovery_authorization_missing')
        p = json.loads(e[0]['details_json'])
        require(j['status'] == 'unknown' and j['error_code'] == 'canonical_unknown_outcome'
                and j['channel'] == 'fb' and j['stage'] == 'publish' and not j['external_intent']
                and j['source_key'] == p['source_key'], 'not_false_observation')
        require(not j['lease_expires_at'] or j['lease_expires_at'] < dt.datetime.now(dt.timezone.utc).isoformat(), 'central_lease_active')
        require(not rows(c, 'SELECT 1 FROM pipeline_operation WHERE job_id=? AND generation>?', (identity, p['generation'])), 'new_central_operation')
        require(not rows(c, 'SELECT 1 FROM pipeline_publication WHERE source_key=?', (j['source_key'],)), 'publication_present')
        auto = j['family'] == 'auto'
        table, key, attempts = ('fb_auto_task','id','fb_auto_publish_attempt') if auto else ('fb_video_task','task_id','fb_video_attempt')
        t = rows(c, 'SELECT * FROM native.'+table+' WHERE '+key+'=?', (j['source_id'],))[0]
        require(t['status'] == ('ready' if auto else 'queued') and not t['graph_post_id'] and not t['unknown_outcome'], 'native_not_ready')
        require(not any(t.get(k) for k in ('lease_owner','lease_expires_at_utc','execution_lease','execution_until')), 'native_lease_active')
        a = rows(c, 'SELECT * FROM native.'+attempts+' WHERE task_id=?', (j['source_id'],))
        require(len(a) == p['attempts'] and all(x['result_kind'] in {'definite_failure','credential_failure'}
                and x['error_code'] in PREWRITE | REJECTED and not x.get('graph_post_id') for x in a), 'attempts_changed')
        o = rows(c, 'SELECT * FROM native.pipeline_fb_operation WHERE source_key=?', (j['source_key'],))
        require(len(o) == p['explicit_rejections'], 'operations_changed')
        for x in o:
            result = json.loads(x['result_json'] or '{}')
            require(x['generation'] <= p['generation'] and result.get('kind') in {'definite_failure','credential_failure'}
                    and not result.get('post_id') and result.get('error_code') in REJECTED, 'operation_unresolved')
        require(not rows(c, 'SELECT 1 FROM native.pipeline_canonical_hold WHERE source_key=?', (j['source_key'],)), 'native_hold')
        items.append(dict(job_id=identity, source_key=j['source_key'], state_sha256=sha([j,t,a,o,p])))
    return dict(items=items, plan_sha256=sha(items))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('central','native','job-ids','output'): p.add_argument('--'+name, required=True)
    p.add_argument('--apply', action='store_true');p.add_argument('--plan-sha256');p.add_argument('--backup-dir')
    a=p.parse_args(); ids=json.loads(Path(a.job_ids).read_text())
    require(ids and len(ids)==len(set(ids)) and all(type(x) is int for x in ids), 'exact_ids_required')
    if a.apply:
        folder=Path(a.backup_dir);folder.mkdir(mode=0o700, exist_ok=False)
        backup(a.central,folder/'dispatch.sqlite3');backup(a.native,folder/'fb.sqlite3')
    mode='rw' if a.apply else 'ro'
    with contextlib.closing(sqlite3.connect(Path(a.central).as_uri()+'?mode='+mode,uri=True,timeout=30)) as c:
        c.row_factory=sqlite3.Row
        c.execute('ATTACH DATABASE ? AS native',(Path(a.native).as_uri()+'?mode='+mode,))
        c.execute('BEGIN IMMEDIATE' if a.apply else 'BEGIN'); result=plan(c,ids)
        if a.apply:
            require(result['plan_sha256']==a.plan_sha256, 'plan_changed')
            require('fb' not in json.loads(c.execute('SELECT config_json FROM pipeline_config').fetchone()[0])['enabled_channels'], 'pause_fb_admission')
            now=dt.datetime.now(dt.timezone.utc).isoformat(timespec='milliseconds')
            for item in result['items']:
                identity=item['job_id']
                c.execute("UPDATE pipeline_job SET status='queued',error_code='',error_message='',finished_at='',node_id='',worker_id='',claim_hash='',lease_expires_at='',updated_at=? WHERE id=?",(now,identity))
                for table in ('pipeline_resource','pipeline_legacy_resource'):
                    c.execute('DELETE FROM '+table+' WHERE job_id=?',(identity,))
                c.execute('INSERT INTO pipeline_event(job_id,event_type,details_json,created_at) VALUES(?,?,?,?)',
                          (identity,'fb_recovery_observation_repaired',dumps(item),now))
            c.commit()
        else: c.rollback()
    result['applied']=a.apply;Path(a.output).write_text(json.dumps(result,indent=2));Path(a.output).chmod(0o600)
    print(json.dumps(result))

if __name__=='__main__': main()
