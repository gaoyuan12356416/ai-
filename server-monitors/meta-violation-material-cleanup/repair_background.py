#!/usr/bin/env python3
"""Durable GET-only reconciliation, followed by one normal scheduled cleanup.

The live cleanup is never stopped. Only the normal service performs DELETE.
An uncertain service-start or message-send is never blindly replayed.
"""
from collections import defaultdict
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE));sys.path.insert(0,str(HERE.parents[1]))
import requests
from run import load_config
from features.fb_ad_asset_delete.store import Store
from meta_violation_cleanup.runner import ACTOR
from meta_violation_cleanup.graph import GraphClient
from meta_violation_cleanup.live_reconciliation import read_status_batch,sync_observation
from meta_violation_cleanup.notifications import Notifier
from meta_violation_cleanup.runtime import Runtime,now,verify_mount,process_lock
from meta_violation_cleanup.sql_source import SqlSource
from meta_violation_cleanup.throttle import Governor,GovernedSession

BASE=Path('/mnt/data-disk/meta-violation-material-cleanup')
ROOT=Path('/mnt/data-disk/codex/tasks/meta-cleanup-indexed-20261024/repair1487235')
SOURCE='scale32-20261024-v2'
LEGACY='policy-cleanup-verified-20261024-v1'
SERVICE='meta-violation-material-cleanup.service'
STOP=False


def save(path,value):
    temporary=path.with_suffix('.tmp')
    with temporary.open('w',encoding='utf-8') as fp:
        json.dump(value,fp,ensure_ascii=False,indent=2);fp.flush();os.fsync(fp.fileno())
    os.replace(temporary,path)


def unit():
    value=subprocess.check_output(['systemctl','show',SERVICE,'-p','ActiveState','-p','MainPID','-p','Result'],
                                  text=True,timeout=15)
    return dict(line.split('=',1) for line in value.splitlines() if '=' in line)


def read_runs():
    with sqlite3.connect('file:'+str(BASE/'runs.sqlite3')+'?mode=ro',uri=True,timeout=30) as db:
        db.row_factory=sqlite3.Row
        rows=db.execute("SELECT * FROM runs WHERE mode='run' OR id=? ORDER BY started DESC LIMIT 10",(SOURCE,)).fetchall()
    return [dict(row,data=json.loads(row['data'])) for row in rows]


def may_start(run,service):
    return run['status'] in ('completed','partial') and service['ActiveState']=='inactive' and service['MainPID']=='0'


def ledger_age(value):
    # Store uses RFC3339 Z; Python 3.9 fromisoformat requires +00:00.
    stamp=datetime.fromisoformat(value.replace('Z','+00:00'))
    if stamp.tzinfo is None:raise ValueError('timezone_required')
    return time.time()-stamp.timestamp()


class ReadOnlySession(GovernedSession):
    def request(self,method,*args,**kwargs):
        if method!='GET':raise RuntimeError('read_only_reconciliation_transport')
        return super().request(method,*args,**kwargs)


