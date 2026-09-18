"""Preview, rehearse and fingerprint exact future Page additions. No Graph calls."""
import argparse
from dataclasses import fields
from datetime import datetime,timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import random
import sqlite3
import sys
import threading

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from features.fb_auto_posts.core import FBAutoPostStore
from features.fb_auto_posts.pool_extension import extend_future_runs,scope_fingerprint
from features.fb_auto_posts.repositories import MaterialCandidate,CandidateSnapshot,PagePoolRepository,ReadOnlyMySQL,MaterialRepository


def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),default=str).encode()).hexdigest()


def store_at(path):
    store=FBAutoPostStore.__new__(FBAutoPostStore)
    store.path=str(path);store._lock=threading.RLock();store.now_fn=lambda:datetime.now(timezone.utc)
    return store


class CachedMaterials:
    def __init__(self,path,seed):
        self.snapshots={};self.rng=random.Random(seed)
        for language,row in json.loads(Path(path).read_text()).items():
            values=[]
            for raw in row['candidates']:
                for name in ('duration_seconds','material_spend','material_roas','drama_spend','drama_roas'):
                    if raw[name] is not None:raw[name]=Decimal(str(raw[name]))
                values.append(MaterialCandidate(**raw))
            self.snapshots[language]=CandidateSnapshot(tuple(values),tuple(row['metric_generation_ids']),tuple(row['metric_dates']))
    def candidate_snapshot(self,config):return self.snapshots[config['language']]
    def choose_from(self,candidates,excluded):return MaterialRepository.choose_from(self,candidates,excluded)


def rows(conn,sql,args=()):return [dict(r) for r in conn.execute(sql,args)]


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--root',required=True);parser.add_argument('--run-ids',required=True)
    parser.add_argument('--operation-id',required=True);parser.add_argument('--publish-floor',default='')
    parser.add_argument('--apply',default='');args=parser.parse_args()
    root=Path(args.root).resolve();assert str(root).startswith('/mnt/data-disk/')
    run_ids=[int(v) for v in args.run_ids.split(',')]
    db=Path(os.environ['FB_AUTO_POST_DB_PATH'])
    import pymysql
    def connect():
        assert int(os.environ['FB_AUTO_MYSQL_PORT'])==63350
        c=pymysql.connect(host=os.environ['FB_AUTO_MYSQL_HOST'],port=63350,user=os.environ['FB_AUTO_MYSQL_USER'],password=os.environ['FB_AUTO_MYSQL_PASSWORD'],database=os.environ['FB_AUTO_MYSQL_DATABASE'],charset='utf8mb4',autocommit=True,connect_timeout=5,read_timeout=60)
        with c.cursor() as cur:cur.execute('SELECT @@read_only');assert cur.fetchone()[0]==1
        return c
    pages_repo=PagePoolRepository(ReadOnlyMySQL(connect))
    c=sqlite3.connect('file:'+str(db)+'?mode=ro',uri=True);c.row_factory=sqlite3.Row
    template=c.execute('SELECT * FROM fb_auto_template WHERE id=(SELECT template_id FROM fb_auto_run WHERE id=?)',(run_ids[0],)).fetchone()
    config=json.loads(c.execute('SELECT config_json FROM fb_auto_template_version WHERE template_id=? AND version=?',(template['id'],template['current_version'])).fetchone()[0])
    page_rows=pages_repo.list_pages(config['group_ids'],is_admin=bool(template['scope_is_admin']),owner_user_id=template['owner_user_id'])
    expected_ids={p['page_id'] for p in json.loads((root/'preflight.json').read_text())['pages']}
    assert {p.page_id for p in page_rows}==expected_ids and len(expected_ids)==145
    assert not pages_repo.legacy_conflicts(config['group_ids'])
    assert c.execute("SELECT count(*) FROM fb_auto_template WHERE status='enabled' AND id<>?",(template['id'],)).fetchone()[0]==0
    before=rows(c,'SELECT * FROM fb_auto_task ORDER BY id');max_id=max(r['id'] for r in before)
    before_facts={t:rows(c,'SELECT * FROM '+t+' ORDER BY 1') for t in ('fb_auto_publish_attempt','fb_auto_publish_ledger')}
    clone=root/('extension-rehearsal-'+datetime.now().strftime('%H%M%S%f')+'.sqlite3')
    with sqlite3.connect(str(clone)) as dest:c.backup(dest)
    c.close()
    cache=root/'language-candidates.json'
    assert datetime.now().timestamp()-cache.stat().st_mtime<3600
    metadata=json.loads((root/'language-candidates-metadata.json').read_text())
    assert metadata['template_id']==template['id'] and metadata['template_version']==template['current_version'] and metadata['config_sha256']==digest(config)
    kw=dict(operation_id=args.operation_id,expected_scope=scope_fingerprint(page_rows),expected_version=template['current_version'],max_pages=int(os.environ['FB_AUTO_MAX_JOBS_PER_SLOT']),max_daily_jobs=int(os.environ['FB_AUTO_MAX_DAILY_JOBS']),publish_floor=args.publish_floor)
    preview=extend_future_runs(store_at(clone),run_ids,page_rows,CachedMaterials(cache,args.operation_id),**kw)
    cx=sqlite3.connect(str(clone));cx.row_factory=sqlite3.Row
    assert cx.execute('PRAGMA quick_check').fetchone()[0]=='ok'
    assert rows(cx,'SELECT * FROM fb_auto_task WHERE id<=? ORDER BY id',(max_id,))==before
    bindings=rows(cx,'SELECT id,run_id,page_id,material_id,content_id,source_media_url,planned_publish_at_utc FROM fb_auto_task WHERE id>? ORDER BY id',(max_id,));cx.close()
    fingerprint=digest({'scope':kw['expected_scope'],'config':digest(config),'old_tasks':digest(before),'bindings':bindings,'operation':args.operation_id})
    output={'fingerprint':fingerprint,'preview':preview,'added_tasks':len(bindings),'historical_tasks_preserved':len(before),'applied':False}
    (root/'extension-preview.json').write_text(json.dumps(output,indent=2))
    if args.apply:
        assert args.apply==fingerprint,'Reviewed fingerprint changed; apply refused'
        current=pages_repo.list_pages(config['group_ids'],is_admin=bool(template['scope_is_admin']),owner_user_id=template['owner_user_id'])
        assert scope_fingerprint(current)==kw['expected_scope'] and not pages_repo.legacy_conflicts(config['group_ids'])
        with sqlite3.connect(str(db)) as live,sqlite3.connect(str(root/'before-extension-apply.sqlite3')) as backup:live.backup(backup)
        output['applied_result']=extend_future_runs(store_at(db),run_ids,current,CachedMaterials(cache,args.operation_id),**kw)
        with sqlite3.connect(str(db)) as live:
            live.row_factory=sqlite3.Row
            assert rows(live,'SELECT * FROM fb_auto_task WHERE id<=? ORDER BY id',(max_id,))==before
            assert rows(live,'SELECT id,run_id,page_id,material_id,content_id,source_media_url,planned_publish_at_utc FROM fb_auto_task WHERE id>? ORDER BY id',(max_id,))==bindings
            assert {t:rows(live,'SELECT * FROM '+t+' ORDER BY 1') for t in before_facts}==before_facts
            assert live.execute('PRAGMA quick_check').fetchone()[0]=='ok'
        output['applied']=True
        (root/'extension-applied.json').write_text(json.dumps(output,indent=2))
    print(json.dumps(output))


if __name__=='__main__':main()
