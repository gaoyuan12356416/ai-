"""Complete a preview after isolating definite multi-ID Graph read failures.

Reads only; never opens the deletion Store. Preserve the source manifest and
its freeze/deadline. A derived preview records both hashes and cannot extend
the one-hour live-conversion window.
"""
from collections import Counter
from copy import deepcopy
import time

from .policy import material_identity,evaluate_ad
from .runtime import digest,now
from .graph import GRAPH_METRIC_FIELDS


def combined_metrics(before,after,fields):
    result={}
    for key in fields:
        values=[v for v in (before.get(key,0),after.get(key,0)) if type(v) in (int,float)]
        result[key]=max(values,default=0) if key=='read_workers' or key.endswith('_max_percent') else sum(values)
    return result


def verify(runner,source_run_id,run_id,base,baseline):
    from .runner import chunks,numeric_id,object_id
    started=time.monotonic()
    for manifest in (base,baseline):
        if digest({k:v for k,v in manifest.items() if k!='sha256'})!=manifest['sha256']:
            raise ValueError('verification_manifest_hash_mismatch')
    if base['run_id']!=source_run_id or base.get('comparison_only') or base['policy_version']!='20261024-v2':
        raise ValueError('verification_source_not_fresh_preview')
    if base['comparison']['baseline_sha256']!=baseline['sha256']:
        raise ValueError('verification_baseline_mismatch')
    result=deepcopy(base);result.pop('sha256')
    result.update(run_id=run_id,verified_from_run_id=source_run_id,verified_from_sha256=base['sha256'])
    accounts={r['account_id']:r for r in base['coverage']['accounts']}
    failed=[r for r in base['coverage']['errors'] if r.get('error',{}).get('code') in (100,803)]
    target_keys=set();targets=[]
    for row in failed:
        aid=row['ad_id']
        uid=accounts[row['account_id']]['read_user_id']
        for value in (aid,row['account_id'],uid):numeric_id(value)
        key=(aid,row['account_id'],uid)
        if key in target_keys:continue
        target_keys.add(key)
        targets.append({'ad_id':aid,'account_id':row['account_id'],'user_id':uid})
    old_ids={c['ad_id'] for c in baseline['candidates']}
    candidates={c['ad_id']:c for c in base['candidates']}
    if set(candidates)&{r['ad_id'] for r in failed}:raise ValueError('verification_candidate_error_conflict')
    comparison=result['comparison']
    missing={r['ad_id']:r for r in comparison['current_missing_old']}
    recovered=set();clear_records={};checked={};read_errors={};skips=Counter(base['skip_counts'])
    runner.runtime.update(run_id,stage='isolating_batch_reads',recheck_targets=len(targets))
    for _,records,errors in runner.graph.iter_target_ads(targets,runner.stop):
        runner.check_stop();read_errors.update(errors)
        for aid,ad in records.items():
            recovered.add(aid)
            if (str(ad.get('status','')).upper() not in ('ACTIVE','PAUSED') or
                    str(ad.get('effective_status','')).upper() not in ('ACTIVE','PAUSED','ADSET_PAUSED','CAMPAIGN_PAUSED')):
                clear_records.pop(aid,None)
                reason='current_status_not_eligible';skips[reason]+=1
                checked[aid]={'reason':reason,'status':ad.get('status'),'effective_status':ad.get('effective_status'),
                    'creative_id':object_id(ad)}
                if aid in old_ids:missing[aid]=dict(ad_id=aid,**checked[aid])
            else:clear_records[aid]=ad
        runner.runtime.update(run_id,stage='isolating_batch_reads',recheck_readable=len(recovered),
            recheck_remaining_errors=len(read_errors),recheck_possible_candidates=len(clear_records))
    for aid in recovered:read_errors.pop(aid,None)
    for part in chunks(sorted(clear_records)):
        runner.check_stop()
        with runner.sql_snapshot():
            rows,sources=runner.lineage_data(part,scope=base['scope'])
            relevant=[r for values in rows.values() for r in values if str(r.get('product')) in base['scope']]
            routes=runner.source.selected_credentials(relevant)
        for aid in part:
            ad=clear_records[aid];cid=object_id(ad)
            identity=material_identity(rows.get(aid,[]),sources,base['scope'],cid)
            matching=[r for r in rows.get(aid,[]) if str(r.get('creative_id'))==cid and str(r.get('product')) in base['scope']]
            selected=[routes.get(str(r.get('id')), {}) for r in matching]
            users={r.get('user_id') for r in selected if r.get('user_id') and not r.get('error')}
            row_accounts={str(r.get('ad_account_id','')).removeprefix('act_') for r in matching}
            reason=identity['reason']
            if not reason and (len(users)!=1 or row_accounts!={str(ad['account_id'])} or
                    any(r.get('error') or not r.get('user_id') for r in selected)):
                reason='credential_lineage_unavailable'
            user=next(iter(users)) if len(users)==1 else ''
            if not reason and user not in runner.graph.tokens:reason='selected_credential_unavailable'
            if reason:
                decision={'eligible':False,'reason':reason};control=decision
            else:
                for value in (user,cid,ad['account_id']):numeric_id(value)
                decision=evaluate_ad(ad,identity,base['evidence'])
                control=evaluate_ad(ad,identity,baseline['evidence'])
            checked[aid]={'reason':control['reason'],'status':ad.get('status'),
                'effective_status':ad.get('effective_status'),'creative_id':cid}
            if control['eligible']:
                comparison['current_candidates']+=1
                if aid in old_ids:
                    missing.pop(aid,None);comparison['common_candidates']+=1
                else:comparison['new_candidate_count']+=1
            elif aid in old_ids:missing[aid]=dict(ad_id=aid,**checked[aid])
            if decision['eligible']:
                candidate={'ad_id':aid,'creative_id':cid,'account_id':str(ad['account_id']),
                    'user_id':user,'identity':identity,'ad_snapshot':ad}
                candidate.update({k:v for k,v in decision.items() if k not in candidate})
                candidates[aid]=candidate;runner.runtime.inventory(run_id,candidate['account_id'],[ad])
            else:skips[decision['reason']]+=1
        runner.runtime.update(run_id,stage='rechecking_recovered_context',recheck_context_done=len(checked),
            candidate_ads=len(candidates))
    result['candidates']=sorted(candidates.values(),key=lambda c:(int(c['account_id']),int(c['ad_id'])))
    result['blocked_account_creatives']=[c for c in base.get('blocked_account_creatives',[]) if c['ad_id'] not in recovered]
    result['coverage']['errors']=[r for r in base['coverage']['errors'] if r['ad_id'] not in recovered]
    for row in result['coverage']['errors']:
        if row['ad_id'] in read_errors:row['error']=read_errors[row['ad_id']]
    failed_accounts={r['account_id'] for r in result['coverage']['errors']}
    account_counts=Counter(c['account_id'] for c in result['candidates'])
    for row in result['coverage']['accounts']:
        row['candidates']=account_counts[row['account_id']]
        row['status']='partial' if row['account_id'] in failed_accounts else 'complete'
    comparison['current_missing_old']=[missing[aid] for aid in sorted(missing)]
    comparison['fresh_window_candidates']=len(candidates)
    result['skip_counts']=dict(skips)
    elapsed=round(time.monotonic()-started,3)
    result['timings']['batch_reverification_seconds']=elapsed
    result['timings']['total_seconds']+=elapsed
    comparison['timings']=result['timings']
    proof={'source_run_id':source_run_id,'source_sha256':base['sha256'],'source_frozen_at':base['frozen_at'],
        'checked_at':now(),'reverification_complete':True,'rechecked_ids':len(targets),
        'readable_after_isolation':len(recovered),'remaining_object_errors':len(read_errors),
        'added_fresh_candidates':len(candidates)-len(base['candidates']),
        'remaining_error_codes':dict(Counter(str(r['error'].get('code')) for r in result['coverage']['errors'])),
        'seconds':elapsed}
    # Deliberately retain original frozen_at; verification never renews expiry.
    assert result['frozen_at']==base['frozen_at']
    source_data=runner.runtime.get(source_run_id)['data'] if hasattr(runner.runtime,'get') else {}
    result['scan_graph_metrics']=combined_metrics(source_data.get('graph_metrics',{}),
        getattr(runner.graph,'safe_metrics',{}),GRAPH_METRIC_FIELDS)
    result['scan_sql_metrics']=combined_metrics(source_data.get('sql_metrics',{}),
        getattr(runner.source,'safe_metrics',{}),('query_count','query_failures','query_seconds','sessions','violation_pages'))
    result['batch_verification']=proof;result['sha256']=digest(result)
    runner.runtime.artifact(run_id,'batch-verification.json',proof)
    runner.runtime.artifact(run_id,'comparison.json',comparison)
    runner.runtime.artifact(run_id,'manifest.json',result)
    runner.runtime.update(run_id,stage='frozen',manifest_sha256=result['sha256'],candidate_ads=len(candidates),
        candidate_creatives=len({c['creative_id'] for c in result['candidates']}),
        product_candidate_ads=dict(Counter(c['root_id'] for c in result['candidates'])),
        coverage_errors=len(result['coverage']['errors']),scan_timings=result['timings'],
        material_count=len(base['evidence']['materials']),remaining_ads=len(candidates),
        rule1_ads=sum('rule1' in c['rules'] for c in result['candidates']),
        rule2_ads=sum('rule2' in c['rules'] for c in result['candidates']))
    return result
