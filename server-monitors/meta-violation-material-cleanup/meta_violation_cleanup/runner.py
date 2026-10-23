"""Frozen-scope scanner and serial Creative -> Ad executor."""
from collections import Counter, defaultdict
from contextlib import nullcontext
from datetime import datetime, timedelta
import json
import re
import sqlite3
import time
import uuid
from .policy import product_scope, parse_ids, material_identity, qualifying_policies, build_evidence, evaluate_ad
from .runtime import CST, digest, legacy_delete_lock, now
from .graph import GraphClient

ACTOR='automation:meta-violation-material-cleanup'

def safe_error(exc):
    data={'type':type(exc).__name__}
    # Never serialize exception text, arbitrary .safe payloads, or a token
    # accidentally returned by a broken credential provider.
    val=getattr(exc,'code',None)
    if isinstance(val,int) and not isinstance(val,bool):data['code']=val
    elif isinstance(val,str) and re.fullmatch(r'[a-z_][a-z0-9_]{0,79}',val):data['code']=val
    return data


def ledger_error(exc):
    return isinstance(exc,sqlite3.Error) or type(exc).__name__ in ('StoreError','RuntimeLedgerError')


def numeric_id(value):
    if isinstance(value,bool) or not re.fullmatch(r'[1-9][0-9]{0,31}',str(value)):
        raise ValueError('invalid_numeric_identity')
    return str(value)

def chunks(values,size=200):
    for i in range(0,len(values),size):
        yield values[i:i+size]

def source_keys(lineages):
    keys=set()
    for rows in lineages.values():
        for row in rows:
            try:keys.update(parse_ids(row.get('source_id')))
            except ValueError:pass
    return sorted(keys)

def object_id(ad):
    return str((ad.get('creative') or {}).get('id',''))