class Repair:
    def __init__(self):
        self.config=load_config(BASE/'config.json');self.store=Store(self.config['shared_ledger'])
        self.path=ROOT/'state.json'
        self.state=json.loads(self.path.read_text()) if self.path.exists() else {
            'started':now(),'stage':'reconciling_live','source':SOURCE,'source_pid':'3031550',
            'jobs':['policy-cleanup-'+SOURCE,LEGACY],'notices':{}}
        self.db=sqlite3.connect(str(ROOT/'targets.sqlite3'));self.db.row_factory=sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL');self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS targets(job TEXT,key TEXT,kind TEXT,cohort TEXT,checks INTEGER DEFAULT 0,'
                        'last_check REAL DEFAULT 0,outcome TEXT,PRIMARY KEY(job,key))');self.db.commit()
        self.governor=Governor(maximum=1,per_account=1,rps=2)
        self.http=ReadOnlySession(requests.Session(),self.governor,lambda:STOP)
        # SQL FIFO session is closed before any Graph request or service start.
        tokens=SqlSource(self.config).credentials()
        self.graph=GraphClient(tokens,version=self.config.get('graph_version','v25.0'),session=self.http,get_retries=0)
        self.last_discover=0

    def checkpoint(self,**values):
        self.state.update(updated=now(),metrics=self.governor.snapshot(),**values)
        save(self.path,self.state)

    def notify(self,key,text):
        runtime=Runtime(ROOT/'notifications');rid='repair-'+hashlib.sha256(key.encode()).hexdigest()[:32]
        try:
            runtime.begin(rid,'preview',{'key':key});notice=runtime.notice(rid,'end')
            body=notice['body'] if notice else '【CPU Meta 清理补删与回查】\n'+text+'\n时间：'+now()
            receipt=Notifier(runtime,self.config['feishu_config']).send(rid,'end',body)
            self.state['notices'][key]={'status':'sent','message_id':receipt.get('message_id')}
        except Exception as exc:
            self.state['notices'][key]={'status':'not_confirmed','error_type':type(exc).__name__}
        finally:runtime.close();self.checkpoint()

    def discover(self):
        first=not self.state.get('snapshot_created')
        with self.store._transaction(False) as db:
            if first:
                rows=db.execute("SELECT a.object_key,a.kind FROM fb_asset_delete_v2_objects a "
                    "WHERE a.job_id=? AND a.kind='ad' AND a.status='pending' AND NOT EXISTS "
                    "(SELECT 1 FROM fb_asset_delete_v2_objects b WHERE b.job_id=? AND b.object_key=a.object_key)",
                    (LEGACY,'policy-cleanup-'+SOURCE)).fetchall()
                if len(rows)!=9514:raise RuntimeError('legacy_snapshot_count_changed')
                for row in rows:
                    self.db.execute('INSERT OR IGNORE INTO targets(job,key,kind,cohort) VALUES(?,?,?,?)',
                                    (LEGACY,row['object_key'],row['kind'],'legacy_9514'))
            for job in self.state['jobs']:
                owned=db.execute('SELECT actor FROM fb_asset_delete_v2_jobs WHERE job_id=?',(job,)).fetchone()
                if not owned:continue
                if owned['actor']!=ACTOR:raise RuntimeError('job_owner_mismatch')
                for row in db.execute("SELECT object_key,kind FROM fb_asset_delete_v2_objects WHERE job_id=? "
                                      "AND status='unknown' AND kind IN ('ad','creative')",(job,)):
                    self.db.execute('INSERT OR IGNORE INTO targets(job,key,kind,cohort) VALUES(?,?,?,?)',
                        (job,row['object_key'],row['kind'],'initial_unknown' if first else 'later_unknown'))
        self.db.commit();self.last_discover=time.monotonic()
        self.checkpoint(snapshot_created=True,summary=self.summary())

    def summary(self):
        return [dict(row) for row in self.db.execute('SELECT cohort,kind,coalesce(outcome,\'unchecked\') AS outcome,'
            'count(*) AS count FROM targets GROUP BY cohort,kind,outcome ORDER BY cohort,kind,outcome')]

    def text_summary(self):
        labels={'legacy_9514':'旧清单 9514','initial_unknown':'初始待核实','later_unknown':'后续待核实',
                'deleted_verified':'已确认删除并同步','already_resolved':'已有删除凭据','present':'当前仍存在',
                'unknown':'权限或状态仍待核实','unchecked':'尚未回查','credential_unavailable':'凭据待核实'}
        return '\n'.join('%s %s：%s %s'%(labels.get(r['cohort'],r['cohort']),r['kind'],
            labels.get(r['outcome'],r['outcome']),r['count']) for r in self.summary())

    def mark(self,row,outcome):
        self.db.execute('UPDATE targets SET checks=checks+1,last_check=?,outcome=? WHERE job=? AND key=?',
                        (time.time(),outcome,row['job'],row['key']))

    def pressure(self):
        runs=read_runs();active=next((r for r in runs if r['status']=='running'),None)
        if not active:return False
        metrics=active['data'].get('execution_metrics') or {}
        return (metrics.get('recent_usage_max_percent') or 0)>=60 or metrics.get('global_cooldown_seconds',0)>0

    def batch(self,final=False):
        if self.pressure():
            self.checkpoint(paused_for_main_usage=True);time.sleep(30);return True
        cutoff=time.time()-(0 if final else 1800)
        targets=self.db.execute("SELECT * FROM targets WHERE checks=0 OR (checks<3 AND last_check<? AND "
            "outcome NOT IN ('deleted_verified','already_resolved')) ORDER BY checks,cohort,key LIMIT 500",(cutoff,)).fetchall()
        if not targets:return False
        groups=defaultdict(list);processed=0
        with self.store._transaction(False) as db:
            for target in targets:
                obj=db.execute('SELECT * FROM fb_asset_delete_v2_objects WHERE job_id=? AND object_key=?',
                               (target['job'],target['key'])).fetchone()
                if not obj:raise RuntimeError('target_disappeared')
                if obj['status'] in ('deleted','already_deleted'):
                    self.mark(target,'already_resolved');processed+=1;continue
                if obj['status']=='in_progress':continue
                # Give the main DELETE readback time to settle; never race its claim.
                try:age=ledger_age(obj['updated_at'])
                except ValueError:raise RuntimeError('invalid_ledger_timestamp')
                if age<120 and not final:continue
                payload=json.loads(obj['payload']);result=json.loads(obj['result'])
                users=payload.get('user_ids',[]);accounts=payload.get('account_ids',[])
                user=result.get('credential_user_id') or (users[0] if len(users)==1 else None)
                if len(accounts)!=1 or str(user) not in users or not self.graph.tokens.get(str(user)):
                    self.mark(target,'credential_unavailable');processed+=1;continue
                groups[(obj['kind'],str(user),str(accounts[0]))].append((target,dict(obj)))
        self.db.commit()
        for (kind,user,account),items in groups.items():
            for offset in range(0,len(items),50):
                if STOP:raise InterruptedError('stop_requested')
                if self.pressure():self.checkpoint(paused_for_main_usage=True);return True
                part=items[offset:offset+50];self.http.account=account
                outcomes=read_status_batch(self.graph,kind,[o['object_id'] for _,o in part],user,account,lambda:STOP)
                for target,obj in part:
                    result=sync_observation(self.store,target['job'],target['key'],outcomes[obj['object_id']],user,ACTOR,
                        allow_pending=target['cohort']=='legacy_9514',expected_updated_at=obj['updated_at'])
                    self.mark(target,result['status']);processed+=1
                self.db.commit();self.checkpoint(summary=self.summary(),paused_for_main_usage=False)
        if not processed:time.sleep(30)
        return True

    def finish_pass(self,final=False):
        while not STOP and self.batch(final):pass
        if STOP:raise InterruptedError('stop_requested')

    def execute(self):
        self.checkpoint(stage='following_new_run' if self.state.get('followup') else 'reconciling_live')
        self.discover()
        if 'start' not in self.state['notices']:
            self.notify('start','1487235 已纳入共享引用补删。当前主任务继续运行，回查任务仅 GET；'
                '旧清单 9514 条 Ad 及所有待核实 Ad/Creative 全量核验后同步台账，已删对象不再 DELETE。'
                '\n当前轮结束后自动接续新版，先完成共享 Ad，再重试 Creative。')
        while not STOP:
            if time.monotonic()-self.last_discover>=300:self.discover()
            if self.batch():continue
            if 'initial_pass' not in self.state['notices']:
                self.notify('initial_pass','首轮全量只读回查已完成，结果如下：\n'+self.text_summary()+
                    '\n当前主进程未中断；新版共享引用补删将在本轮完成后接续。')
            runs=read_runs();source=next((r for r in runs if r['id']==SOURCE),None)
            if not source:raise RuntimeError('source_run_missing')
            if source['status']=='running':
                actual=unit()
                if actual['MainPID']!=self.state['source_pid']:raise RuntimeError('source_owner_changed')
                time.sleep(30);continue
            if not self.state.get('followup'):
                actual=unit()
                # A timer may have started the same next normal run before us.
                newer=next((r for r in runs if r['started']>source['started'] and r['mode']=='run'),None)
                if newer:
                    self.state['followup']=newer['id'];self.state['jobs'].append('policy-cleanup-'+newer['id'])
                    self.checkpoint(stage='following_new_run');continue
                if not may_start(source,actual):
                    if actual['ActiveState']=='active':time.sleep(30);continue
                    raise RuntimeError('source_not_safely_completed')
                if self.state.get('launch_requested'):raise RuntimeError('service_launch_requires_reconciliation')
                self.discover();self.finish_pass(final=True)
                self.checkpoint(stage='starting_followup',launch_requested=now())
                subprocess.run(['systemctl','start','--no-block',SERVICE],check=True,timeout=20)
                for _ in range(120):
                    newer=next((r for r in read_runs() if r['started']>source['started'] and r['mode']=='run'),None)
                    if newer:break
                    time.sleep(5)
                if not newer:raise RuntimeError('followup_start_not_confirmed')
                self.state['followup']=newer['id'];self.state['jobs'].append('policy-cleanup-'+newer['id'])
                self.checkpoint(stage='following_new_run')
                self.notify('followup_started','原 30 天任务已结束，新版已接续：'+newer['id']+
                    '\n新筛选窗口为近 3 天，并保留原 30 天未完成队列。1487235/3858800 均按共享 Ad → Creative 顺序处理。'
                    '\n'+self.text_summary());continue
            follow=next((r for r in runs if r['id']==self.state['followup']),None)
            if not follow:raise RuntimeError('followup_missing')
            if follow['status']=='running':
                if unit()['MainPID']!=str(follow['data'].get('_owner',{}).get('pid')):
                    raise RuntimeError('followup_owner_missing')
                time.sleep(30);continue
            if unit()['ActiveState']=='active':time.sleep(30);continue
            if follow['status'] not in ('completed','partial'):raise RuntimeError('followup_failed')
            self.discover();self.finish_pass(final=True)
            with self.store._transaction(False) as db:
                counts=[dict(row) for row in db.execute('SELECT kind,status,count(*) AS count FROM fb_asset_delete_v2_objects '
                    'WHERE job_id=? GROUP BY kind,status',('policy-cleanup-'+follow['id'],))]
            self.checkpoint(stage='completed',followup_status=follow['status'],followup_counts=counts,ended=now())
            self.notify('completed','共享引用补删和全量回查已完成。\n后续运行：'+follow['id']+'（'+follow['status']+'）\n'+
                self.text_summary()+'\n补删运行台账：'+json.dumps(counts,ensure_ascii=False)+
                '\n仍违规、无权限或账号不可写的对象保留状态；待核实对象未重放 DELETE。明细：'+str(ROOT/'targets.sqlite3'))
            return
        raise InterruptedError('stop_requested')


def main():
    global STOP
    os.umask(0o077);verify_mount();ROOT.mkdir(parents=True,exist_ok=True)
    def stopped(*_):
        global STOP
        STOP=True
    signal.signal(signal.SIGTERM,stopped);signal.signal(signal.SIGINT,stopped)
    with process_lock(ROOT/'worker.lock'):
        repair=Repair()
        try:repair.execute()
        except BaseException as exc:
            repair.checkpoint(stage='failed',error_type=type(exc).__name__)
            repair.notify('failed','回查/接续任务异常停止，主删除任务未被本脚本停止或重启。\n异常类型：'+
                type(exc).__name__+'\n状态文件：'+str(repair.path))
            raise


if __name__=='__main__':main()
