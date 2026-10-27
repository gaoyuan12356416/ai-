"""Frozen-scope scanner and serial Creative -> Ad executor."""
from collections import Counter, defaultdict
from contextlib import nullcontext
import re
import sqlite3
import time
import uuid
from .policy import product_scope, parse_ids, material_identity, qualifying_policies, build_evidence, evaluate_ad
from .runtime import digest, legacy_delete_lock, now
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

def chunks(values,size=1900):
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
        self.skip_policy=None

    def check_stop(self):
        if self.stop():raise InterruptedError('stop_requested')

    def sql_snapshot(self):
        factory=getattr(self.source,'snapshot',None)
        return factory() if callable(factory) else nullcontext()

    def lineage_data(self, ids, violation_rows=(), scope=None):
        data={};ids=list(ids)
        if not ids and not violation_rows:return {},{}
        with self.sql_snapshot():
            for part in chunks(ids):
                self.check_stop()
                data.update(self.source.lineages(part))
            material_rows=data if scope is None else {key:[row for row in rows if str(row.get('product','')) in scope] for key,rows in data.items()}
            keys=set(source_keys(material_rows))
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

    def account_batches(self, accounts):
        """Amortize FIFO setup without holding SQL open across Graph reads.

        Each account still finishes its entire Graph pagination before joining
        a batch. Individual lineage lookups remain capped at 1900 Ad IDs and
        every SQL page at 2000 rows. Batches target at most 20,000 inventory Ads
        across 32 accounts; an oversized complete account occupies one batch.
        """
        batch=[];total=0
        for item in self.account_inventories(accounts):
            count=len(item[1] or ())
            if batch and (len(batch)>=32 or total+count>20000):
                yield batch;batch=[];total=0
            batch.append(item);total+=count
            if len(batch)>=32 or total>=20000:
                yield batch;batch=[];total=0
        if batch:yield batch

    def scan(self, run_id, start, end):
        if self.config.get('scanner') == 'material_indexed':
            from .material_scan import scan
            return scan(self, run_id, start, end)
        settings=self.source.products();scope=product_scope(settings)
        known_products={str(row.get('id')) for row in settings
            if re.fullmatch(r'[1-9][0-9]{0,31}',str(row.get('id','')))}
        violations=self.source.violations(start,end)
        policy_rows=[r for r in violations if qualifying_policies(r.get('issues_info'))]
        lineage,sources=self.lineage_data(sorted({str(r['ad_id']) for r in policy_rows}),policy_rows,scope=scope)
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
        self.runtime.update(run_id,stage='inventory',accounts_total=len(accounts),accounts_finished=0,
            graph_metrics=getattr(self.graph,'safe_metrics',{}),sql_metrics=getattr(self.source,'safe_metrics',{}))
        index=-1
        for batch in self.account_batches(accounts):
            self.check_stop()
            batch_lineages=None;batch_sources={};cached_routes=None
            try:
                ids=[str(ad['id']) for _,ads,_,_ in batch for ad in (ads or ())]
                batch_method=getattr(self.source,'selected_credentials',None)
                # Keep only local parsing inside this FIFO lease. All Graph
                # pagination above has finished; no network wait holds SQL.
                with self.sql_snapshot() if ids else nullcontext():
                    batch_lineages,batch_sources=self.lineage_data(ids,scope=scope)
                    if callable(batch_method):
                        route_rows={}
                        for _,ads,_,_ in batch:
                            for ad in ads or ():
                                rows=batch_lineages.get(str(ad['id']),[])
                                identity=material_identity(rows,batch_sources,scope,creative_id=object_id(ad))
                                if evaluate_ad(ad,identity,evidence)['eligible']:
                                    route_rows.update({str(r['id']):r for r in rows
                                        if str(r.get('creative_id',''))==object_id(ad) and str(r.get('product','')) in scope})
                        cached_routes=batch_method(list(route_rows.values())) if route_rows else {}
            except Exception as exc:
                batch_lineages=None;batch_sources={};cached_routes=None
                if ledger_error(exc) or isinstance(exc,InterruptedError):raise
                # Retry read-only lookups per account to isolate a bad account.
                # No incomplete batch contributes deletion candidates.
                self.runtime.event(run_id,'lineage_batch_fallback',{'accounts':len(batch),'error':safe_error(exc)})
            for account,ads,read_user,account_errors in batch:
                index+=1
                self.check_stop();account=str(account)
                if account not in accounts or account in seen_accounts:raise ValueError('account_inventory_identity_conflict')
                seen_accounts.add(account)
                if ads is None:
                    errors.append({'account_id':account,'stage':'inventory','errors':account_errors})
                    account_results.append({'account_id':account,'status':'failed','errors':account_errors})
                    self.runtime.update(run_id,stage='inventory',accounts_total=len(accounts),accounts_finished=index+1,coverage_errors=len(errors))
                    continue
                try:
                    if batch_lineages is None:
                        identities,account_sources=self.lineage_data([str(a['id']) for a in ads],scope=scope)
                    else:
                        identities={str(a['id']):batch_lineages.get(str(a['id']),[]) for a in ads}
                        account_sources=batch_sources
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
                    batch_routes=cached_routes
                    if cached_routes is None and callable(batch_method) and routed:
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
            self.runtime.update(run_id,graph_metrics=getattr(self.graph,'safe_metrics',{}),
                sql_metrics=getattr(self.source,'safe_metrics',{}))
        if seen_accounts!=set(accounts):raise ValueError('account_inventory_incomplete')
        # Include even out-of-product shared Ad references as impact evidence;
        # those IDs are NOT inserted into the deletion objects list.
        candidates.sort(key=lambda c:(int(c['account_id']),int(c['ad_id'])))
        by_creative=defaultdict(list)
        for candidate in candidates:by_creative[candidate['creative_id']].append(candidate['ad_id'])
        shared={};references={};reference_ids=set();ref_lineage={}
        for cid,targets in by_creative.items():
            refs=self.runtime.references(run_id,cid)
            extra=[r for r in refs if str(r['id']) not in targets]
            if extra:
                references[cid]=extra;reference_ids.update(str(r['id']) for r in extra)
        # Shared-impact reporting is one bounded lookup, not one FIFO session
        # per Creative. It never adds these references to deletion candidates.
        if reference_ids:
            with self.sql_snapshot():
                for part in chunks(sorted(reference_ids)):
                    self.check_stop();ref_lineage.update(self.source.lineages(part))
        for cid,extra in references.items():
            shared[cid]=[{'ad_id':str(r['id']),'account_id':str(r.get('account_id','')),
                'product_ids':sorted({str(a.get('product')) for a in ref_lineage.get(str(r['id']),[])
                    if str(a.get('creative_id',''))==cid and str(a.get('product','')).isdigit()})} for r in extra]
        violation_ids=defaultdict(list)
        for row in policy_rows:violation_ids[str(row['ad_id'])].append(str(row['id']))
        from .policy import POLICY_VERSION, POLICY_CONTRACT
        manifest={'version':3,'policy_version':POLICY_VERSION,'selection_policy':dict(POLICY_CONTRACT),'run_id':run_id,'window_start':start,'window_end':end,'frozen_at':now(),
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

    def preflight(self, candidate, manifest, *, context=None, graph=None, ad=None):
        # execute() verifies the whole manifest SHA before any preflight. The
        # complete fixed-window proof is immutable for this run; refreshing
        # historical rows here would silently change its authorization basis.
        # Keep only this candidate's materials in per-Ad audit events.
        evidence={'materials':{mid:manifest['evidence']['materials'][mid]
            for mid in candidate['material_ids'] if mid in manifest['evidence']['materials']}}
        # Refresh current identity and credentials in one short FIFO snapshot,
        # then release it before Graph I/O. No historical proof/directory scan.
        if context is None:
            with self.sql_snapshot():
                context=self.source.current_ad_context(candidate['ad_id'],candidate['creative_id'])
        from .backlog_policy import current_account_matches
        if not current_account_matches(context,candidate):
            return {'eligible':False,'reason':'current_account_changed'}
        rows=context['rows'];sources=context['sources'];scope=context['scope']
        users={numeric_id(uid) for uid in context['credential_users']}
        if users!={candidate['user_id']}:
            return {'eligible':False,'reason':'credential_lineage_changed'}
        if ad is None:ad=(graph or self.execution_graph).get_ad(candidate['ad_id'],candidate['user_id'])
        if str(ad.get('account_id','')).removeprefix('act_') != candidate['account_id'].removeprefix('act_') or object_id(ad)!=candidate['creative_id']:
            return {'eligible':False,'reason':'account_or_creative_changed'}
        identity=material_identity(rows,sources,scope,creative_id=candidate['creative_id'])
        decision=evaluate_ad(ad,identity,evidence)
        if decision['eligible'] and (decision['root_id']!=candidate['root_id'] or set(decision['material_ids'])!=set(candidate['material_ids'])
                or set(identity['product_ids'])!=set(candidate['identity']['product_ids'])):
            return {'eligible':False,'reason':'frozen_material_or_product_changed'}
        if decision['eligible']:
            # Keep exactly the frozen trigger evidence for this candidate. The Ad
            # guard must re-evaluate its current review/status after Creative
            # deletion or a legacy-task wait instead of assuming it stayed clear.
            decision['checked_evidence']=evidence
        return decision

    def attempt(self, job_id, shared_run, kind, identifier, candidate, run_id=None, lease=None):
        # Yield for the legacy job before acquiring an object claim. A wait or
        # stop here has not issued a DELETE and must not create an unknown fence.
        self.check_stop()
        if lease is not None:lease.check()
        key=kind+':'+identifier
        claim=self.store.claim_object(job_id,key,shared_run)
        if not claim.get('claimed'):
            prior=claim.get('result',{})
            result={'status':claim['status'],'reason':claim.get('reason','existing_receipt_or_fence'),
                'request_sent':False,'prior_result':prior}
            if claim['status']=='blocked' and prior.get('policy_skip_reason'):
                result.update(policy_skip_reason=prior['policy_skip_reason'],policy_skip_source=prior.get('policy_skip_source'))
            return result
        uid=numeric_id(candidate['user_id']);account=numeric_id(candidate['account_id'])
        context={'credential_kind':'user','credential_user_id':uid,
            'credential_relation':('shared_creative_account' if candidate.get('shared_dependency') else 'frozen_publish_queue')}
        self.store.record_object_credential(job_id,key,shared_run,context)
        policy=self.skip_policy
        source={'job_id':job_id,'run_id':shared_run,'attempt_id':claim['attempt_id'],'object_key':key}
        skipped=policy.result(kind,candidate) if policy else None
        if skipped is not None:
            result=dict(skipped,**context,account_id=account)
            self.store.finish_object(job_id,key,shared_run,'blocked',result)
            return result
        result=None;request_started=False
        original_http=None
        try:
            self.check_stop()
            # This write is a real durable health check immediately before the
            # destructive boundary, not a filesystem permission assumption.
            self.runtime.write_barrier(run_id)
            self.check_stop()
            request_started=True
            http=getattr(self.execution_graph,'http',None)
            if policy and isinstance(self.execution_graph,GraphClient) and not hasattr(http,'write_check'):
                from .skip_policy import RequestBoundarySession
                original_http=http;http=RequestBoundarySession(http);self.execution_graph.http=http
            if hasattr(http,'write_check'):
                from .throttle import RequestNotSent
                http.account=account
                checked_at=getattr(self,'serial_context_checked_at',time.monotonic()) if kind=='creative' else time.monotonic()
                deadline=checked_at+30
                def write_check():
                    if policy:policy.check(kind,candidate)
                    if self.stop():raise RequestNotSent('cancelled_before_request')
                    if time.monotonic()>deadline:raise RequestNotSent('execution_context_expired')
                    try:
                        if lease is not None:lease.check()
                    except InterruptedError:raise RequestNotSent('legacy_priority_before_request') from None
                http.write_check=write_check
            if self.config.get('execution_order')=='ads_first' and kind=='ad' and isinstance(self.execution_graph,GraphClient):
                result=self.execution_graph.delete(kind,identifier,uid,account,expected_creative_id=candidate['creative_id'])
            else:
                result=self.execution_graph.delete(kind,identifier,uid,account)
            result=dict(result,**context,account_id=account)
            if policy:result=policy.outcome(kind,candidate,result,source)
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
            ledger_status='deleted' if result['status']=='deleted_verified' else (result['status'] if result['status'] in ('failed','blocked') else 'unknown')
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
        finally:
            if original_http is not None:self.execution_graph.http=original_http

    def dependency_guard(self,candidate,decision):
        """Recheck identity and pause eligibility after our Creative mutation.

        A Creative result is not proof of the cause of a later Ad issue. Until
        there is an explicitly verified platform error/field attribution, even
        WITH_ISSUES after confirmed Creative deletion stays ineligible. Never
        allow a new content rejection or pending review through this boundary.
        """
        with self.sql_snapshot():
            context=self.source.current_ad_context(candidate['ad_id'],candidate['creative_id'])
            identity=material_identity(context['rows'],context['sources'],context['scope'],creative_id=candidate['creative_id'])
            users={numeric_id(uid) for uid in context['credential_users']}
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
        current=evaluate_ad(ad,identity,decision.get('checked_evidence',{}))
        if not current['eligible']:return 'dependency_'+current['reason']
        return ''

    def execute(self, run_id, manifest, max_ads=None, start_phase='start'):
        # No config flag or legacy manifest may recover the former active-Ad rule.
        from .phased_execution import execute
        return execute(self, run_id, manifest, max_ads, start_phase)

    def start_text(self, run_id, start, end):
        from .reports import start_text
        return start_text(run_id, start, end)

    def end_text(self, run_id):
        from .reports import end_text
        return end_text(self, run_id)
