"""All selected Ads, then Creatives, then shared-reference remediation.

Only definite dependency rejection can cause a same-run Creative retry.
Unknown results are never replayed. The local recovery queue survives new scans.
"""
from collections import Counter, defaultdict
from copy import deepcopy
import uuid
import time

from .dependency_store import append_shared_ad
from .graph import GraphClient, GraphError
from .policy import material_identity, evaluate_ad, _feedback_empty
from .runtime import digest, legacy_delete_lock, now


SUCCESS={'deleted_verified','deleted','already_deleted'}


def shared_state(ad):
    if ad.get('status')=='DELETED' or ad.get('effective_status')=='DELETED': return 'deleted'
    try:
        clear=all(_feedback_empty(ad.get(k)) for k in ('issues_info','ad_review_feedback'))
    except (TypeError,ValueError): return 'review_unreadable'
    if not clear or ad.get('effective_status')=='DISAPPROVED': return 'violating'
    if (ad.get('status') not in ('ACTIVE','PAUSED') or ad.get('effective_status') not in
            ('ACTIVE','PAUSED','ADSET_PAUSED','CAMPAIGN_PAUSED','WITH_ISSUES')):
        return 'state_not_eligible'
    return 'clear'


def dependency_failure(result):
    return result.get('status')=='failed' and result.get('response',{}).get('error_subcode')==3858800


def object_state(store,job,key):
    with store._transaction(False) as conn:
        return store._object_data(store._object(conn,job,key))


def has_ad_receipt(store,identifier):
    with store._transaction(False) as conn:
        return conn.execute('SELECT 1 FROM fb_asset_delete_v2_receipts WHERE object_key=?',
                            ('ad:'+identifier,)).fetchone() is not None


def basis_valid(runner,basis,evidence,disabled=False,*,context=None,graph=None,receipt=None):
    """Recheck the exact material/account/credential even after Ad deletion."""
    from .runner import numeric_id, object_id
    if context is None:
        runner.serial_context_checked_at=time.monotonic()
        with runner.sql_snapshot():
            context=runner.source.current_ad_context(basis['ad_id'],basis['creative_id'])
    graph=graph or runner.execution_graph
    if hasattr(getattr(graph,'http',None),'account'):graph.http.account=basis['account_id']
    identity=material_identity(context['rows'],context['sources'],context['scope'],basis['creative_id'])
    if (identity['reason'] or identity['root_id']!=basis['root_id'] or
            set(identity['material_ids'])!=set(basis['material_ids']) or
            set(identity['product_ids'])!=set(basis['identity']['product_ids']) or
            {numeric_id(x) for x in context['credential_users']}!={basis['user_id']}):
        return False,'creative_basis_changed'
    if disabled:
        diagnostic=graph.account_diagnostic(basis['account_id'],basis['user_id'])
        if diagnostic.get('account_status')==2: return True,'disabled_account_creative_probe'
    if (receipt if receipt is not None else has_ad_receipt(runner.store,basis['ad_id'])): return True,'confirmed_ad_deleted'
    try:
        ad=graph.get_ad(basis['ad_id'],basis['user_id'])
    except GraphError:
        diagnostic=graph.account_diagnostic(basis['account_id'],basis['user_id'])
        if diagnostic.get('account_status')==2:return True,'disabled_account_creative_probe'
        raise
    if str(ad.get('account_id'))!=basis['account_id'] or object_id(ad)!=basis['creative_id']:
        return False,'creative_basis_changed'
    # Exact GET proof also covers an Ad deleted outside this job.
    if ad.get('status')=='DELETED': return True,'ad_deleted_readback'
    decision=evaluate_ad(ad,identity,evidence)
    return decision['eligible'],decision['reason']