class Runner:
    def __init__(self, config, source, graph, runtime, shared_store, notifier, stop=lambda:False):
        self.config=config;self.source=source;self.graph=graph;self.runtime=runtime
        self.store=shared_store;self.notifier=notifier;self.stop=stop
        self.execution_graph=graph

    def check_stop(self):
        if self.stop():raise InterruptedError('stop_requested')

    def sql_snapshot(self):
        factory=getattr(self.source,'snapshot',None)
        return factory() if callable(factory) else nullcontext()

    def current_scope(self):
        reader=getattr(self.source,'scope_products',None)
        return product_scope(reader() if callable(reader) else self.source.products())

    def lineage_data(self, ids, violation_rows=()):
        data={};ids=list(ids)
        if not ids and not violation_rows:return {},{}
        with self.sql_snapshot():
            for part in chunks(ids):
                self.check_stop()
                data.update(self.source.lineages(part))
            keys=set(source_keys(data))
            keys.update(source_keys({'violations':violation_rows}))
            sources=self.source.sources(sorted(keys))
        return data,sources

    def account_inventories(self,accounts):
        parallel=getattr(self.graph,'iter_account_ads',None)
        if callable(parallel):
            yield from parallel(accounts,stop=self.stop)
            return
        for account,account_info in sorted(accounts.items()):
            self.check_stop();ads=None;read_user=None;errors=[]
            for uid in account_info['user_ids']:
                try:
                    ads=self.graph.list_ads(str(account),str(uid));read_user=str(uid);break
                except Exception as exc:errors.append({'user_id':str(uid),'error':safe_error(exc)})
            yield account,ads,read_user,errors

    def scan(self, run_id, start, end):
        settings=self.source.products();scope=product_scope(settings)
        known_products={str(row.get('id')) for row in settings
            if re.fullmatch(r'[1-9][0-9]{0,31}',str(row.get('id','')))}
        violations=self.source.violations(start,end)
        policy_rows=[r for r in violations if qualifying_policies(r.get('issues_info'))]
        lineage,sources=self.lineage_data(sorted({str(r['ad_id']) for r in policy_rows}),policy_rows)
        evidence=build_evidence(policy_rows,lineage,sources,scope,start,end)
        self.runtime.artifact(run_id,'violation-evidence.json',{'rows':policy_rows,'evidence':evidence,'start':start,'end':end})
        self.runtime.update(run_id,stage='account_discovery',vio_rows=len(violations),policy_rows=len(policy_rows),material_count=len(evidence['materials']))
        if isinstance(self.graph,GraphClient):
            self.graph.on_discovery_progress=lambda data:self.runtime.update(run_id,**data)
            self.graph.stop=self.stop
        discovery=self.graph.list_accounts()
        errors=list(discovery['errors']);account_results=[];candidates=[];skip=Counter();product_counts=Counter();mapping_gaps=Counter()
        accounts=discovery['accounts']
        seen_accounts=set()
        for index,(account,ads,read_user,account_errors) in enumerate(self.account_inventories(accounts)):
            self.check_stop();account=str(account)
            if account not in accounts or account in seen_accounts:raise ValueError('account_inventory_identity_conflict')
            seen_accounts.add(account)
            if ads is None:
                errors.append({'account_id':account,'stage':'inventory','errors':account_errors})
                account_results.append({'account_id':account,'status':'failed','errors':account_errors})
                self.runtime.update(run_id,stage='inventory',accounts_total=len(accounts),accounts_finished=index+1,coverage_errors=len(errors))
                continue
            try:
                identities,account_sources=self.lineage_data([str(a['id']) for a in ads])
                eligible=[];routed=[];account_skip=Counter();account_products=Counter();account_gaps=Counter()
                for ad in ads:
                    rows=identities.get(str(ad['id']),[])
                    identity=material_identity(rows,account_sources,scope,creative_id=object_id(ad))
                    decision=evaluate_ad(ad,identity,evidence)
                    if identity.get('root_id'):account_products[identity['root_id']]+=1
                    if not decision['eligible']:
                        account_skip[decision['reason']]+=1
                        other_product=(identity.get('reason')=='product_out_of_scope' and rows
                            and all(str(r.get('product','')) in known_products and str(r.get('product','')) not in scope for r in rows))
                        if (identity.get('reason') and not other_product) or decision['reason']=='review_feedback_unreadable':
                            account_gaps[decision['reason']]+=1
                        continue
                    # Never rotate credentials to force a mutation. Select from
                    # the exact publishing queue/product lineage before freezing.
                    matched=[r for r in rows if str(r.get('creative_id',''))==object_id(ad) and str(r.get('product','')) in scope]
                    routed.append((ad,identity,decision,matched))
                batch_method=getattr(self.source,'selected_credentials',None)
                batch_routes=None
                if callable(batch_method) and routed:
                    route_rows={str(r['id']):r for _,_,_,matched in routed for r in matched}
                    batch_routes=batch_method(list(route_rows.values()))
                for ad,identity,decision,matched in routed:
                    try:
                        if batch_routes is None:
                            users={numeric_id(self.source.selected_credential(r)) for r in matched}
                        else:
                            selected=[batch_routes.get(str(r['id']),{}) for r in matched]
                            if any(r.get('error') or not r.get('user_id') for r in selected):
                                raise ValueError('credential_lineage_unavailable')
                            users={numeric_id(r['user_id']) for r in selected}
                    except Exception as exc:
                        if ledger_error(exc):raise
                        account_skip['credential_lineage_unavailable']+=1;account_gaps['credential_lineage_unavailable']+=1
                        self.runtime.event(run_id,'ad_skipped',{'ad_id':str(ad['id']),'reason':'credential_lineage_unavailable','error':safe_error(exc)})
                        continue
                    if len(users)!=1:
                        account_skip['ambiguous_credential_lineage']+=1;account_gaps['ambiguous_credential_lineage']+=1;continue
                    user=next(iter(users))
                    if user not in self.graph.tokens:
                        account_skip['selected_credential_unavailable']+=1;account_gaps['selected_credential_unavailable']+=1;continue
                    eligible.append(dict(decision,account_id=str(account),user_id=user,identity=identity,ad_snapshot=ad))
                # Publish an account only after ALL pagination AND SQL lookups
                # complete. Failed pages never contribute deletion candidates.
                self.runtime.inventory(run_id,str(account),ads)
                skip.update(account_skip);product_counts.update(account_products);mapping_gaps.update(account_gaps)
                candidates.extend(eligible)
                if account_gaps:errors.append({'account_id':account,'stage':'mapping_coverage','gap_counts':dict(account_gaps)})
                account_results.append({'account_id':account,'status':'complete','ads':len(ads),'candidates':len(eligible),'read_user_id':read_user,'mapping_gap_counts':dict(account_gaps)})
            except Exception as exc:
                errors.append({'account_id':account,'stage':'lineage','error':safe_error(exc)})
                account_results.append({'account_id':account,'status':'failed','error':safe_error(exc)})
            self.runtime.update(run_id,stage='inventory',accounts_total=len(accounts),accounts_finished=index+1,candidate_ads=len(candidates),coverage_errors=len(errors))
            self.runtime.event(run_id,'account_scan',account_results[-1])
        if seen_accounts!=set(accounts):raise ValueError('account_inventory_incomplete')
        # Include even out-of-product shared Ad references as impact evidence;
        # those IDs are NOT inserted into the deletion objects list.
        candidates.sort(key=lambda c:(int(c['account_id']),int(c['ad_id'])))
        by_creative=defaultdict(list)
        for candidate in candidates:by_creative[candidate['creative_id']].append(candidate['ad_id'])
        shared={}
        for cid,targets in by_creative.items():
            refs=self.runtime.references(run_id,cid)
            extra=[r for r in refs if str(r['id']) not in targets]
            if extra:
                ref_lineage={}
                for part in chunks([str(r['id']) for r in extra]):ref_lineage.update(self.source.lineages(part))
                shared[cid]=[{'ad_id':str(r['id']),'account_id':str(r.get('account_id','')),
                    'product_ids':sorted({str(a.get('product')) for a in ref_lineage.get(str(r['id']),[])
                        if str(a.get('creative_id',''))==cid and str(a.get('product','')).isdigit()})} for r in extra]
        violation_ids=defaultdict(list)
        for row in policy_rows:violation_ids[str(row['ad_id'])].append(str(row['id']))
        manifest={'version':1,'policy_version':'20261023-v1','run_id':run_id,'window_start':start,'window_end':end,'frozen_at':now(),
            'root_products':['1479','3302'],'scope':scope,'candidates':candidates,'evidence':evidence,
            'violation_ids_by_ad':{adid:sorted(set(ids)) for adid,ids in violation_ids.items()},
            'coverage':{'accounts_total':len(accounts),'accounts':account_results,'errors':errors},
            'shared_creative_impacts':shared,'shared_reference_coverage':'complete' if not errors else 'partial',
            'violation_excluded_reason_counts':dict(Counter(r['reason'] for r in evidence['excluded'])),
            'qualified_violation_ads':len({adid for v in evidence['materials'].values() for adid in v['violating_ad_ids']}),
            'skip_counts':dict(skip),'mapping_gap_counts':dict(mapping_gaps),'product_scanned_ads':dict(product_counts)}
        manifest['sha256']=digest(manifest)
        self.runtime.artifact(run_id,'manifest.json',manifest)
        self.runtime.update(run_id,stage='frozen',manifest_sha256=manifest['sha256'],candidate_ads=len(candidates),
            candidate_creatives=len(by_creative),coverage_errors=len(errors),material_count=len(evidence['materials']),
            rule1_ads=sum('rule1' in c['rules'] for c in candidates),rule2_ads=sum('rule2' in c['rules'] for c in candidates),
            product_candidate_ads=dict(Counter(c['root_id'] for c in candidates)),shared_creatives=len(shared),skip_counts=dict(skip),
            mapping_gap_counts=dict(mapping_gaps),product_scanned_ads=dict(product_counts))
        return manifest

    def preflight(self, candidate, manifest):
        # Re-read a sufficient set of trigger evidence; never authorize from
        # stale counters alone. Four distinct Ads suffice for rule 2.
        proof_ids=set()
        for mid in candidate['material_ids']:
            ev=manifest['evidence']['materials'].get(mid,{})
            for adid in ev.get('violating_ad_ids',[])[:4]:
                vids=manifest['violation_ids_by_ad'].get(str(adid),[])
                if isinstance(vids,str):vids=[vids]
                proof_ids.update(vids)
        # All SQL reads share one FIFO snapshot; release it before Graph I/O.
        with self.sql_snapshot():
            scope=self.current_scope()
            proofs=self.source.violations_by_ids(sorted(proof_ids))
            proof_lineages,proof_sources=self.lineage_data([str(r['ad_id']) for r in proofs],proofs)
            evidence=build_evidence(proofs,proof_lineages,proof_sources,scope,manifest['window_start'],manifest['window_end'])
            rows,sources=self.lineage_data([candidate['ad_id']])
            matching=[r for r in rows.get(candidate['ad_id'],[]) if str(r.get('creative_id',''))==candidate['creative_id'] and str(r.get('product','')) in scope]
            users={numeric_id(self.source.selected_credential(r)) for r in matching}
        if users!={candidate['user_id']}:
            return {'eligible':False,'reason':'credential_lineage_changed'}
        ad=self.execution_graph.get_ad(candidate['ad_id'],candidate['user_id'])
        if str(ad.get('account_id','')).removeprefix('act_') != candidate['account_id'].removeprefix('act_') or object_id(ad)!=candidate['creative_id']:
            return {'eligible':False,'reason':'account_or_creative_changed'}
        identity=material_identity(rows[candidate['ad_id']],sources,scope,creative_id=candidate['creative_id'])
        decision=evaluate_ad(ad,identity,evidence)
        if decision['eligible'] and (decision['root_id']!=candidate['root_id'] or set(decision['material_ids'])!=set(candidate['material_ids'])
                or set(identity['product_ids'])!=set(candidate['identity']['product_ids'])):
            return {'eligible':False,'reason':'frozen_material_or_product_changed'}
        return decision

    def attempt(self, job_id, shared_run, kind, identifier, candidate, run_id=None, lease=None):
        # Yield for the legacy job before acquiring an object claim. A wait or
        # stop here has not issued a DELETE and must not create an unknown fence.
        self.check_stop()
        if lease is not None:lease.check()
        key=kind+':'+identifier
        claim=self.store.claim_object(job_id,key,shared_run)
        if not claim.get('claimed'):
            return {'status':claim['status'],'reason':claim.get('reason','existing_receipt_or_fence'),
                'request_sent':False,'prior_result':claim.get('result',{})}
        uid=numeric_id(candidate['user_id']);account=numeric_id(candidate['account_id'])
        context={'credential_kind':'user','credential_user_id':uid,'credential_relation':'frozen_publish_queue'}
        self.store.record_object_credential(job_id,key,shared_run,context)
        result=None;request_started=False
        try:
            self.check_stop()
            # This write is a real durable health check immediately before the
            # destructive boundary, not a filesystem permission assumption.
            self.runtime.write_barrier(run_id)
            self.check_stop()
            request_started=True
            result=self.execution_graph.delete(kind,identifier,uid,account)
            result=dict(result,**context,account_id=account)
            status=result['status']
            result['delete_status']=status
            result['api_accepted']=status=='api_accepted'
            if status in ('api_accepted','unknown'):
                verification=self.execution_graph.reconcile(kind,identifier,uid,account)
                result=dict(result,verification=verification)
                if verification.get('status')=='deleted_verified':
                    result.update(status='deleted_verified',confirmed_deleted=True)
                else:
                    result['status']='unknown'
            ledger_status='deleted' if result['status']=='deleted_verified' else ('failed' if result['status']=='failed' else 'unknown')
            self.store.finish_object(job_id,key,shared_run,ledger_status,result)
            return result
        except BaseException as exc:
            # A broken ledger must terminate all new writes. The already
            # committed in-progress claim is fenced by recovery/finish_run.
            if ledger_error(exc):raise
            if not request_started:
                failed=dict(context,status='failed',account_id=account,request_sent=False,
                    code='cancelled_before_delete',error=safe_error(exc))
                self.store.finish_object(job_id,key,shared_run,'failed',failed)
                raise
            unknown=dict(context,status='unknown',account_id=account,error=safe_error(exc),
                prior_result=result,requires_reconciliation=True,
                api_accepted=bool(result and result.get('api_accepted')))
            self.store.finish_object(job_id,key,shared_run,'unknown',unknown)
            if isinstance(exc,(KeyboardInterrupt,SystemExit,InterruptedError)):raise
            return unknown

    def dependency_guard(self,candidate,decision):
        """Recheck identity and pause eligibility after our Creative mutation.

        Do not reinterpret feedback created by our own Creative deletion as a
        new content-policy decision. Group preflight owns that review decision.
        A concurrent Creative/product/material change still cancels the Ad.
        """
        with self.sql_snapshot():
            scope=self.current_scope()
            rows,sources=self.lineage_data([candidate['ad_id']])
            current=rows.get(candidate['ad_id'],[])
            identity=material_identity(current,sources,scope,creative_id=candidate['creative_id'])
            matched=[r for r in current if str(r.get('creative_id',''))==candidate['creative_id'] and str(r.get('product','')) in scope]
            users={numeric_id(self.source.selected_credential(r)) for r in matched}
        if identity.get('reason') or identity['root_id']!=candidate['root_id'] or set(identity['material_ids'])!=set(candidate['material_ids']) or set(identity['product_ids'])!=set(candidate['identity']['product_ids']):
            return 'dependency_identity_changed'
        if users!={candidate['user_id']}:
            return 'dependency_credential_changed'
        ad=self.execution_graph.get_ad(candidate['ad_id'],candidate['user_id'])
        if str(ad.get('id'))!=candidate['ad_id'] or str(ad.get('account_id','')).removeprefix('act_')!=candidate['account_id'] or object_id(ad)!=candidate['creative_id']:
            return 'dependency_ad_or_creative_changed'
        statuses=[ad.get('status'),(ad.get('adset') or {}).get('status'),(ad.get('campaign') or {}).get('status')]
        if any(s not in ('ACTIVE','PAUSED') for s in statuses):return 'dependency_status_changed'
        if 'rule2' not in decision['rules'] and 'PAUSED' not in statuses:return 'dependency_ad_resumed'
        return ''

    def execute(self, run_id, manifest, max_ads=None, start_phase='start'):
        plain={k:v for k,v in manifest.items() if k!='sha256'}
        if digest(plain)!=manifest['sha256']:raise ValueError('manifest_hash_mismatch')
        if manifest.get('run_id')!=run_id:raise ValueError('manifest_run_mismatch')
        if max_ads is not None and (isinstance(max_ads,bool) or not isinstance(max_ads,int) or max_ads<1):
            raise ValueError('max_ads_must_be_positive')
        notice=self.runtime.notice(run_id,start_phase)
        if not notice or notice['status']!='sent':raise RuntimeError('start_notification_not_confirmed')
        all_groups=defaultdict(list);seen=set()
        complete_accounts={str(a['account_id']) for a in manifest['coverage']['accounts'] if a['status']=='complete'}
        for c in manifest['candidates']:
            for name in ('ad_id','creative_id','account_id','user_id'):numeric_id(c[name])
            if c['ad_id'] in seen:raise ValueError('duplicate_candidate_ad')
            seen.add(c['ad_id'])
            if c['account_id'] not in complete_accounts:raise ValueError('candidate_account_not_complete')
            if c['root_id'] not in ('1479','3302'):raise ValueError('candidate_product_out_of_scope')
            all_groups[c['creative_id']].append(c)
        objects=[]
        for cid,items in all_groups.items():
            first=items[0]
            if any(c['account_id']!=first['account_id'] for c in items):raise ValueError('creative_account_conflict')
            common={'account_ids':[first['account_id']],'user_ids':sorted({c['user_id'] for c in items}),'product_ids':sorted({p for x in items for p in x['identity']['product_ids']})}
            objects.append(dict(common,kind='creative',object_id=cid,shared_impact=manifest['shared_creative_impacts'].get(cid,[])))
            for c in items:
                objects.append(dict(common,kind='ad',object_id=c['ad_id'],creative_ids=[cid],material_ids=c['material_ids'],rules=c['rules'],user_ids=[c['user_id']]))
        job_id='policy-cleanup-'+run_id
        try:job=self.store.get_job(job_id)
        except Exception as exc:
            if getattr(exc,'code',None)!='not_found':raise
            job=self.store.create_job({'job_id':job_id,'preview_id':manifest['sha256'],'actor':ACTOR,'actor_name':'Meta违规素材定时清理',
                'status':'ready','objects':objects,'job_source':'scheduled_violation_cleanup','policy_version':manifest['policy_version'],
                'manifest_sha256':manifest['sha256'],'window_start':manifest['window_start'],'window_end':manifest['window_end'],
                'products':[{'id':r,'name':n,'kind':'root','parent_id':r,'parent_name':n} for r,n in [('1479','DramaWave'),('3302','HotDrama')]]})
        if job.get('preview_id')!=manifest['sha256'] or job.get('actor')!=ACTOR:
            raise ValueError('existing_job_manifest_mismatch')
        old={o['key']:o for o in job['objects']}
        if set(old)!={o['kind']+':'+o['object_id'] for o in objects}:
            raise ValueError('existing_job_object_scope_mismatch')
        # A canary uses whole Creative dependency groups. The full manifest is
        # frozen in the job from the outset, so a later invocation can process
        # untouched groups without resetting receipts, failures, or fences.
        groups={};selected=0;limited=0
        for cid,items in all_groups.items():
            pending=[c for c in items if old['ad:'+c['ad_id']]['status']=='pending']
            if not pending:continue
            if max_ads is not None and selected+len(pending)>max_ads:
                limited+=len(pending);continue
            groups[cid]=pending;selected+=len(pending)
        run=self.store.claim_run(job_id,manifest['sha256'],ACTOR,['creative','ad'],job_id+'-'+uuid.uuid4().hex)
        shared_run=run['run_id']
        counts=Counter();results=[];final='completed'
        isolated_graph=None
        try:
            if isinstance(self.graph,GraphClient):
                # Do not alter the scan client's retries or any inventory
                # worker's session. A separate short, non-retrying read client
                # bounds lock-held mutation/preflight work near 10:00.
                isolated_graph=GraphClient(self.graph.tokens,version=self.graph.version,
                    timeout=min(self.graph.timeout,20),get_retries=0)
                isolated_graph.delete_stop=self.stop
                self.execution_graph=isolated_graph
            self.runtime.update(run_id,stage='executing',shared_job_id=job_id,shared_run_id=shared_run,canary_deferred_ads=limited)
            for cid,items in groups.items():
                self.check_stop()
                while True:
                    try:
                        with legacy_delete_lock(self.config['legacy_lock'],self.stop) as lease:
                            admitted=[];decisions={};group_skipped=0
                            for c in items:
                                self.check_stop()
                                if lease is not None:lease.check()
                                try:decision=self.preflight(c,manifest)
                                except Exception as exc:
                                    if ledger_error(exc):raise
                                    decision={'eligible':False,'reason':'preflight_failed','error':safe_error(exc)}
                                if lease is not None:lease.check()
                                self.runtime.event(run_id,'preflight',{'ad_id':c['ad_id'],'decision':decision})
                                if decision['eligible']:admitted.append(c);decisions[c['ad_id']]=decision
                                else:group_skipped+=1
                            if admitted:
                                previous=old['creative:'+cid]
                                if previous['status']=='pending':
                                    result=self.attempt(job_id,shared_run,'creative',cid,admitted[0],run_id,lease)
                                else:
                                    result={'status':previous['status'],'request_sent':False,'reason':'previous_attempt_preserved','prior_result':previous.get('result',{})}
                        break
                    except InterruptedError as exc:
                        if str(exc)!='legacy_priority_window':raise
                        # Exiting the context releases the lock. Reacquisition
                        # waits for the old task and repeats qualification.
                        self.runtime.event(run_id,'legacy_priority_yield',{'creative_id':cid,'stage':'creative'})
                counts['skipped_ads']+=group_skipped
                if not admitted:continue
                results.append({'kind':'creative','id':cid,'result':result});counts['creative_'+result['status']]+=1
                if result.get('api_accepted'):counts['creative_api_accepted']+=1
                self.runtime.event(run_id,'delete_result',results[-1])
                # Each Ad independently reacquires the legacy lock and checks
                # current identity/pause after any wait. Never hold one large
                # Creative group's lock across the old task's start window.
                for c in admitted:
                    while True:
                        self.check_stop()
                        try:
                            with legacy_delete_lock(self.config['legacy_lock'],self.stop) as lease:
                                if lease is not None:lease.check()
                                try:guard=self.dependency_guard(c,decisions[c['ad_id']])
                                except Exception as exc:
                                    if ledger_error(exc):raise
                                    guard='dependency_recheck_failed'
                                if lease is not None:lease.check()
                                if not guard:result=self.attempt(job_id,shared_run,'ad',c['ad_id'],c,run_id,lease)
                            break
                        except InterruptedError as exc:
                            if str(exc)!='legacy_priority_window':raise
                            self.runtime.event(run_id,'legacy_priority_yield',{'ad_id':c['ad_id'],'stage':'ad'})
                    if guard:
                        counts['skipped_ads']+=1
                        self.runtime.event(run_id,'ad_skipped',{'ad_id':c['ad_id'],'reason':guard})
                        continue
                    results.append({'kind':'ad','id':c['ad_id'],'result':result});counts['ad_'+result['status']]+=1
                    if result.get('api_accepted'):counts['ad_api_accepted']+=1
                    self.runtime.event(run_id,'delete_result',results[-1])
                self.runtime.update(run_id,result_counts=dict(counts))
            if manifest['coverage']['errors'] or counts['skipped_ads'] or any(k.endswith(('failed','unknown','pending')) for k in counts):final='partial'
            if limited:final='partial'
        except BaseException:
            final='interrupted'
            raise
        finally:
            self.execution_graph=self.graph
            if isolated_graph is not None:isolated_graph.close()
            # Fence in-flight claims before writing optional reporting files.
            # If this commit fails, propagate and let startup recovery fence
            # the durable in-progress records; never proceed to another write.
            finished=self.store.finish_run(shared_run,final,dict(counts))
            final=finished['status']
            current=self.store.get_job(job_id)
            remaining=sum(o['kind']=='ad' and o['status']=='pending' for o in current['objects'])
            unresolved=sum(o['kind']=='ad' and o['status'] not in ('deleted','already_deleted') for o in current['objects'])
            self.runtime.artifact(run_id,'execution-results-'+shared_run+'.json',results)
            self.runtime.artifact(run_id,'execution-results.json',results)
            self.runtime.update(run_id,stage='execution_finished',execution_status=final,result_counts=dict(counts),remaining_ads=remaining,unresolved_ads=unresolved)
        return final

    def start_text(self, run_id, start, end):
        return ('Meta违规素材清理｜开始\n运行：%s\n启动：%s\n产品：DramaWave、HotDrama（含W2A）\n窗口：%s 至 %s（北京时间）\n'
                '规则1：指定原因≥1次，清理当前未违规的已关闭广告（含上层暂停）。\n'
                '规则2：同素材≥4个不同违规广告，清理其全部当前未违规广告。\n'
                '原因：成人裸露及性行为、成人性招揽及露骨性语言、成人性剥削、人口剥削、垃圾内容、暴力与血腥内容、毒品与药品。\n'
                '先删Creative再删Ad；共享Creative亦删除。')%(run_id,now(),start,end)

    def end_text(self, run_id):
        run=self.runtime.get(run_id);d=run['data']
        try:manifest=self.runtime.load_artifact(run_id,'manifest.json')
        except ValueError:manifest={}
        labels={'completed':'完成','partial':'部分完成','interrupted':'中断','failed':'失败','previewed':'预演完成','running':'执行中'}
        started=d.get('execution_started_at') or run['started']
        ended=run['ended'] or now()
        try:seconds=max(0,int((datetime.fromisoformat(ended)-datetime.fromisoformat(started)).total_seconds()))
        except (TypeError,ValueError):seconds=0
        lines=['Meta违规素材清理｜结束','运行：'+run_id,'状态：'+labels.get(run['status'],run['status']),
            '窗口：%s 至 %s（北京时间）'%(d.get('window_start',''),d.get('window_end','')),
            '账户：%s/%s；覆盖异常：%s'%(d.get('accounts_finished',0),d.get('accounts_total',0),d.get('coverage_errors',0)),
            '30天命中素材：%s；候选广告去重：%s'%(d.get('material_count',0),d.get('candidate_ads',0)),
            '规则1：%s；规则2：%s（可重叠）'%(d.get('rule1_ads',0),d.get('rule2_ads',0))]
        candidates=manifest.get('candidates',[])
        for root,name in [('1479','DramaWave'),('3302','HotDrama')]:
            selected=[c for c in candidates if c['root_id']==root]
            lines.append('%s：已识别广告%s，候选%s（规则1=%s，规则2=%s）'%(name,
                d.get('product_scanned_ads',{}).get(root,0),len(selected),
                sum('rule1' in c['rules'] for c in selected),sum('rule2' in c['rules'] for c in selected)))
        counts=d.get('result_counts',{})
        for kind,label in [('creative','Creative'),('ad','Ad')]:
            lines.append('本次%s：API接受%s，回读确认删除%s，明确失败%s，未知%s，既有成功回执%s'%(label,
                counts.get(kind+'_api_accepted',0),counts.get(kind+'_deleted_verified',0),counts.get(kind+'_failed',0),
                counts.get(kind+'_unknown',0),counts.get(kind+'_already_deleted',0)+counts.get(kind+'_deleted',0)))
        impacts=manifest.get('shared_creative_impacts',{})
        lines.append('共享Creative：%s；引用覆盖：%s'%(len(impacts),manifest.get('shared_reference_coverage','未完成')))
        for cid,refs in impacts.items():
            lines.append('Creative '+cid+' 的其他引用（这些Ad不因共享关系删除）：')
            for ref in refs:
                products=ref.get('product_ids') or []
                outside=any(pid not in manifest.get('scope',{}) for pid in products)
                lines.append('Ad %s｜产品 %s%s'%(ref['ad_id'],','.join(products) or '未识别','（包含范围外产品）' if outside else ''))
        lines.extend(['跳过原因：'+json.dumps(d.get('skip_counts',{}),ensure_ascii=False),
            '执行时资格变化/跳过：%s'%counts.get('skipped_ads',0),
            '剩余待处理：%s；未解决（含失败及未知）：%s'%(d.get('remaining_ads',0),d.get('unresolved_ads',0)),
            '本次启动：%s；结束：%s；耗时：%s分%s秒'%(started,ended,seconds//60,seconds%60),
            '完整证据：/mnt/data-disk/meta-violation-material-cleanup/runs/'+run_id+'/'])
        return '\n'.join(lines)
