"""Scope the last legacy entrypoint updates to FB and the X pool owner."""
import contextlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts import deploy_publish_queue_isolation as deploy

# The pool is owned by the OAuth sidecar. The automatic-template sidecar does
# not import these two modules and keeps its existing process and active work.
deploy.LANES['x']=(deploy.LANES['x'][0],[('x-post-automation.service',8810,1)])

def prepare(source,commit,directory):
    source,directory=Path(source).resolve(),Path(directory).resolve()
    assert deploy.run(['git','-C','/mnt/data-disk/pipeline-dispatch/github','rev-parse',commit+'^{commit}'])==commit
    expected=json.loads((source/'ops/publish-queue-isolation/final-entry-files.json').read_text())
    directory.mkdir(mode=0o711,exist_ok=False)
    manifest={'commit':commit,'source':str(source),'lanes':{},'database_backups':{}}
    for lane,files in expected.items():
        live=Path(deploy.LANES[lane][0]).resolve();candidate=directory/'candidates'/lane
        candidate.parent.mkdir(exist_ok=True)
        if lane=='app':candidate.mkdir()
        else:
            shutil.copytree(live,candidate,symlinks=True,ignore=shutil.ignore_patterns('__pycache__','*.pyc','.git'))
            deploy.preserve_owners(live,candidate)
        for rel,h in files.items():
            assert deploy.sha(live/rel)==h['before'],'source_drift:'+lane+'/'+rel
            assert deploy.sha(source/rel)==h['after'];compile((source/rel).read_bytes(),rel,'exec')
            backup=directory/'before'/lane/rel;backup.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(live/rel,backup)
            target=candidate/rel;target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(source/rel,target);st=(live/rel).stat();os.chown(target,st.st_uid,st.st_gid)
        manifest['lanes'][lane]={'before_root':str(live),'candidate':str(candidate),'units':deploy.LANES[lane][1],'files':files,'state':'prepared'}
    for key in ['fb','x']:
        dst=directory/(key+'-before.sqlite3');deploy.backup_db(Path(deploy.DBS[key]),dst);manifest['database_backups'][key]=str(dst)
    deploy.save(directory/'manifest.json',manifest)
    # Rehearse the automatic pool-pause migration, with no callbacks/network.
    from features.x_video_publish.core import PersonalStore
    from features.x_video_publish.pool import PoolService
    p=directory/'x-rehearsal.sqlite3';shutil.copy2(directory/'x-before.sqlite3',p)
    with contextlib.closing(sqlite3.connect(p)) as c:
        before=c.execute('SELECT * FROM x_video_publish_pool_item ORDER BY id').fetchall()
        paused=c.execute("SELECT COUNT(*) FROM x_video_publish_pool WHERE status='paused' AND pause_reason IN ('unknown_outcome','x_post_unknown_outcome','pool_unknown_outcome')").fetchone()[0]
    PoolService(PersonalStore(p),None,None,None)
    with contextlib.closing(sqlite3.connect(p)) as c:
        assert before==c.execute('SELECT * FROM x_video_publish_pool_item ORDER BY id').fetchall()
        assert c.execute("SELECT COUNT(*) FROM x_video_publish_pool WHERE status='paused' AND pause_reason IN ('unknown_outcome','x_post_unknown_outcome','pool_unknown_outcome')").fetchone()[0]==0
    from features.fb_auto_posts.manual_batch import task_policy
    policies={}
    with contextlib.closing(sqlite3.connect(directory/'fb-before.sqlite3')) as c:
        c.row_factory=sqlite3.Row
        for tid in [29670,29813,29814]:
            row=c.execute('SELECT * FROM fb_auto_task WHERE id=?',(tid,)).fetchone()
            policy=task_policy(c,row);assert not policy or not policy['waiting_for_previous'];policies[tid]=bool(policy)
    deploy.save(directory/'rehearsal.json',{'pool_items_unchanged':True,'obsolete_pool_pauses_lifted':paused,'fb_previous_task_wait_removed':policies})
    print(json.dumps({'prepared':str(directory),'pool_pauses':paused,'fb_policies':policies}),flush=True)

if __name__=='__main__':
    if sys.argv[1]=='prepare':prepare(*sys.argv[2:])
    else:
        directory=Path(sys.argv[2]);lanes=sys.argv[3:] or ['fb','x','app']
        for lane in lanes:deploy.switch(directory,lane,sys.argv[1]=='rollback')
