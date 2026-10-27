"""V3: eligible Ads, complete reference proof, eligible shared Ads, Creatives."""
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timedelta
import time
import uuid

from .policy import POLICY_VERSION, POLICY_CONTRACT, evaluate_ad, material_identity
from .runtime import digest, now
from .skip_policy import SkipPolicy
from .dependency_store import append_shared_ad
from .concurrent_execution import ConcurrentExecutor
from .reference_scan import scan_accounts, read_known_ads
from .backlog_policy import read_deleted_ad_proof, current_account_matches


def _global_state(store, kind, identifier):
    with store._transaction(False) as conn:
        key = kind + ':' + identifier
        if conn.execute('SELECT 1 FROM fb_asset_delete_v2_receipts WHERE object_key=?', (key,)).fetchone():
            return 'deleted'
        row = conn.execute('SELECT status FROM fb_asset_delete_v2_object_locks WHERE object_key=?', (key,)).fetchone()
        return row['status'] if row else None


def _reference(runner, executor, manifest, basis, ad, context):
    """Owner-thread decision; a changed binding clears only that old reference."""
    aid, cid, account = str(ad.get('id', '')), basis['creative_id'], basis['account_id']
    if str(ad.get('account_id')) != account:
        return None, 'reference_account_changed'
    global_state = _global_state(runner.store, 'ad', aid)
    if global_state in ('unknown', 'in_progress'):
        return None, 'unknown_fence'
    if ad.get('status') == 'DELETED':
        return None, ''
    if str((ad.get('creative') or {}).get('id')) != cid:
        return None, ''
    if global_state == 'deleted':
        return None, 'reference_receipt_conflict'
    if not current_account_matches(context, dict(basis, ad_id=aid)):
        return None, 'current_account_changed'
    identity = material_identity(context.get('rows', []), context.get('sources', {}),
                                 context.get('scope', {}), cid)
    decision = evaluate_ad(ad, identity, manifest['evidence'])
    if context.get('error') or set(map(str, context.get('credential_users', []))) != {basis['user_id']}:
        return None, 'reference_identity_unavailable'
    if not decision['eligible']:
        return None, decision['reason']
    candidate = dict(decision, identity=identity, account_id=account, user_id=basis['user_id'],
                     ad_snapshot=deepcopy(ad), shared_dependency=True)
    blocked = runner.skip_policy.result('ad', candidate)
    if blocked:
        return None, blocked['reason']
    return candidate, ''


