#!/usr/bin/env python3
"""One bounded, reconciled retry for explicit code-9 account denials."""
import argparse
import collections
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import time

import meta_asset_permission_sync as m
import meta_asset_permission_followup as f


def denied_keys(events, results):
    if any(e['event']=='bm_paused' for e in events):
        raise ValueError('restricted_parent_cannot_be_retried')
    last={e['key']:e for e in events if e['event']=='response'}
    keys,actors=[],set()
    for key,r in results.items():
        response=last.get(key,{})
        safe=response.get('response',{});error=safe.get('error',{})
        if (key.startswith('account:') and r.get('status')=='failed' and safe.get('http')==400
                and error.get('code')==9 and error.get('error_subcode') is None
                and error.get('is_transient') is True and 'Access denied' in error.get('message','')
                and response.get('acknowledged') is False):
            keys.append(key);actors.add(response['actor']['credential_id'])
    return sorted(keys),sorted(actors)


def parent_evidence(path):
    summary=json.loads((path/'execution_summary.json').read_text())
    if summary['bm_paused'] or summary['unattempted']:
        raise ValueError('parent_not_complete_or_is_restricted')
    events=[json.loads(s) for s in (path/'journal.jsonl').read_text().splitlines()]
    manifest=json.loads((path/'manifest.json').read_text())
    digest=hashlib.sha256((path/'manifest.json').read_bytes()).hexdigest()
    if digest!=(path/'manifest.sha256').read_text().strip() or any(e['manifest_sha256']!=digest for e in events):
        raise ValueError('parent_integrity_failure')
    keys,actors=denied_keys(events,json.loads((path/'results.json').read_text()))
    if not keys:raise ValueError('no_explicit_denials_to_retry')
    return manifest,events,keys,{'parent_run':str(path),'manifest_sha256':digest,
        'journal_sha256':hashlib.sha256((path/'journal.jsonl').read_bytes()).hexdigest(),'excluded_actor_ids':actors}


