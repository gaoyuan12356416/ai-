"""Material-driven candidate discovery and exact-ID, read-only Meta validation."""
from collections import Counter, defaultdict
from datetime import datetime
import time

from .indexed_source import material_rows
from .policy import product_scope, qualifying_policies, build_evidence, material_identity, evaluate_ad
from .runtime import now, digest


def scan(runner, run_id, start, end):
    from .runner import chunks, numeric_id, object_id, safe_error, ledger_error
    began=time.monotonic(); timing={}; errors=[]; skips=Counter(); coverage={}
    progress=lambda **kw: runner.runtime.update(run_id,**kw)
    baseline_id=runner.config.get('comparison_baseline_run_id')
    baseline=None
    if baseline_id:
        baseline=runner.runtime.load_artifact(baseline_id,'manifest.json')
        if digest({k:v for k,v in baseline.items() if k!='sha256'})!=baseline['sha256']:
            raise ValueError('baseline_hash_mismatch')
        start,end=baseline['window_start'],baseline['window_end']
        scope=baseline['scope']; evidence=baseline['evidence']
        violation_ids=baseline['violation_ids_by_ad']
        progress(window_start=start,window_end=end,comparison_baseline_run_id=baseline_id)
    else:
        scope=product_scope(runner.source.products())
        violations=runner.source.violations(start,end)
        policy_rows=[r for r in violations if qualifying_policies(r.get('issues_info'))]
        lineage,sources=runner.lineage_data(sorted({str(r['ad_id']) for r in policy_rows}),policy_rows,scope=scope)
        evidence=build_evidence(policy_rows,lineage,sources,scope,start,end)
        violation_ids=defaultdict(list)
        for row in policy_rows: violation_ids[str(row['ad_id'])].append(str(row['id']))
        runner.runtime.artifact(run_id,'violation-evidence.json',
            {'rows':policy_rows,'evidence':evidence,'start':start,'end':end})
        progress(vio_rows=len(violations),policy_rows=len(policy_rows))
    timing['evidence_seconds']=round(time.monotonic()-began,3)
    progress(stage='material_lookup',material_count=len(evidence['materials']))
    phase=time.monotonic()
    raw,lookup=material_rows(runner.source,evidence['materials'],scope,progress,runner.stop)
    timing['reverse_lookup_seconds']=round(time.monotonic()-phase,3)
    ids=set()
    for row in raw:
        try: ids.add(numeric_id(row['ad_id']))
        except ValueError: skips['invalid_publication_ad_id']+=1
    del raw
    retrieval_ids=set(ids)
    comparison={}
    if baseline:
        old={c['ad_id']:c for c in baseline['candidates']}
        missing=sorted(set(old)-ids); unresolved=[]; changed=[]
        # Missing IDs are independently looked up by Ad index; a still-eligible
        # old snapshot MUST be retrieved by the new material path.
        for part in chunks(missing):
            rows,sources=runner.lineage_data(part,scope=scope)
            for aid in part:
                candidate=old[aid]
                identity=material_identity(rows.get(aid,[]),sources,scope,candidate['creative_id'])
                decision=evaluate_ad(candidate['ad_snapshot'],identity,evidence)
                (unresolved if decision['eligible'] else changed).append(aid)
        comparison={'baseline_run_id':baseline_id,'baseline_sha256':baseline['sha256'],
            'baseline_candidate_ads':len(old),'retrieved_old_candidates':len(set(old)&ids),
            'retrieval_missing_ids':missing,'source_changed_ids':changed,
            'unexplained_retrieval_missing_ids':unresolved,
            'legacy_scan_seconds':20507 if baseline_id=='preview-20261023-v5' else None,
            'same_frozen_evidence':True}
        runner.runtime.artifact(run_id,'comparison-retrieval.json',comparison)
        if unresolved: raise ValueError('material_lookup_lost_baseline_candidates')
    phase=time.monotonic(); targets=[]; identities={}; by_ad=defaultdict(list)
    material_set=set(evidence['materials'])
    # Read every current publication row for each retrieved Ad, so an older
    # matched row cannot override a later Creative/product/material binding.
    for part in chunks(sorted(ids)):
        runner.check_stop()
        with runner.sql_snapshot():
            rows,sources=runner.lineage_data(part,scope=scope)
            relevant=[r for values in rows.values() for r in values if str(r.get('product')) in scope]
            routes=runner.source.selected_credentials(relevant)
        for aid in part:
            all_rows=rows.get(aid,[])
            options=[]
            for cid in sorted({str(r.get('creative_id','')) for r in all_rows}):
                identity=material_identity(all_rows,sources,scope,cid)
                if identity['reason'] or not set(identity['material_ids'])&material_set: continue
                matching=[r for r in all_rows if str(r.get('creative_id'))==cid and str(r.get('product')) in scope]
                selected=[routes.get(str(r.get('id')), {}) for r in matching]
                users={r.get('user_id') for r in selected if not r.get('error') and r.get('user_id')}
                accounts={str(r.get('ad_account_id','')).removeprefix('act_') for r in matching}
                if len(users)!=1 or len(accounts)!=1 or any(r.get('error') or not r.get('user_id') for r in selected):
                    skips['credential_lineage_unavailable']+=1; continue
                user=next(iter(users)); account=next(iter(accounts))
                try: numeric_id(user); numeric_id(account); numeric_id(cid)
                except ValueError: skips['invalid_route_identity']+=1; continue
                if user not in runner.graph.tokens:
                    skips['selected_credential_unavailable']+=1; continue
                candidate={'ad_id':aid,'creative_id':cid,'account_id':account,'user_id':user,'identity':identity}
                identities[(aid,cid)]=candidate
                by_ad[aid].append(candidate)
                options.append(candidate)
            if options:
                # A read may use the selected identity of a historical binding;
                # its returned current Creative must have its OWN frozen route.
                targets.extend(options)
            else: skips['no_confirmed_material_route']+=1
        progress(stage='publication_context',target_ads=len(targets),processed_publication_ads=len(identities))
    timing['publication_context_seconds']=round(time.monotonic()-phase,3)
    phase=time.monotonic(); candidates=[]; blocked=[]; checked={}; account_diagnostics={}
    read_failures=defaultdict(list); read_success=set(); accepted_ids=set()
    for (user,account,requested),records,failures in runner.graph.iter_target_ads(targets,runner.stop):
        runner.check_stop()
        cov=coverage.setdefault(account,{'account_id':account,'status':'complete','ads':0,'candidates':0,'read_user_id':user})
        cov['ads']+=len(requested)
        for aid,error in failures.items():
            read_failures[aid].append((account,user,error))
            if aid not in read_success:checked[aid]={'reason':'meta_read_unavailable','error':error}
        accepted_records=[]
        for aid,ad in records.items():
            if aid in accepted_ids:continue
            read_success.add(aid)
            candidate=identities.get((aid,object_id(ad)))
            if not candidate:
                skips['current_creative_lineage_missing']+=1
                checked[aid]={'reason':'current_creative_lineage_missing'}; continue
            decision=evaluate_ad(ad,candidate['identity'],evidence)
            checked[aid]={'reason':decision['reason'],'status':ad.get('status'),
                          'effective_status':ad.get('effective_status'),'creative_id':object_id(ad)}
            if not decision['eligible']:
                skips[decision['reason']]+=1; continue
            candidates.append(dict(candidate,**{k:v for k,v in decision.items() if k not in candidate},ad_snapshot=ad))
            accepted_ids.add(aid)
            cov['candidates']+=1; accepted_records.append(ad)
        if accepted_records: runner.runtime.inventory(run_id,account,accepted_records)
        progress(stage='target_verification',target_ads=len(targets),target_ads_checked=len(checked),
            candidate_ads=len(candidates),coverage_errors=len(errors),
            graph_metrics=runner.graph.safe_metrics,sql_metrics=runner.source.safe_metrics)
    for aid,failed_routes in read_failures.items():
        if aid in read_success:continue
        for account,user,error in failed_routes:
            errors.append({'ad_id':aid,'account_id':account,'stage':'target_get','error':error})
            key=(account,user)
            if key not in account_diagnostics:
                try:account_diagnostics[key]=runner.graph.account_diagnostic(account,user)
                except Exception as exc:account_diagnostics[key]={'error':safe_error(exc)}
            diagnostic=account_diagnostics[key]
            if diagnostic.get('account_status')==2:
                for candidate in by_ad[aid]:
                    if candidate['account_id']!=account:continue
                    blocked.append(dict(candidate,root_id=candidate['identity']['root_id'],
                        material_ids=candidate['identity']['material_ids'],rules=['disabled_account_creative_only'],
                        account_diagnostic=diagnostic,ad_read_error=error))
    timing['meta_verification_seconds']=round(time.monotonic()-phase,3)
    candidates.sort(key=lambda c:(int(c['account_id']),int(c['ad_id'])))
    by_creative=defaultdict(list)
    for candidate in candidates: by_creative[candidate['creative_id']].append(candidate)
    # Preserve failed/deferred Creative work even if its last normal Ad has
    # already been deleted or its original violation has aged out of the window.
    backlog=getattr(runner.runtime,'cleanup_backlog',lambda:[])()
    timing['total_seconds']=round(time.monotonic()-began,3)
    manifest={'version':2,'policy_version':'20261024-v2','scanner':'material_indexed',
        'run_id':run_id,'window_start':start,'window_end':end,'frozen_at':now(),
        'root_products':['1479','3302'],'scope':scope,'candidates':candidates,'evidence':evidence,
        'violation_ids_by_ad':dict(violation_ids),'coverage':{'accounts_total':len(coverage),
            'accounts':list(coverage.values()),'errors':errors},'shared_creative_impacts':{},
        'shared_reference_coverage':'checked_on_dependency_failure',
        'blocked_account_creatives':blocked,'recovery_creatives':backlog,
        'qualified_violation_ads':len({a for e in evidence['materials'].values() for a in e['violating_ad_ids']}),
        'skip_counts':dict(skips),'mapping_gap_counts':{},'product_scanned_ads':{},
        'lookup_metrics':lookup,'timings':timing,'comparison_only':bool(baseline)}
    if baseline:
        new={c['ad_id'] for c in candidates}; old_ids=set(old)
        comparison.update(current_candidates=len(new),common_candidates=len(new&old_ids),
            new_candidate_count=len(new-old_ids))
        comparison['current_missing_old']=[dict(ad_id=aid,**checked.get(aid,{'reason':'publication_route_unavailable'}))
            for aid in sorted(old_ids-new)]
        comparison['timings']=timing
        runner.runtime.artifact(run_id,'comparison.json',comparison)
        manifest['comparison']=comparison
    manifest['sha256']=digest(manifest)
    runner.runtime.artifact(run_id,'manifest.json',manifest)
    progress(stage='frozen',manifest_sha256=manifest['sha256'],candidate_ads=len(candidates),
        candidate_creatives=len(by_creative),accounts_total=len(coverage),accounts_finished=len(coverage),
        coverage_errors=len(errors),material_count=len(evidence['materials']),
        product_candidate_ads=dict(Counter(c['root_id'] for c in candidates)),skip_counts=dict(skips),
        rule1_ads=sum('rule1' in c['rules'] for c in candidates),
        rule2_ads=sum('rule2' in c['rules'] for c in candidates),
        scan_timings=timing,lookup_metrics=lookup,blocked_account_creatives=len(blocked),
        recovery_creatives=len(backlog),remaining_ads=len(candidates))
    return manifest