def execute(runner, run_id, manifest, max_ads=None, start_phase='start'):
    from .runner import ACTOR, numeric_id, ledger_error, safe_error
    from .phased_execution import basis_valid, object_state
    if digest({k: v for k, v in manifest.items() if k != 'sha256'}) != manifest.get('sha256'):
        raise ValueError('manifest_hash_mismatch')
    if (manifest.get('run_id') != run_id or manifest.get('policy_version') != POLICY_VERSION
            or manifest.get('selection_policy') != POLICY_CONTRACT or manifest.get('comparison_only')):
        raise ValueError('execution_manifest_mismatch')
    if (datetime.fromisoformat(manifest['window_end']) - datetime.fromisoformat(manifest['window_start'])) != timedelta(days=3):
        raise ValueError('execution_window_mismatch')
    if max_ads is not None and (type(max_ads) is not int or max_ads < 1):
        raise ValueError('max_ads_must_be_positive')
    if (runner.runtime.notice(run_id, start_phase) or {}).get('status') != 'sent':
        raise RuntimeError('start_notification_not_confirmed')
    if manifest.get('blocked_account_creatives'):
        raise ValueError('disabled_account_probe_forbidden')
    runner.skip_policy = SkipPolicy.from_store(runner.store)
    groups, entries, seen, objects = defaultdict(list), {}, set(), []
    coverage = {str(a['account_id']): a.get('status') for a in manifest.get('coverage', {}).get('accounts', [])}
    for entry in manifest.get('recovery_creatives', []):
        cid = entry['basis']['creative_id']
        if cid in entries:
            raise ValueError('duplicate_recovery_creative')
        entries[cid] = deepcopy(entry)
        entries[cid]['execution_evidence'] = deepcopy(manifest['evidence'])
    for candidate in manifest['candidates']:
        for key in ('ad_id', 'creative_id', 'account_id', 'user_id'):
            numeric_id(candidate[key])
        decision = evaluate_ad(candidate['ad_snapshot'], candidate['identity'], manifest['evidence'])
        if candidate['ad_id'] in seen or not decision['eligible']:
            raise ValueError('candidate_scope_conflict')
        if coverage.get(candidate['account_id']) != 'complete':
            raise ValueError('candidate_account_coverage_incomplete')
        seen.add(candidate['ad_id'])
        groups[candidate['creative_id']].append(candidate)
    for cid, items in groups.items():
        first = items[0]
        if any(c['account_id'] != first['account_id'] for c in items):
            raise ValueError('creative_account_conflict')
        entry = entries.setdefault(cid, {'origin_run_id': run_id, 'origin_manifest_sha256': manifest['sha256'],
                                         'evidence': deepcopy(manifest['evidence'])})
        entry.update(basis=deepcopy(first), execution_evidence=deepcopy(manifest['evidence']))
    for cid, entry in entries.items():
        basis = entry['basis']
        for key in ('ad_id', 'creative_id', 'account_id', 'user_id'):
            numeric_id(basis[key])
        if basis['root_id'] not in ('1479', '3302') or not entry.get('origin_manifest_sha256'):
            raise ValueError('recovery_scope_invalid')
        objects.append({'kind': 'creative', 'object_id': cid, 'account_ids': [basis['account_id']],
                        'user_ids': [basis['user_id']], 'product_ids': basis['identity']['product_ids']})
    for items in groups.values():
        for c in items:
            objects.append({'kind': 'ad', 'object_id': c['ad_id'], 'creative_ids': [c['creative_id']],
                'account_ids': [c['account_id']], 'user_ids': [c['user_id']], 'material_ids': c['material_ids'], 'rules': c['rules']})
    job_id = 'policy-cleanup-' + run_id
    try:
        job = runner.store.get_job(job_id)
    except Exception as exc:
        if getattr(exc, 'code', None) != 'not_found':
            raise
        job = runner.store.create_job({'job_id': job_id, 'preview_id': manifest['sha256'], 'actor': ACTOR,
            'status': 'ready', 'objects': objects, 'job_source': 'scheduled_violation_cleanup',
            'policy_version': POLICY_VERSION, 'manifest_sha256': manifest['sha256']})
    if job['preview_id'] != manifest['sha256'] or job['actor'] != ACTOR:
        raise ValueError('existing_job_manifest_mismatch')
    old = {obj['key']: obj for obj in job['objects']}
    if any(o['kind'] + ':' + o['object_id'] not in old for o in objects):
        raise ValueError('existing_job_scope_mismatch')
    chosen = set(entries)
    if max_ads is not None:
        chosen, used = set(), 0
        for cid, items in groups.items():
            count = sum(old['ad:' + c['ad_id']]['status'] == 'pending' for c in items)
            if count and used + count <= max_ads:
                chosen.add(cid); used += count
    shared = runner.store.claim_run(job_id, manifest['sha256'], ACTOR, ['ad', 'creative'], job_id + '-' + uuid.uuid4().hex)
    shared_run = shared['run_id']; counts = Counter(); results = []; deferred = {}; final = 'interrupted'
    def record(kind, identifier, result, stage):
        row = {'kind': kind, 'id': identifier, 'result': result, 'stage': stage}
        results.append(row); counts[kind + '_' + result['status']] += 1
        runner.runtime.event(run_id, 'delete_result', row)
        runner.runtime.update(run_id, result_counts=dict(counts))
    def defer(cid, reason, blocked=None):
        entry = entries[cid]; result = {'status': 'blocked', 'request_sent': False, 'reason': reason}
        deferred[cid] = result
        entry.update(last_checked_at=now(), reference_check={'policy_version': POLICY_VERSION,
            'checked_at': now(), 'reason': reason, 'complete': False})
        if blocked is not None:
            entry['blocked_ads'] = blocked
        runner.runtime.queue_creative(entry)
        state = object_state(runner.store, job_id, 'creative:' + cid)
        if state['status'] in ('pending', 'failed'):
            claim = runner.store.claim_object(job_id, 'creative:' + cid, shared_run)
            if claim.get('claimed'):
                runner.store.finish_object(job_id, 'creative:' + cid, shared_run, 'blocked', result)
        record('creative', cid, result, 'reference_hold')
    executor = ConcurrentExecutor(runner, run_id, manifest, job_id, shared_run, entries, groups, record, counts)
    try:
        held = manifest.get('recovery_assessment', {}).get('deferred_ids', {})
        for previous in runner.runtime.cleanup_backlog():
            cid = previous['basis']['creative_id']
            if cid in held:
                previous = deepcopy(previous)
                previous['requalification'] = {'policy_version': POLICY_VERSION, 'run_id': run_id,
                    'checked_at': manifest['frozen_at'], 'eligible': False, 'reason': held[cid]}
                runner.runtime.queue_creative(previous)
        runner.runtime.update(run_id, stage='deleting_ads', shared_job_id=job_id, shared_run_id=shared_run)
        pending = [c for cid, items in groups.items() if cid in chosen for c in items if old['ad:' + c['ad_id']]['status'] == 'pending']
        for candidate, result in executor.phase('ad', pending):
            record('ad', candidate['ad_id'], result, 'ads')

        runner.runtime.update(run_id, stage='shared_reference_scan')
        reference_started = time.monotonic(); scan_metrics = {}; skipped = 0; ready = {}; qualification = []
        for cid in sorted(chosen, key=int):
            runner.check_stop(); entry = entries[cid]; basis = entry['basis']
            if old['creative:' + cid]['status'] in ('failed', 'blocked'):
                defer(cid, 'previous_attempt_requires_fresh_run'); continue
            state = _global_state(runner.store, 'creative', cid)
            if state == 'deleted':
                runner.store.claim_object(job_id, 'creative:' + cid, shared_run)
                runner.runtime.complete_creative(cid)
                continue
            if state in ('unknown', 'in_progress'):
                defer(cid, 'creative_result_unknown'); continue
            if any(object_state(runner.store, job_id, 'ad:' + c['ad_id'])['status'] in ('unknown', 'in_progress') for c in groups.get(cid, [])):
                defer(cid, 'ad_result_unknown'); continue
            if runner.skip_policy.result('creative', basis):
                result = executor.claim('creative', basis)
                if result is None:
                    raise RuntimeError('skip_policy_claim_changed')
                deferred[cid] = result; record('creative', cid, result, 'account_skip'); continue
            qualification.append(basis)
        for offset in range(0, len(qualification), 64):
            part = qualification[offset:offset + 64]
            try:
                contexts = executor.contexts(part)
            except Exception as exc:
                if ledger_error(exc) or isinstance(exc, InterruptedError): raise
                contexts = {}
            futures = []
            for basis in part:
                proof = read_deleted_ad_proof(runner.store, basis['ad_id'], basis['account_id']) or {}
                futures.append((basis['creative_id'], executor.pool.submit(executor.preflight, 'creative', basis,
                    contexts.get((basis['ad_id'], basis['creative_id']), {'error': True}), proof)))
            for cid, future in futures:
                try:
                    decision = future.result()
                except Exception as exc:
                    if ledger_error(exc) or isinstance(exc, InterruptedError): raise
                    decision = {'eligible': False, 'reason': 'creative_basis_unavailable'}
                if decision['eligible']:
                    ready[cid] = entries[cid]
                else:
                    defer(cid, decision['reason'])

        known = defaultdict(dict)
        for cid, entry in ready.items():
            basis = entry['basis']; key = (basis['account_id'], basis['user_id'])
            for blocked in entry.get('blocked_ads', []):
                if blocked.get('ad_id'):
                    known[key].setdefault(str(blocked['ad_id']), set()).add(cid)
        known_results = {}
        for (account, uid), ids in known.items():
            known_results[(account, uid)] = read_known_ads(runner, account, uid, ids, executor.governor, executor.stopped)
        known_bases = {}
        for cid, entry in ready.items():
            basis = entry['basis']; observed = known_results.get((basis['account_id'], basis['user_id']), {})
            for item in entry.get('blocked_ads', []):
                aid = str(item.get('ad_id') or ''); ad = observed.get('records', {}).get(aid)
                c = dict(basis, ad_id=aid)
                if (ad and ad.get('status') != 'DELETED' and str((ad.get('creative') or {}).get('id')) == cid
                        and not runner.skip_policy.result('ad', c)):
                    known_bases[(aid, cid)] = c
        known_contexts = {}
        values = list(known_bases.values())
        for offset in range(0, len(values), 64):
            try:
                known_contexts.update(executor.contexts(values[offset:offset + 64]))
            except Exception as exc:
                if ledger_error(exc) or isinstance(exc, InterruptedError): raise
        for cid, entry in list(ready.items()):
            basis = entry['basis']; key = (basis['account_id'], basis['user_id']); blockers = []
            ids = [str(b['ad_id']) for b in entry.get('blocked_ads', []) if b.get('ad_id')]
            observed = known_results.get(key, {})
            for aid in ids:
                ad = observed.get('records', {}).get(aid)
                if _global_state(runner.store, 'ad', aid) in ('unknown', 'in_progress'):
                    reason = 'unknown_fence'
                elif ad is None:
                    reason = 'known_reference_unreadable'
                elif ad.get('status') == 'DELETED' or str((ad.get('creative') or {}).get('id')) != cid:
                    continue
                else:
                    try:
                        c = dict(basis, ad_id=aid)
                        policy_hold = runner.skip_policy.result('ad', c)
                        if policy_hold:
                            reason = policy_hold['reason']
                        else:
                            _, reason = _reference(runner, executor, manifest, basis, ad,
                                known_contexts.get((aid, cid), {'error': True}))
                    except Exception as exc:
                        if ledger_error(exc) or isinstance(exc, InterruptedError): raise
                        reason = 'reference_identity_unavailable'
                if reason:
                    blockers.append({'ad_id': aid, 'result': {'reason': reason, 'request_sent': False},
                                     'observed_ad': ad})
            if blockers:
                defer(cid, 'known_reference_blocked', blockers); ready.pop(cid); skipped += 1

        by_account = defaultdict(list)
        for cid, entry in ready.items():
            b = entry['basis']; by_account[(b['account_id'], b['user_id'])].append(cid)
        shared_candidates = []; shared_by_creative = defaultdict(list); approved = set()
        for key, scanned in scan_accounts(runner, by_account, executor.governor, executor.stopped, metrics=scan_metrics):
            for cid in by_account[key]:
                entry = entries[cid]; basis = entry['basis']; blockers = []; candidates = []
                if not scanned['complete']:
                    defer(cid, 'reference_scan_incomplete'); continue
                refs = scanned['by_creative'].get(cid, [])
                runner.runtime.event(run_id, 'shared_references', {'creative_id': cid, 'ads': refs})
                for offset in range(0, len(refs), 64):
                    part = refs[offset:offset + 64]
                    try:
                        contexts = executor.contexts([dict(basis, ad_id=str(ad['id'])) for ad in part])
                    except Exception as exc:
                        if ledger_error(exc) or isinstance(exc, InterruptedError): raise
                        contexts = {}
                    for ad in part:
                        aid = str(ad['id'])
                        c, reason = _reference(runner, executor, manifest, basis, ad, contexts.get((aid, cid), {'error': True}))
                        if reason:
                            blockers.append({'ad_id': aid, 'result': {'reason': reason, 'request_sent': False}, 'observed_ad': ad})
                        elif c:
                            candidates.append(c)
                if blockers:
                    defer(cid, 'reference_not_eligible', blockers); continue
                entry.update(blocked_ads=[], reference_check={'policy_version': POLICY_VERSION, 'complete': True,
                    'checked_at': now(), 'account_id': key[0], 'user_id': key[1], 'reference_ids': [str(a['id']) for a in refs]})
                runner.runtime.queue_creative(entry)
                approved.add(cid)
                for c in candidates:
                    append_shared_ad(runner.store, job_id, shared_run, cid, c['ad_snapshot'], c['user_id'], ACTOR, candidate=c)
                    if not any(old_c['ad_id'] == c['ad_id'] for old_c in groups[cid]):
                        groups[cid].append(c)
                    status = object_state(runner.store, job_id, 'ad:' + c['ad_id'])['status']
                    if status not in ('pending', 'deleted', 'already_deleted'):
                        approved.discard(cid)
                        defer(cid, 'shared_ad_previous_attempt', [{'ad_id': c['ad_id'], 'result': {'reason': 'shared_ad_previous_attempt', 'request_sent': False}}])
                    elif status == 'pending':
                        shared_candidates.append(c); shared_by_creative[cid].append(c)
        scan_metrics['account_scan_wall_seconds'] = scan_metrics.get('wall_seconds', 0)
        scan_metrics.update(wall_seconds=round(time.monotonic() - reference_started, 3), skipped_creative_scans=skipped)
        runner.runtime.update(run_id, reference_scan_metrics=scan_metrics, stage='deleting_shared_ads')
        shared_started = time.monotonic()
        unique = {c['ad_id']: c for c in shared_candidates}
        for c, result in executor.phase('ad', list(unique.values())):
            record('ad', c['ad_id'], result, 'shared_ads')
            if result['status'] not in ('deleted_verified', 'deleted', 'already_deleted'):
                cid = c['creative_id']; approved.discard(cid)
                defer(cid, 'shared_ad_unresolved', [{'ad_id': c['ad_id'], 'result': result}])
        for cid in list(approved):
            unresolved = [c for c in groups.get(cid, []) if object_state(runner.store, job_id, 'ad:' + c['ad_id'])['status'] not in ('deleted', 'already_deleted')]
            if unresolved:
                approved.discard(cid)
                defer(cid, 'ad_not_confirmed_deleted', [{'ad_id': c['ad_id'], 'result': {'reason': 'ad_not_confirmed_deleted', 'request_sent': False}} for c in unresolved])
        scan_metrics.update(scan_and_validation_seconds=scan_metrics['wall_seconds'],
            shared_ad_seconds=round(time.monotonic()-shared_started, 3),
            wall_seconds=round(time.monotonic()-reference_started, 3))
        runner.runtime.update(run_id, reference_scan_metrics=scan_metrics)
        runner.runtime.update(run_id, stage='deleting_creatives')
        for basis, result in executor.phase('creative', [entries[cid]['basis'] for cid in sorted(approved, key=int)]):
            cid = basis['creative_id']; record('creative', cid, result, 'creatives')
            if result['status'] in ('deleted_verified', 'deleted', 'already_deleted'):
                runner.runtime.complete_creative(cid)
            else:
                entries[cid].update(last_result=result, last_checked_at=now())
                runner.runtime.queue_creative(entries[cid]); deferred[cid] = result
        final = 'partial' if deferred or manifest.get('coverage', {}).get('errors') or chosen != set(entries) else 'completed'
    finally:
        executor.close()
        final = runner.store.finish_run(shared_run, final, dict(counts))['status']
        current = runner.store.get_job(job_id)
        runner.runtime.artifact(run_id, 'execution-results-' + shared_run + '.json', results)
        runner.runtime.artifact(run_id, 'execution-results.json', results)
        runner.runtime.artifact(run_id, 'deferred-cleanup.json', runner.runtime.cleanup_backlog())
        runner.runtime.update(run_id, execution_status=final, result_counts=dict(counts), stage='execution_finished',
            remaining_ads=sum(o['kind'] == 'ad' and o['status'] == 'pending' for o in current['objects']),
            deferred_creatives=len(runner.runtime.cleanup_backlog()))
    return final
