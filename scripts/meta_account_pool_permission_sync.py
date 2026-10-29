#!/usr/bin/env python3
"""Freeze a bounded pool-account manifest from fresh read-only evidence."""
import argparse
import collections
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess

import meta_asset_permission_sync as m
import meta_asset_permission_followup as f


def active_members(members, pools):
    out = collections.defaultdict(list)
    for row in members:
        if row['group_id'] in pools and row['platform']==0 and row['deleted_at']==0:
            key=m.normalized_id(row['account_id'])
            if not key.isdigit(): raise ValueError('invalid_pool_account_id')
            out[key].append(row)
    return dict(out)


def audit(args):
    p=Path(args.run_dir).resolve()
    if not str(p).startswith('/mnt/data-disk/') or subprocess.check_output(
            ['findmnt','-no','UUID','/mnt/data-disk'],text=True).strip()!='3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':
        raise ValueError('run_directory_or_data_disk_invalid')
    if (p/'manifest.json').exists(): raise ValueError('manifest_already_exists')
    evidence=json.loads((p/'readonly-audit.json').read_text())
    if (datetime.now(timezone.utc)-datetime.fromisoformat(evidence['at'])).total_seconds()>1800:
        raise ValueError('read_only_evidence_too_old')
    pools=sorted(set(args.pool_id))
    recovery=f.prior_restriction(Path(args.prior_run))
    db=m.Database(args.credential_config)
    try:
        groups=db.query('SELECT id,name,user_id,updated_at FROM ads_alternative_account_group WHERE id IN ('+','.join(['%s']*len(pools))+')',tuple(pools))
        if {g['id'] for g in groups}!=set(pools): raise ValueError('pool_not_found')
        members=db.query('SELECT id,group_id,account_id,platform,deleted_at,account_status,updated_at FROM ads_alternative_accounts WHERE group_id IN ('+','.join(['%s']*len(pools))+') ORDER BY group_id,id',tuple(pools))
        active=active_members(members,pools)
        if set(active)!={r['asset_id'] for r in evidence['rows']}:
            raise ValueError('pool_scope_changed_repeat_readonly_audit')
        m.write_json(p/'pool-membership-current.json',{'at':m.now(),'groups':groups,'members':members})
        cs=db.credentials();g=m.Graph('v26.0')
        admin=next(c for c in cs if c['id']==recovery['actor_credential_id'])
        if m.public_credential(admin)['fingerprint']!=recovery['actor_fingerprint']:
            raise ValueError('original_actor_changed')
        users=g.rows(m.DEFAULT_BM+'/system_users',admin,'id,name,role')
        if not m.verify_admin(g,admin,m.DEFAULT_BM,users): raise ValueError('original_actor_not_admin')
        source=m.single_credential(cs,803);target=m.single_credential(cs,916)
        identities={side:m.verify_identity(g,c,users) for side,c in [('source',source),('target',target)]}
        if identities['target']['meta_id']!=recovery['target_id'] or identities!=evidence['identities']:
            raise ValueError('identity_changed')
        backend=db.assets('account',['act_'+key for key in active])
        assets=[]
        for item in evidence['rows']:
            key=item['asset_id'];target_row=item['target_assignment']
            before=(target_row or {}).get('tasks') or []
            a={'kind':'account','asset_id':key,'name':item['name'],'pool_ids':sorted({r['group_id'] for r in active[key]}),
               'business_id':m.DEFAULT_BM,'permission_profile':'basic_ads','source_live':item['source_live'],
               'backend':backend.get(key),'before_tasks':before,'desired_tasks':sorted(set(before)|m.BASIC_AD_TASKS),
               'target_assigned':target_row is not None,'source_tasks':(item['source_live'] or {}).get('user_tasks'),
               'checked_at':m.now()}
            state,reason=m.eligibility('account',a['source_live'] or {},a['backend'])
            if any(r['account_status']!=1 for r in active[key]): state,reason='unknown','pool_status_not_normal'
            if state!='normal': a.update(category=state,reason=reason)
            elif item['category']=='already_sufficient': a.update(category='already_sufficient',reason='verified_basic_ad_access')
            elif item['category']!='needs_authorization': a.update(category='unknown',reason='readonly_evidence_conflict')
            else:
                try:
                    assigned=g.rows('act_'+key+'/assigned_users',admin,'id,name,tasks',{'business':m.DEFAULT_BM})
                    observed=assigned.get(identities['target']['meta_id'])
                    sr=assigned.get(identities['source']['meta_id'])
                    grant=m.current_share_grant(g,admin,source,a,a['source_live'],m.DEFAULT_BM)
                    a['bm_permitted_tasks']=(grant or {}).get('permitted_tasks')
                    a['delegable']=bool(grant and (grant.get('owned') or m.meets_requirements('account',grant.get('permitted_tasks'),'basic_ads')))
                    if not m.assignment_equal(observed,target_row) or not sr or set(sr.get('tasks') or [])!=set(a['source_tasks'] or []):
                        a.update(category='unknown',reason='same_edge_admin_task_disagreement')
                    elif target_row and not m.meets_requirements('account',(item['target_live'] or {}).get('user_tasks'),'basic_ads'):
                        a.update(category='unknown',reason='target_live_access_unconfirmed')
                    elif not grant: a.update(category='unknown',reason='bm_share_grant_unavailable')
                    elif not a['delegable']: a.update(category='not_delegable',reason='owner_shared_without_required_tasks')
                    else: a.update(category='missing' if target_row is None else 'insufficient',reason='needs_required_access')
                except m.GraphError as e:
                    if m.error_kind(e.response) in ('restricted','rate_limited'): raise
                    a.update(category='unknown',reason='preflight_read_failed',read_error=m.safe_response(e.response))
            assets.append(a)
            m.write_json(p/'pool-audit-progress.json',{'done':len(assets),'total':len(evidence['rows'])})
        candidates=sorted((a for a in assets if a['category'] in ('missing','insufficient')),key=lambda a:a['asset_id'])
        if candidates:
            recovery.update(prior_restricted_key=recovery['key'],key='account:'+candidates[0]['asset_id'],mode='explicit_account_pool_probe')
        else: recovery=None
        manifest={'format_version':3,'created_at':m.now(),'complete':True,'code_version':args.code_version,
                  'permission_profile':'basic_ads','graph_version':'v26.0','business_id':m.DEFAULT_BM,
                  'identities':identities,'assets':assets,'recovery':recovery,'write_interval':30,'token_interval':300,
                  'input_scope':{'type':'account_pools','pool_ids':pools,'membership_sha256':hashlib.sha256((p/'pool-membership-current.json').read_bytes()).hexdigest()},
                  'counts':{'account':dict(collections.Counter(a['category'] for a in assets))}}
        m.write_json(p/'manifest.json',manifest)
        digest=hashlib.sha256((p/'manifest.json').read_bytes()).hexdigest()
        (p/'manifest.sha256').write_text(digest+'\n')
        m.export_csv(p/'comparison.csv',assets)
        m.emit('pool_audit_complete',counts=manifest['counts'],manifest_sha256=digest,candidates=len(candidates))
    finally: db.conn.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir',required=True)
    parser.add_argument('--pool-id',type=int,action='append',required=True)
    parser.add_argument('--prior-run',required=True)
    parser.add_argument('--code-version',required=True)
    parser.add_argument('--credential-config',default='/root/codex_test/authorize_fb_accounts_to_user913_20260819.py')
    args=parser.parse_args()
    import fcntl
    with (Path(args.run_dir)/'.run.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        audit(args)
