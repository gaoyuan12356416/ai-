"""Read today's task/receipt consistency and duplicate evidence without publishing."""
from collections import Counter,defaultdict
from contextlib import closing
from datetime import datetime,timezone
import json
from pathlib import Path
import sqlite3

ROOT=Path('/mnt/data-disk/x-today-publish-20261123')
def ro(path):
    c=sqlite3.connect('file:'+path+'?mode=ro',uri=True,timeout=10);c.row_factory=sqlite3.Row;return c

def audit():
    with closing(ro('/mnt/data-disk/x-tag-publisher/auto/x-auto-post.sqlite3')) as a:
        tasks=[dict(x) for x in a.execute("SELECT id,run_id,status,account_id,material_id,execution_run_id,execution_queue_id,publish_id,publish_url,unknown_outcome,error_code,error_message,claim_phase,next_attempt_at_utc FROM x_auto_task WHERE template_id=9000000000007 AND scheduled_at_utc>='2026-11-22T16:00:00' AND scheduled_at_utc<'2026-11-23T16:00:00' ORDER BY id")]
        template=dict(a.execute('SELECT t.id,t.current_version,t.enabled,v.config_json FROM x_auto_template t JOIN x_auto_template_version v ON v.template_id=t.id AND v.version=t.current_version WHERE t.id=9000000000007').fetchone())
    ids=[t['id'] for t in tasks];keys=['x-auto-task-'+str(i) for i in ids];marks=','.join('?' for _ in ids)
    with closing(ro('/mnt/data-disk/x-tag-publisher/posts.sqlite3')) as p:
        runs=[dict(r) for r in p.execute('SELECT id,external_task_key FROM x_post_manual_run WHERE external_task_key IN ('+marks+')',keys)]
        queues=[]
        for run in runs:
            for row in p.execute('SELECT q.id,q.manual_run_id,q.account_id,q.material_id,q.status,l.status log_status,l.unknown_outcome,l.x_post_id,l.x_post_url,l.error_code,l.attempt_count FROM x_post_queue q LEFT JOIN x_post_publish_log l ON l.queue_id=q.id WHERE q.manual_run_id=?',(run['id'],)):
                queues.append(dict(row,task_id=int(run['external_task_key'].removeprefix('x-auto-task-'))))
        post_intents=[]
        for q in queues:
            count=p.execute("SELECT count(*) FROM pipeline_x_operation WHERE source_key=? AND operation='x:POST:/2/tweets'",('x:queue:'+str(q['id']),)).fetchone()[0]
            if count>1:post_intents.append({'queue_id':q['id'],'count':count})
    errors=[];byid={t['id']:t for t in tasks}
    for row in queues:
        task=byid[row['task_id']]
        if str(row['account_id'])!=str(task['account_id']) or str(row['material_id'])!=str(task['material_id']):errors.append({'task':task['id'],'error':'identity_mismatch'})
    for label,values in [('task_queue',[q['task_id'] for q in queues]),('execution_key',[r['external_task_key'] for r in runs]),('post_id',[q['x_post_id'] for q in queues if q['x_post_id']])]:
        errors += [{'error':'duplicate_'+label,'id':key,'count':n} for key,n in Counter(values).items() if n>1]
    published=[q for q in queues if q['log_status']=='published' and q['x_post_id'] and not q['unknown_outcome']]
    pairs={(str(q['account_id']),str(q['material_id'])) for q in published}
    published_by_pair=defaultdict(set)
    for lane,path in [('tag','/mnt/data-disk/x-tag-publisher/posts.sqlite3'),('ordinary','/var/lib/x-post-automation/accounts.sqlite3')]:
        with closing(ro(path)) as p:
            for q in p.execute("SELECT q.account_id,q.material_id,l.x_post_id FROM x_post_queue q JOIN x_post_publish_log l ON l.queue_id=q.id WHERE l.status='published' AND l.x_post_id<>''"):
                pair=(str(q['account_id']),str(q['material_id']))
                if pair in pairs:published_by_pair[pair].add(q['x_post_id'])
    duplicates=[{'account_id':pair[0],'material_id':pair[1],'post_ids':sorted(posts)} for pair,posts in published_by_pair.items() if len(posts)>1]
    counts={str(run):dict(Counter(t['status'] for t in tasks if t['run_id']==run)) for run in sorted({t['run_id'] for t in tasks})}
    return {'at':datetime.now(timezone.utc).isoformat(),'runs':counts,'total':len(tasks),'task_counts':dict(Counter(t['status'] for t in tasks)),
            'canonical_counts':dict(Counter(q['log_status'] or q['status'] for q in queues)), 'confirmed_posts':len(published),
            'unknown_queues':[q['id'] for q in queues if q['unknown_outcome']], 'duplicate_or_identity_errors':errors,
            'multiple_post_intents':post_intents,'account_material_duplicate_posts':duplicates,'template':template,'tasks':tasks,'queues':queues}

if __name__=='__main__':
    result=audit();(ROOT/'today-latest.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps({k:v for k,v in result.items() if k not in ('tasks','queues','template')},ensure_ascii=False))