def execute(runner,run_id,manifest,max_ads=None,start_phase='start'):
    from .runner import ACTOR,numeric_id,object_id,safe_error,ledger_error
    if digest({k:v for k,v in manifest.items() if k!='sha256'})!=manifest.get('sha256'):
        raise ValueError('manifest_hash_mismatch')
    if manifest.get('run_id')!=run_id or manifest.get('policy_version')!='20261024-v2' or manifest.get('comparison_only'):
        raise ValueError('execution_manifest_mismatch')
    if max_ads is not None and (isinstance(max_ads,bool) or not isinstance(max_ads,int) or max_ads<1):
        raise ValueError('max_ads_must_be_positive')
    if (runner.runtime.notice(run_id,start_phase) or {}).get('status')!='sent':
        raise RuntimeError('start_notification_not_confirmed')
    groups=defaultdict(list);entries={};objects=[];seen=set()
    for candidate in manifest['candidates']:
        for k in ('ad_id','creative_id','account_id','user_id'):numeric_id(candidate[k])
        if candidate['ad_id'] in seen or candidate['root_id'] not in ('1479','3302'):
            raise ValueError('candidate_scope_conflict')
        seen.add(candidate['ad_id']);groups[candidate['creative_id']].append(candidate)
    for entry in manifest.get('recovery_creatives',[]):
        entries[entry['basis']['creative_id']]=deepcopy(entry)
    for candidate in manifest.get('blocked_account_creatives',[]):
        cid=candidate['creative_id']
        entries[cid]={'basis':candidate,'disabled_account':True,'origin_run_id':run_id,
                     'origin_manifest_sha256':manifest['sha256'],'evidence':{'materials':{
                         mid:manifest['evidence']['materials'][mid] for mid in candidate['material_ids']
                         if mid in manifest['evidence']['materials']}}}
    for cid,items in groups.items():
        first=items[0]
        if any(c['account_id']!=first['account_id'] for c in items):raise ValueError('creative_account_conflict')
        entries[cid]={'basis':first,'origin_run_id':run_id,'origin_manifest_sha256':manifest['sha256'],
                     'evidence':{'materials':{mid:manifest['evidence']['materials'][mid]
                         for mid in first['material_ids'] if mid in manifest['evidence']['materials']}}}
    for cid,entry in entries.items():
        basis=entry['basis']
        for k in ('ad_id','creative_id','account_id','user_id'):numeric_id(basis[k])
        if basis['root_id'] not in ('1479','3302') or not entry.get('origin_manifest_sha256'):
            raise ValueError('recovery_scope_invalid')
        objects.append({'kind':'creative','object_id':cid,'account_ids':[basis['account_id']],
            'user_ids':[basis['user_id']],'product_ids':basis['identity']['product_ids']})
    for cid,items in groups.items():
        for c in items:
            objects.append({'kind':'ad','object_id':c['ad_id'],'creative_ids':[cid],
                'account_ids':[c['account_id']],'user_ids':[c['user_id']],
                'material_ids':c['material_ids'],'rules':c['rules']})
    job_id='policy-cleanup-'+run_id
    try:job=runner.store.get_job(job_id)
    except Exception as exc:
        if getattr(exc,'code',None)!='not_found':raise
        job=runner.store.create_job({'job_id':job_id,'preview_id':manifest['sha256'],'actor':ACTOR,
            'status':'ready','objects':objects,'job_source':'scheduled_violation_cleanup',
            'policy_version':'20261024-v2','manifest_sha256':manifest['sha256']})
    if job['preview_id']!=manifest['sha256'] or job['actor']!=ACTOR:raise ValueError('existing_job_manifest_mismatch')
    old={obj['key']:obj for obj in job['objects']}
    if any(o['kind']+':'+o['object_id'] not in old for o in objects):raise ValueError('existing_job_scope_mismatch')
    chosen=set(entries);limited=0
    if max_ads is not None:
        chosen=set();total=0
        for cid,items in groups.items():
            pending=[c for c in items if old['ad:'+c['ad_id']]['status']=='pending']
            if pending and total+len(pending)<=max_ads:
                chosen.add(cid);total+=len(pending)
            else:limited+=len(pending)
    shared=runner.store.claim_run(job_id,manifest['sha256'],ACTOR,['ad','creative'],job_id+'-'+uuid.uuid4().hex)
    shared_run=shared['run_id'];counts=Counter();results=[];final='partial';isolated=None
    failures={};deferred={};account_inventory={};parallel=None
    def record(kind,identifier,result,stage):
        row={'kind':kind,'id':identifier,'result':result,'stage':stage}
        results.append(row);counts[kind+'_'+result['status']]+=1
        if result.get('api_accepted'):counts[kind+'_api_accepted']+=1
        runner.runtime.event(run_id,'delete_result',row)
        runner.runtime.update(run_id,result_counts=dict(counts))
    def locked(callback):
        while True:
            runner.check_stop()
            try:
                with legacy_delete_lock(runner.config['legacy_lock'],runner.stop) as lease:
                    if lease is not None:lease.check()
                    return callback(lease)
            except InterruptedError as exc:
                if str(exc)!='legacy_priority_window':raise
                runner.runtime.event(run_id,'legacy_priority_yield',{'stage':'ads_first'})
    try:
        if isinstance(runner.graph,GraphClient):
            isolated=GraphClient(runner.graph.tokens,version=runner.graph.version,
                timeout=min(runner.graph.timeout,20),get_retries=0)
            isolated.share_read_metrics(runner.graph);isolated.delete_stop=runner.stop
            runner.execution_graph=isolated
        runner.runtime.update(run_id,stage='deleting_ads',shared_job_id=job_id,shared_run_id=shared_run)
        if runner.config.get('execution_workers',1)>1:
            from .concurrent_execution import ConcurrentExecutor
            parallel=ConcurrentExecutor(runner,run_id,manifest,job_id,shared_run,entries,groups,record,counts)
        if parallel:
            pending=[c for cid,items in groups.items() if cid in chosen for c in items
                if old['ad:'+c['ad_id']]['status']=='pending']
            for candidate,result in parallel.phase('ad',pending):
                record('ad',candidate['ad_id'],result,'ads')
        else:
            for cid,items in groups.items():
                if cid not in chosen:continue
                for candidate in items:
                    if old['ad:'+candidate['ad_id']]['status']!='pending':continue
                    def delete_ad(lease):
                        try:decision=runner.preflight(candidate,manifest)
                        except Exception as exc:
                            if ledger_error(exc) or isinstance(exc,InterruptedError):raise
                            decision={'eligible':False,'reason':'preflight_failed','error':safe_error(exc)}
                        runner.runtime.event(run_id,'preflight',{'ad_id':candidate['ad_id'],'stage':'ad','decision':decision})
                        if not decision['eligible']:
                            counts['skipped_ads']+=1;return None
                        # Preserve the last actually eligible basis, not a skipped
                        # first group member. Commit before the destructive boundary.
                        entries[cid]['basis']=candidate
                        entries[cid]['evidence']={'materials':{mid:manifest['evidence']['materials'][mid]
                            for mid in candidate['material_ids'] if mid in manifest['evidence']['materials']}}
                        runner.runtime.queue_creative(entries[cid])
                        if lease is not None:lease.check()
                        return runner.attempt(job_id,shared_run,'ad',candidate['ad_id'],candidate,run_id,lease)
                    result=locked(delete_ad)
                    if result is not None:record('ad',candidate['ad_id'],result,'ads')
        runner.runtime.update(run_id,stage='deleting_creatives')
        if parallel:
            pending=[]
            for cid in sorted(chosen):
                state=old['creative:'+cid]
                if state['status'] in ('deleted','already_deleted'):
                    runner.runtime.complete_creative(cid);continue
                if state['status']=='unknown':
                    counts['creative_unknown']+=1;continue
                pending.append(entries[cid]['basis'])
            for basis,result in parallel.phase('creative',pending):
                cid=basis['creative_id'];entry=entries[cid]
                record('creative',cid,result,'creatives')
                if result['status'] in SUCCESS:
                    runner.runtime.complete_creative(cid);failures.pop(cid,None);deferred.pop(cid,None)
                else:
                    entry.update(last_result=result,last_checked_at=now());runner.runtime.queue_creative(entry)
                    if dependency_failure(result):failures[cid]=entry
                    else:deferred[cid]=result
        else:
            for cid in sorted(chosen):
                runner.check_stop();entry=entries[cid];basis=entry['basis']
                state=object_state(runner.store,job_id,'creative:'+cid)
                if state['status'] in ('deleted','already_deleted'):
                    runner.runtime.complete_creative(cid);continue
                if state['status']=='unknown':
                    counts['creative_unknown']+=1;continue
                runner.runtime.queue_creative(entry)
                def delete_creative(lease):
                    # Do not mutate a dependency while one of its Ad writes is unknown.
                    for c in groups.get(cid,[]):
                        if object_state(runner.store,job_id,'ad:'+c['ad_id'])['status'] in ('unknown','in_progress'):
                            return {'status':'deferred','request_sent':False,'reason':'ad_result_unknown'}
                    try:eligible,reason=basis_valid(runner,basis,entry['evidence'],entry.get('disabled_account',False))
                    except Exception as exc:
                        if ledger_error(exc) or isinstance(exc,InterruptedError):raise
                        return {'status':'deferred','request_sent':False,'reason':'creative_basis_unavailable','error':safe_error(exc)}
                    if not eligible:return {'status':'deferred','request_sent':False,'reason':reason}
                    if lease is not None:lease.check()
                    return runner.attempt(job_id,shared_run,'creative',cid,basis,run_id,lease)
                result=locked(delete_creative);record('creative',cid,result,'creatives')
                if result['status'] in SUCCESS:runner.runtime.complete_creative(cid)
                else:
                    entry.update(last_result=result,last_checked_at=now());runner.runtime.queue_creative(entry)
                    if dependency_failure(result):failures[cid]=entry
                    else:deferred[cid]=result
        runner.runtime.update(run_id,stage='shared_reference_remediation',dependency_failures=len(failures))
        if parallel and isinstance(runner.graph,GraphClient):
            from .throttle import GovernedSession
            runner.graph.http=GovernedSession(runner.graph.http,parallel.governor,parallel.stopped)
        retry_entries={}
        for cid,entry in failures.items():
            runner.check_stop();basis=entry['basis'];account=basis['account_id'];uid=basis['user_id']
            if hasattr(getattr(runner.execution_graph,'http',None),'account'):runner.execution_graph.http.account=account
            key=(account,uid);blocked=[]
            try:
                if key not in account_inventory:
                    if hasattr(getattr(runner.graph,'http',None),'account'):runner.graph.http.account=account
                    # Only accounts whose Creative explicitly failed dependency
                    # deletion are enumerated, once each for this final phase.
                    account_inventory[key]=runner.graph.list_ads(account,uid)
                references=[ad for ad in account_inventory[key] if object_id(ad)==cid]
                runner.runtime.event(run_id,'shared_references',{'creative_id':cid,'ads':references})
                for ad in references:
                    def delete_reference(lease):
                        fresh=runner.execution_graph.get_ad(str(ad['id']),uid)
                        if str(fresh.get('account_id'))!=account or object_id(fresh)!=cid:
                            return {'status':'deferred','request_sent':False,'reason':'shared_binding_changed'}
                        state=shared_state(fresh)
                        if state=='deleted':return {'status':'deleted_verified','request_sent':False,'confirmed_deleted':True}
                        if state!='clear':
                            return {'status':'deferred','request_sent':False,'reason':state,'observed_ad':fresh}
                        append_shared_ad(runner.store,job_id,shared_run,cid,fresh,uid,ACTOR)
                        dependency=dict(basis,ad_id=str(ad['id']),shared_dependency=True)
                        if lease is not None:lease.check()
                        return runner.attempt(job_id,shared_run,'ad',str(ad['id']),dependency,run_id,lease)
                    result=locked(delete_reference);record('ad',str(ad['id']),result,'shared_ads')
                    if result['status'] not in SUCCESS:blocked.append({'ad_id':str(ad['id']),'result':result})
                if not blocked:
                    retry_entries[cid]=entry
            except Exception as exc:
                if ledger_error(exc) or isinstance(exc,InterruptedError):raise
                blocked.append({'reason':'reference_check_failed','error':safe_error(exc)})
            entry.update(blocked_ads=blocked,last_checked_at=now())
            runner.runtime.queue_creative(entry);deferred[cid]=entry.get('last_result',{})
        # Finish ALL newly discovered shared Ads before any Creative retry.
        runner.runtime.update(run_id,stage='retrying_creatives')
        for cid,entry in retry_entries.items():
            runner.check_stop();basis=entry['basis']
            try:
                def retry_creative(lease):
                    eligible,reason=basis_valid(runner,basis,entry['evidence'],entry.get('disabled_account',False))
                    if not eligible:return {'status':'deferred','request_sent':False,'reason':reason}
                    if lease is not None:lease.check()
                    return runner.attempt(job_id,shared_run,'creative',cid,basis,run_id,lease)
                result=locked(retry_creative)
            except Exception as exc:
                if ledger_error(exc) or isinstance(exc,InterruptedError):raise
                result={'status':'deferred','request_sent':False,'reason':'creative_retry_check_failed','error':safe_error(exc)}
            record('creative',cid,result,'creative_retry')
            if result['status'] in SUCCESS:
                runner.runtime.complete_creative(cid);deferred.pop(cid,None)
            else:
                entry.update(last_result=result,last_checked_at=now())
                runner.runtime.queue_creative(entry);deferred[cid]=result
        final='partial' if limited or deferred or manifest['coverage']['errors'] or counts['skipped_ads'] else 'completed'
    except BaseException:
        final='interrupted';raise
    finally:
        if parallel is not None:parallel.close()
        runner.execution_graph=runner.graph
        if isolated is not None:isolated.close()
        finished=runner.store.finish_run(shared_run,final,dict(counts));final=finished['status']
        current=runner.store.get_job(job_id)
        remaining=sum(o['kind']=='ad' and o['status']=='pending' for o in current['objects'])
        unresolved=sum(o['kind']=='ad' and o['status'] not in ('deleted','already_deleted') for o in current['objects'])
        runner.runtime.artifact(run_id,'execution-results-'+shared_run+'.json',results)
        runner.runtime.artifact(run_id,'execution-results.json',results)
        runner.runtime.artifact(run_id,'deferred-cleanup.json',runner.runtime.cleanup_backlog())
        runner.runtime.update(run_id,execution_status=final,result_counts=dict(counts),remaining_ads=remaining,unresolved_ads=unresolved,
            deferred_creatives=len(runner.runtime.cleanup_backlog()),stage='execution_finished')
    return final