def prepare(args,db,g,cs):
    p=Path(args.run_dir);parent,events,keys,proof=parent_evidence(Path(args.parent_run))
    if (p/'manifest.json').exists():raise ValueError('manifest_already_exists')
    if parent['input_scope'].get('type')!='account_pools':raise ValueError('not_pool_parent')
    recovery=f.prior_restriction(Path(args.blocked_run))
    admin=next(c for c in cs if c['id']==recovery['actor_credential_id'])
    if m.public_credential(admin)['fingerprint']!=recovery['actor_fingerprint']:raise ValueError('original_actor_changed')
    users=g.rows(m.DEFAULT_BM+'/system_users',admin,'id,name,role')
    if not m.verify_admin(g,admin,m.DEFAULT_BM,users):raise ValueError('original_actor_not_admin')
    source=m.single_credential(cs,803);target=m.single_credential(cs,916)
    identities={side:m.verify_identity(g,c,users) for side,c in [('source',source),('target',target)]}
    if identities['target']['meta_id']!=recovery['target_id']:raise ValueError('target_identity_changed')
    assets=[];checks=[]
    for old in parent['assets']:
        if 'account:'+old['asset_id'] not in keys:continue
        a={**old,'checked_at':m.now()};key=a['asset_id']
        a['source_live']=m.live_asset(g,source,a)
        a['backend']=db.assets('account',['act_'+key]).get(key)
        assigned=g.rows('act_'+key+'/assigned_users',admin,'id,tasks',{'business':m.DEFAULT_BM})
        before=assigned.get(identities['target']['meta_id'])
        own=m.read_target_before(g,target,a,identities['target']['meta_id'],m.DEFAULT_BM,before)
        sr=assigned.get(identities['source']['meta_id'])
        own_source=m.read_assignment(g,source,a,identities['source']['meta_id'],m.DEFAULT_BM)
        visible=False;target_live=None
        if own is not None:
            try:
                target_live=m.live_asset(g,target,a)
                visible=m.meets_requirements('account',target_live.get('user_tasks'),'basic_ads')
            except m.GraphError as e:
                if m.error_kind(e.response) in ('restricted','rate_limited'):raise
                target_live={'read_error':m.safe_response(e.response)}
        share=m.current_share_grant(g,admin,source,a,a['source_live'],m.DEFAULT_BM)
        f.classify(a,own_source,sr,own,before,True,visible,share,False)
        if not m.pool_membership_current(db,a):a.update(category='unknown',reason='removed_or_disabled_in_selected_pools')
        checks.append({'key':'account:'+key,'at':m.now(),'admin_assignment':before,'target_own_assignment':own,'target_live':target_live})
        assets.append(a)
    m.write_json(p/'denial-reconciliation.json',checks)
    candidates=[a for a in assets if a['category'] in ('missing','insufficient')]
    if candidates:recovery.update(prior_restricted_key=recovery['key'],key='account:'+candidates[0]['asset_id'],mode='explicit_account_pool_probe')
    else:recovery=None
    last_write=max(datetime.fromisoformat(e['at']).timestamp() for e in events if e['event']=='intent')
    manifest={'format_version':3,'created_at':m.now(),'code_version':args.code_version,'complete':True,
        'business_id':m.DEFAULT_BM,'graph_version':'v26.0','permission_profile':'basic_ads','identities':identities,
        'input_scope':parent['input_scope'],'assets':assets,'recovery':recovery,'denial_retry':proof,
        'not_before_epoch':last_write+300,'write_interval':30,'token_interval':300,
        'counts':{'account':dict(collections.Counter(a['category'] for a in assets))}}
    m.write_json(p/'manifest.json',manifest)
    digest=hashlib.sha256((p/'manifest.json').read_bytes()).hexdigest();(p/'manifest.sha256').write_text(digest+'\n')
    shutil.copyfile(Path(args.parent_run)/'admin_checks.json',p/'admin_checks.json')
    m.export_csv(p/'comparison.csv',assets)
    m.emit('denial_retry_prepared',counts=manifest['counts'],excluded_actor_ids=proof['excluded_actor_ids'],manifest_sha256=digest)


def execute(args,db,g,cs):
    p=Path(args.run_dir);manifest=json.loads((p/'manifest.json').read_text())
    proof=manifest['denial_retry'];_,_,keys,current=parent_evidence(Path(proof['parent_run']))
    if current!=proof or any('account:'+a['asset_id'] not in keys for a in manifest['assets']):
        raise ValueError('retry_parent_or_scope_changed')
    if not any(a['category'] in ('missing','insufficient') for a in manifest['assets']):
        m.emit('no_retry_writes_needed');return
    remaining=max(0,manifest['not_before_epoch']-time.time())
    if remaining:m.emit('prior_write_cooldown',seconds=round(remaining))
    while time.time()<manifest['not_before_epoch']:time.sleep(min(30,manifest['not_before_epoch']-time.time()))
    credentials=[c for c in cs if c['id'] not in proof['excluded_actor_ids']]
    args.command='execute'
    m.execute(args,db,g,credentials)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['prepare','execute'])
    for name in ['run-dir','parent-run','blocked-run','code-version']:parser.add_argument('--'+name,required=True)
    parser.add_argument('--business',default=m.DEFAULT_BM)
    parser.add_argument('--credential-config',default='/root/codex_test/authorize_fb_accounts_to_user913_20260819.py')
    parser.add_argument('--admin-cache',default='/root/codex_test/output/meta_bm_admin_token_cache.json')
    args=parser.parse_args();p=Path(args.run_dir).resolve()
    if not str(p).startswith('/mnt/data-disk/') or subprocess.check_output(['findmnt','-no','UUID','/mnt/data-disk'],text=True).strip()!='3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':raise ValueError('data_disk_invalid')
    p.mkdir(parents=True,exist_ok=True)
    import fcntl
    with (p/'.run.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        db=m.Database(args.credential_config)
        try:(prepare if args.command=='prepare' else execute)(args,db,m.Graph('v26.0'),db.credentials())
        finally:db.conn.close()
