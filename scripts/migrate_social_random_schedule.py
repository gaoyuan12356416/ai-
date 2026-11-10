"""Re-time unstarted future FB tasks without rebuilding material or identities.

Plan is read-only against production. Apply uses the reviewed manifest and a
single attached transaction, then verifies both native and dispatch records.
The manifest permits idempotent repair after an interrupted multi-DB commit.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import sqlite3

from features import social_random_schedule as daily
from features.fb_auto_posts import target_schedule
from features.fb_auto_posts.strategy import in_slot


def _exists(c, table):
    schema, _, name = table.rpartition('.')
    schema = schema or 'main'
    return c.execute(f"SELECT 1 FROM {schema}.sqlite_master WHERE type='table' AND name=?", (name or table,)).fetchone() is not None


def _rows(c, table, where, args):
    return [dict(r) for r in c.execute(f'SELECT * FROM {table} WHERE {where}', args)] if _exists(c,table) else []


NATIVE_EVIDENCE = (
    ('fb_auto_publish_attempt', 'task_id', ''),
    ('fb_auto_publish_ledger', 'task_id', ''),
    ('pipeline_fb_claim', 'source_key', " AND stage='publish'"),
    ('pipeline_fb_operation', 'source_key', " AND stage='publish'"),
    ('pipeline_canonical_hold', 'source_key', ''),
)
DISPATCH_EVIDENCE = (
    'pipeline_claim_history', 'pipeline_operation', 'pipeline_receipt',
    'pipeline_publication', 'pipeline_resource', 'pipeline_legacy_resource',
)


def _matching_values(c, table, result_column, lookup_column, values, suffix=''):
    values = sorted(set(values))
    result = set()
    if values and _exists(c, table):
        # Older deployed SQLite builds allow only 999 bound variables. One
        # batch covers this migration; larger manifests remain bounded too.
        for offset in range(0, len(values), 900):
            batch = values[offset:offset + 900]
            placeholders = ','.join('?' for _ in batch)
            result.update(row[0] for row in c.execute(
                f'SELECT DISTINCT {result_column} FROM {table} '
                f'WHERE {lookup_column} IN ({placeholders}){suffix}', batch))
    return result


def execution_evidence(c, task_ids):
    """Read evidence once while apply holds both databases' write transaction.

    Evidence cannot change concurrently under BEGIN IMMEDIATE, and migration
    writes only schedule fields. Native task and dispatch job state are still
    checked on each pass; only immutable execution evidence is cached here.
    """
    task_ids = {int(value) for value in task_ids}
    source_keys = {'fb:auto:' + str(value) for value in task_ids}
    native = {
        table: _matching_values(c, table, column, column,
                                task_ids if column == 'task_id' else source_keys, suffix)
        for table, column, suffix in NATIVE_EVIDENCE
    }
    job_ids = _matching_values(c, 'dispatch.pipeline_job', 'id', 'source_key',
                               source_keys, " AND stage='publish'")
    dispatch = {
        table: _matching_values(c, 'dispatch.' + table, 'job_id', 'job_id', job_ids)
        for table in DISPATCH_EVIDENCE
    }
    return {'native': native, 'dispatch': dispatch}


def native_reason(c, task, evidence=None):
    key='fb:auto:'+str(task['id'])
    if task['status'] not in {'planned','ready'}:
        return 'preserved_'+task['status']
    # started_at/attempt_count also describe media preparation, not publication.
    if any(task.get(k) for k in ('graph_post_id','unknown_outcome','lease_owner','lease_expires_at_utc')):
        return 'native_execution_or_receipt'
    for table,column,suffix in NATIVE_EVIDENCE:
        identity = task['id'] if column == 'task_id' else key
        found = (identity in evidence['native'][table] if evidence is not None
                 else bool(_rows(c,table,column+'=?'+suffix,(identity,))))
        if found: return table
    return ''


def dispatch_reason(c, task_id, evidence=None):
    key='fb:auto:'+str(task_id)
    jobs=_rows(c,'dispatch.pipeline_job',"source_key=? AND stage='publish'",(key,))
    for job in jobs:
        if job['status'] not in {'queued','ready'} or any(job.get(k) for k in ('generation','claim_hash','external_intent','canonical_receipt','lease_expires_at','hold_reason')):
            return 'dispatch_execution_or_hold'
        for table in DISPATCH_EVIDENCE:
            found = (job['id'] in evidence['dispatch'][table] if evidence is not None
                     else bool(_rows(c,'dispatch.'+table,'job_id=?',(job['id'],))))
            if found: return table
    return ''


def stable_facts(task):
    # Publication scheduling is the only business field this migration changes.
    return {k:v for k,v in task.items() if k not in {'planned_publish_at_utc','schedule_policy'}}


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()


def legacy_target(c,group,page_id,tasks):
    rows=_rows(c,'fb_auto_schedule_plan','template_id=? AND template_version=? AND local_date=?',(group['template_id'],group['version'],group['day']))
    times=json.loads(rows[0]['times_json']) if rows else []
    if times:
        times=[value for value in times if in_slot(group['config'],page_id,f"auto:v{group['version']}:{group['day']}:{value}",times=times)]
    else:
        # With no original full-day record, preserve only proven original slots.
        # Inventing missing times could violate the original Page spacing.
        times=sorted(dt.datetime.fromisoformat(t['planned_publish_at_utc'].replace('Z','+00:00')).astimezone(daily.SHANGHAI).strftime('%H:%M') for t in tasks)
    return {'target_id':page_id,'times':times,'slots':[{'ordinal':i,'publish_time':value,'scheduled_at':daily.utc_at(group['day'],value)} for i,value in enumerate(times)]}


def connect(fb, dispatch, *, readonly):
    c=sqlite3.connect(Path(fb).resolve().as_uri()+('?mode=ro' if readonly else ''),uri=True,timeout=30)
    c.row_factory=sqlite3.Row
    c.execute('ATTACH DATABASE ? AS dispatch',(Path(dispatch).resolve().as_uri()+('?mode=ro' if readonly else ''),))
    if readonly:c.execute('PRAGMA query_only=ON')
    return c


def build_plan(fb, dispatch, effective_date, *, now=None):
    now=now or dt.datetime.now(dt.timezone.utc)
    if effective_date <= daily.local_day(now): raise ValueError('migration_effective_date_must_be_future')
    c=connect(fb,dispatch,readonly=True)
    try:
        groups={}
        for row in c.execute("SELECT t.*,r.config_json,r.trigger_type FROM fb_auto_task t JOIN fb_auto_run r ON r.id=t.run_id WHERE t.planned_publish_at_utc>=? ORDER BY t.planned_publish_at_utc,t.id",(daily.utc_at(effective_date),)):
            raw=dict(row); config=json.loads(raw.pop('config_json')); trigger=raw.pop('trigger_type')
            if trigger!='auto' or config.get('schedule',{}).get('mode')!='random': continue
            day=daily.local_day(dt.datetime.fromisoformat(raw['planned_publish_at_utc'].replace('Z','+00:00')))
            key=(int(raw['template_id']),day)
            group=groups.setdefault(key,{'template_id':key[0],'day':day,'config':config,'version':raw['template_version'],'tasks':[]})
            if group['version']!=raw['template_version'] or group['config']!=config: raise ValueError('multiple_frozen_versions_in_day')
            group['tasks'].append(raw)
        results=[]
        memory=sqlite3.connect(':memory:')
        daily.ensure_schema(memory,now=now,effective_date=effective_date)
        try:
            for group in groups.values():
                by_page={}
                for task in group['tasks']:
                    if task.get('skip_reason')=='fb_auto_page_frequency_limit':continue
                    by_page.setdefault(task['page_id'],[]).append(task)
                plan=target_schedule.ensure_plan(memory,group['template_id'],group['version'],group['config'],group['day'],sorted(by_page),now=now)
                entries=[]; originals={}
                for target in plan['targets']:
                    tasks=sorted(by_page[target['target_id']],key=lambda t:(t['planned_publish_at_utc'],t['id']))
                    originals[target['target_id']]=legacy_target(c,group,target['target_id'],tasks)
                    if len(tasks)>len(target['slots']): raise ValueError('existing_tasks_exceed_page_daily_quota')
                    reasons={t['id']:native_reason(c,t) or dispatch_reason(c,t['id']) for t in tasks}
                    # An immutable existing slot is a real spacing constraint on
                    # this Page. Keep its whole day's original order/times only.
                    preserve_page=any(reasons.values())
                    if preserve_page:
                        target.update(originals[target['target_id']])
                    for ordinal,task in enumerate(tasks):
                        if preserve_page:
                            old_minute=dt.datetime.fromisoformat(task['planned_publish_at_utc'].replace('Z','+00:00')).astimezone(daily.SHANGHAI).strftime('%H:%M')
                            ordinal=target['times'].index(old_minute)
                        slot=target['slots'][ordinal]
                        before=task['planned_publish_at_utc']
                        after=dt.datetime.fromisoformat(slot['scheduled_at'].replace('Z','+00:00')).isoformat(timespec='seconds')
                        reason=reasons[task['id']]
                        if preserve_page:
                            after=before
                            slot['scheduled_at']=dt.datetime.fromisoformat(before.replace('Z','+00:00')).astimezone(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
                            slot['publish_time']=dt.datetime.fromisoformat(before.replace('Z','+00:00')).astimezone(daily.SHANGHAI).strftime('%H:%M')
                            target['times'][ordinal]=slot['publish_time']
                        entries.append({'task_id':task['id'],'page_id':task['page_id'],'ordinal':ordinal,'before':before,'after':after,
                                        'change_policy':not bool(reason),'preserved_reason':reason or ('same_page_immutable_slot' if preserve_page else ''),
                                        'facts_sha256':digest(stable_facts(task))})
                results.append({'plan':plan,'entries':entries,'original_targets':originals})
        finally: memory.close()
        body={'policy':daily.POLICY,'effective_date':effective_date,'created_at':now.isoformat(),'groups':results}
        body['plan_sha256']=digest(body)
        return body
    finally:c.close()


def _rewrite_times(value, before, after):
    if isinstance(value,dict):
        return {k:(after if k in {'scheduled_at','planned_publish_at_utc','cutover_due_at'} and v else _rewrite_times(v,before,after)) for k,v in value.items()}
    if isinstance(value,list):return [_rewrite_times(v,before,after) for v in value]
    return value


def apply_plan(fb,dispatch,plan,*,now=None):
    supplied=dict(plan); expected=supplied.pop('plan_sha256')
    if digest(supplied)!=expected:raise ValueError('manifest_hash_mismatch')
    now=now or dt.datetime.now(dt.timezone.utc)
    if plan['effective_date']<=daily.local_day(now):raise ValueError('migration_window_closed')
    c=connect(fb,dispatch,readonly=False)
    changed=0
    skipped=[]
    applied_groups=[]
    try:
        c.execute('BEGIN IMMEDIATE')
        daily.ensure_schema(c,now=now,effective_date=plan['effective_date'])
        evidence=execution_evidence(c,(entry['task_id'] for group in plan['groups']
                                       for entry in group['entries'] if entry['change_policy']))
        for group in plan['groups']:
            # Revalidate under the write transaction. A concurrently changed
            # task only preserves its Page's day (spacing is Page-local).
            group=json.loads(json.dumps(group))
            preserved_pages=set()
            for entry in group['entries']:
                row=c.execute('SELECT * FROM fb_auto_task WHERE id=?',(entry['task_id'],)).fetchone()
                if row is None:raise ValueError('task_identity_removed')
                task=dict(row)
                reason=native_reason(c,task,evidence) or dispatch_reason(c,task['id'],evidence) if entry['change_policy'] else ''
                if digest(stable_facts(task))!=entry['facts_sha256'] or task['planned_publish_at_utc'] not in {entry['before'],entry['after']}:
                    reason=reason or 'task_changed_since_preview'
                if reason:
                    preserved_pages.add(entry['page_id'])
                    skipped.append({'task_id':task['id'],'reason':reason})
            for entry in group['entries']:
                if entry['page_id'] not in preserved_pages:continue
                task=dict(c.execute('SELECT * FROM fb_auto_task WHERE id=?',(entry['task_id'],)).fetchone())
                entry.update(change_policy=False,after=task['planned_publish_at_utc'],facts_sha256=digest(stable_facts(task)))
                for target in group['plan']['targets']:
                    if target['target_id']!=entry['page_id']:continue
                    target.update(group['original_targets'][entry['page_id']])
                    value=dt.datetime.fromisoformat(entry['after'].replace('Z','+00:00'))
                    entry['ordinal']=target['times'].index(value.astimezone(daily.SHANGHAI).strftime('%H:%M'))
                    target['slots'][entry['ordinal']].update(scheduled_at=value.astimezone(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),publish_time=value.astimezone(daily.SHANGHAI).strftime('%H:%M'))
                    target['times'][entry['ordinal']]=value.astimezone(daily.SHANGHAI).strftime('%H:%M')
            applied_groups.append(group)
            frozen=group['plan']; source=frozen['source_key']; day=frozen['local_date']
            existing=daily.get_day_plan(c,source,day)
            if existing and (existing['config']!=frozen['config'] or existing['targets']!=frozen['targets']):raise ValueError('day_plan_changed')
            c.execute('INSERT OR IGNORE INTO social_random_day VALUES(?,?,?,?,?,?)',(source,day,str(frozen['config_version']),json.dumps(frozen['config'],ensure_ascii=False,sort_keys=True),json.dumps(frozen['targets'],ensure_ascii=False,sort_keys=True),frozen['created_at']))
            for target in frozen['targets']:
                for slot in target['slots']:
                    c.execute('INSERT OR IGNORE INTO social_random_slot(source_key,local_date,target_id,ordinal,planned_at) VALUES(?,?,?,?,?)',(source,day,target['target_id'],slot['ordinal'],slot['scheduled_at']))
            for entry in group['entries']:
                task=dict(c.execute('SELECT * FROM fb_auto_task WHERE id=?',(entry['task_id'],)).fetchone())
                if digest(stable_facts(task))!=entry['facts_sha256']:raise ValueError('task_facts_changed:'+str(entry['task_id']))
                if task['planned_publish_at_utc'] not in {entry['before'],entry['after']}:raise ValueError('task_time_changed')
                if entry['change_policy']:
                    reason=native_reason(c,task,evidence) or dispatch_reason(c,task['id'],evidence)
                    if reason:raise ValueError('task_execution_changed:'+str(task['id'])+':'+reason)
                    c.execute('UPDATE fb_auto_task SET planned_publish_at_utc=?,schedule_policy=? WHERE id=?',(entry['after'],daily.POLICY,task['id']))
                    for job in _rows(c,'dispatch.pipeline_job',"source_key=? AND stage='publish'",('fb:auto:'+str(task['id']),)):
                        scheduled=dt.datetime.fromisoformat(entry['after'].replace('Z','+00:00')).astimezone(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
                        meta=json.loads(job['source_meta_json']);meta['cutover_due_at']=scheduled
                        payload=_rewrite_times(json.loads(job['payload_json']),entry['before'],entry['after'])
                        c.execute('UPDATE dispatch.pipeline_job SET scheduled_at=?,source_meta_json=?,payload_json=? WHERE id=?',(scheduled,json.dumps(meta,sort_keys=True),json.dumps(payload,sort_keys=True),job['id']))
                    changed+=int(entry['before']!=entry['after'])
                daily.bind_slot(c,source,day,entry['page_id'],entry['ordinal'],'fb_auto_task',task['id'])
            # Legacy pending intentions must not create another batch next day.
            # Existing prepared runs/slot keys remain immutable audit evidence.
            c.execute("UPDATE fb_auto_due_slot SET status='prepared',error_code='target_random_day_mapped' WHERE template_id=? AND slot_key LIKE ? AND status='pending' AND schedule_policy<>?",(int(source.rsplit(':',1)[1]),'%:'+day+':%',daily.POLICY))
        c.commit()
        for group in applied_groups:
            for entry in group['entries']:
                task=dict(c.execute('SELECT * FROM fb_auto_task WHERE id=?',(entry['task_id'],)).fetchone())
                if digest(stable_facts(task))!=entry['facts_sha256']:raise ValueError('verification_facts_changed')
                if entry['change_policy'] and task['planned_publish_at_utc']!=entry['after']:raise ValueError('verification_time_changed')
                for job in _rows(c,'dispatch.pipeline_job',"source_key=? AND stage='publish'",('fb:auto:'+str(task['id']),)):
                    if entry['change_policy'] and dt.datetime.fromisoformat(job['scheduled_at'].replace('Z','+00:00'))!=dt.datetime.fromisoformat(entry['after'].replace('Z','+00:00')):raise ValueError('verification_dispatch_time_mismatch')
        return {'groups':len(plan['groups']),'bound':sum(len(g['entries']) for g in plan['groups']),'retimed':changed,'skipped':skipped,'plan_sha256':expected}
    except BaseException:
        c.rollback();raise
    finally:c.close()


def reverse_before_effective(fb,dispatch,plan,*,now=None):
    """Reverse only unexecuted future intent, never a business database backup."""
    now=now or dt.datetime.now(dt.timezone.utc)
    supplied=dict(plan);expected=supplied.pop('plan_sha256')
    if digest(supplied)!=expected:raise ValueError('manifest_hash_mismatch')
    if daily.local_day(now)>=plan['effective_date']:raise ValueError('compatibility_rollback_required_after_activation')
    c=connect(fb,dispatch,readonly=False)
    try:
        c.execute('BEGIN IMMEDIATE')
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='social_random_day'").fetchone() and c.execute('SELECT 1 FROM social_random_day LIMIT 1').fetchone():
            raise ValueError('compatibility_rollback_required_for_frozen_days')
        ids={e['task_id'] for g in plan['groups'] for e in g['entries'] if e['change_policy']}
        if any(r[0] not in ids for r in c.execute('SELECT id FROM fb_auto_task WHERE schedule_policy=?',(daily.POLICY,))):raise ValueError('new_target_tasks_require_compatibility_rollback')
        entries=[e for g in plan['groups'] for e in g['entries'] if e['change_policy']]
        for entry in entries:
            task=dict(c.execute('SELECT * FROM fb_auto_task WHERE id=?',(entry['task_id'],)).fetchone())
            if task['planned_publish_at_utc'] not in {entry['before'],entry['after']} or digest(stable_facts(task))!=entry['facts_sha256'] or native_reason(c,task) or dispatch_reason(c,task['id']):raise ValueError('rollback_task_changed:'+str(task['id']))
        for entry in entries:
            c.execute("UPDATE fb_auto_task SET planned_publish_at_utc=?,schedule_policy='' WHERE id=?",(entry['before'],entry['task_id']))
            for job in _rows(c,'dispatch.pipeline_job',"source_key=? AND stage='publish'",('fb:auto:'+str(entry['task_id']),)):
                value=dt.datetime.fromisoformat(entry['before'].replace('Z','+00:00')).astimezone(dt.timezone.utc).isoformat(timespec='milliseconds')
                meta=json.loads(job['source_meta_json']);meta['cutover_due_at']=value
                payload=_rewrite_times(json.loads(job['payload_json']),entry['after'],entry['before'])
                c.execute('UPDATE dispatch.pipeline_job SET scheduled_at=?,source_meta_json=?,payload_json=? WHERE id=?',(value,json.dumps(meta,sort_keys=True),json.dumps(payload,sort_keys=True),job['id']))
        c.commit()
        return {'restored_future_intents':len(entries),'ledgers_preserved':True}
    except BaseException:c.rollback();raise
    finally:c.close()


def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=['plan','apply']);p.add_argument('--fb',required=True);p.add_argument('--dispatch',required=True);p.add_argument('--manifest',required=True);p.add_argument('--effective-date');a=p.parse_args()
    target=Path(a.manifest)
    if a.action=='plan':
        plan=build_plan(a.fb,a.dispatch,a.effective_date)
        target.write_text(json.dumps(plan,ensure_ascii=False,indent=2)+'\n',encoding='utf-8');target.chmod(0o600)
        print(json.dumps({'groups':len(plan['groups']),'tasks':sum(len(g['entries']) for g in plan['groups']),'retime':sum(e['change_policy'] and e['before']!=e['after'] for g in plan['groups'] for e in g['entries']),'plan_sha256':plan['plan_sha256']}))
    else: print(json.dumps(apply_plan(a.fb,a.dispatch,json.loads(target.read_text(encoding='utf-8')))))


if __name__=='__main__':main()
