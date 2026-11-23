"""Re-admit only today's original unselected tasks or audited queue receipts."""
import argparse
from contextlib import closing
from datetime import datetime,timezone,timedelta
import json
import os
from pathlib import Path
import sqlite3

ROOT=Path('/mnt/data-disk/x-today-publish-20261123')
AUTO=Path('/mnt/data-disk/x-tag-publisher/auto/x-auto-post.sqlite3')
POST=Path('/mnt/data-disk/x-tag-publisher/posts.sqlite3')

def connect(path,write=False):
    c=sqlite3.connect(path.as_uri()+('?mode=rw' if write else '?mode=ro'),uri=True,timeout=15)
    c.row_factory=sqlite3.Row;return c

def recover(action):
    now=datetime.now(timezone.utc);stamp=now.isoformat()
    assert (now+timedelta(hours=8)).date().isoformat()=='2026-11-23'
    scope=json.loads((ROOT/'today-scope.json').read_text())['tasks']
    plan=json.loads((ROOT/'today-media-plan.json').read_text())
    authorized={x['execution_queue_id'] for x in json.loads((ROOT/'failed-scope.json').read_text())}
    result={'action':action,'at':stamp,'rearmed':[],'unchanged':[]}
    with closing(connect(POST)) as p:
        p.execute('BEGIN');p.execute('SELECT 1 FROM sqlite_master LIMIT 1').fetchone()
        prior_uid,prior_gid=os.geteuid(),os.getegid();owner=AUTO.stat()
        try:
            os.setegid(owner.st_gid);os.seteuid(owner.st_uid)
            with closing(connect(AUTO,True)) as a:
                a.execute('BEGIN IMMEDIATE')
                for frozen in scope:
                    if frozen['template_id']!=9000000000007:raise RuntimeError('scope_changed')
                    if action=='selection' and frozen['status']!='no_candidate':continue
                    if action=='receipts' and frozen['execution_queue_id'] not in authorized:continue
                    row=dict(a.execute('SELECT * FROM x_auto_task WHERE id=?',(frozen['id'],)).fetchone())
                    assert all(row[k]==frozen[k] for k in ('run_id','template_id','template_version','account_id','material_id','execution_queue_id'))
                    if row['claim_token'] or row['status']=='published':
                        result['unchanged'].append({'task':row['id'],'reason':'active_or_published'});continue
                    if a.execute('SELECT 1 FROM x_auto_event WHERE task_id=? AND event_type=?',
                                 (row['id'],'today_'+action+'_recovered')).fetchone():continue
                    run=a.execute('SELECT status FROM x_auto_run WHERE id=?',(row['run_id'],)).fetchone()
                    assert run and run[0]!='canceled'
                    if action=='selection':
                        assert row['status']=='no_candidate'
                        assert not any(row[k] for k in ('material_id','execution_run_id','execution_queue_id','execution_log_id',
                            'publish_id','unknown_outcome','preparation_attempt_count','publish_attempt_count','reserved_at_utc','bridged_at_utc'))
                        assert not a.execute('SELECT 1 FROM x_auto_material_ledger WHERE task_id=?',(row['id'],)).fetchone()
                        assert not p.execute('SELECT 1 FROM x_post_manual_run WHERE external_task_key=?',('x-auto-task-'+str(row['id']),)).fetchone()
                        phase='selection'
                    else:
                        assert row['status'] in ('failed','retry_wait','reconciling')
                        auth=p.execute('SELECT * FROM pipeline_x_prepost_recovery WHERE source_key=? ORDER BY from_generation DESC LIMIT 1',
                            ('x:queue:'+str(row['execution_queue_id']),)).fetchone()
                        assert auth and auth['state'] in ('armed','consumed')
                        log=p.execute('SELECT * FROM x_post_publish_log WHERE queue_id=?',(row['execution_queue_id'],)).fetchone()
                        assert log
                        if log['unknown_outcome']:
                            result['unchanged'].append({'task':row['id'],'reason':'new_result_requires_reconciliation'});continue
                        phase='reconcile'
                    a.execute("UPDATE x_auto_task SET status='retry_wait',claim_phase=?,next_attempt_at_utc=?,"
                        "claim_worker='',claim_token='',lease_expires_at_utc='',unknown_outcome=0,"
                        "error_code='',error_message='',finished_at_utc='',updated_at=? WHERE id=?",
                        (phase,stamp,stamp,row['id']))
                    a.execute("INSERT INTO x_auto_event(run_id,task_id,event_type,from_status,to_status,message,details_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
                        (row['run_id'],row['id'],'today_'+action+'_recovered',row['status'],'retry_wait',
                         'Today original task resumed after scoped repair',json.dumps({'before':row,'plan_sha256':auth['plan_sha256'] if action=='receipts' else None}),stamp))
                    a.execute("UPDATE x_auto_run SET status='running',finished_at_utc='',updated_at=? WHERE id=?",(stamp,row['run_id']))
                    result['rearmed'].append(row['id'])
                a.commit()
        finally:
            os.seteuid(prior_uid);os.setegid(prior_gid)
    (ROOT/('auto-'+action+'-'+now.strftime('%H%M%S')+'.json')).write_text(json.dumps(result))
    print(json.dumps({'action':action,'rearmed':len(result['rearmed']),'unchanged':result['unchanged']}))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['selection','receipts']);recover(p.parse_args().action)
