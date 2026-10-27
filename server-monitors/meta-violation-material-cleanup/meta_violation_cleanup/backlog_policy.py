"""Fresh v3 qualification of historical Creative work; preview never mutates it."""
from collections import Counter
from copy import deepcopy
import json

from .policy import evaluate_ad, material_identity, parse_ids, qualified_material_ids
from .current_contexts import current_contexts
from .runtime import now


def exact_deleted_proof(value, ad_id, account_id):
    return (isinstance(value, dict) and str(value.get('id')) == str(ad_id)
            and str(value.get('account_id')) == str(account_id)
            and value.get('status') == 'DELETED')


def current_account_matches(context, basis):
    """Require current SQL rows to bind this Ad/Creative to its exact account."""
    rows = [row for row in context.get('rows', [])
            if str(row.get('creative_id')) == str(basis.get('creative_id'))]
    account = str(basis.get('account_id') or '').removeprefix('act_')
    return bool(account and rows and all(
        str(row.get('ad_id')) == str(basis.get('ad_id'))
        and str(row.get('ad_account_id') or '').removeprefix('act_') == account
        for row in rows))


def read_deleted_ad_proof(store, ad_id, account_id):
    """Owner-thread receipt lookup. A bare success status is not a proof."""
    if store is None:
        return None
    with store._transaction(False) as conn:
        row = conn.execute('SELECT status,result FROM fb_asset_delete_v2_receipts WHERE object_key=?',
                           ('ad:' + str(ad_id),)).fetchone()
    if row is None or row['status'] not in ('deleted', 'already_deleted'):
        return None
    result = json.loads(row['result'])
    verification = result.get('verification') or {}
    for proof in (result.get('object'), result.get('proof'),
                  verification.get('object') if isinstance(verification, dict) else None):
        if exact_deleted_proof(proof, ad_id, account_id):
            return deepcopy(proof)
    return None


def validate_basis(basis, evidence, context, ad=None, deleted_proof=None):
    """Pure current-identity + fresh-threshold gate shared by scan and execution."""
    try:
        if context.get('error'):
            return False, 'creative_basis_unavailable'
        identity = material_identity(context['rows'], context['sources'], context['scope'],
                                     basis['creative_id'])
        if (identity['reason'] or identity['root_id'] != basis['root_id']
                or set(identity['material_ids']) != set(basis['material_ids'])
                or set(identity['product_ids']) != set(basis['identity']['product_ids'])
                or set(parse_ids(context['credential_users'])) != {str(basis['user_id'])}):
            return False, 'creative_basis_changed'
        if not current_account_matches(context, basis):
            return False, 'creative_basis_changed'
        if not set(basis['material_ids']) & qualified_material_ids(evidence):
            return False, 'violation_threshold_not_met'
        # SQL identity is never a substitute for exact Meta account/binding proof.
        if ad is not None:
            if not isinstance(ad, dict) or str(ad.get('id')) != str(basis['ad_id']) or str(ad.get('account_id')) != str(basis['account_id']):
                return False, 'creative_basis_changed'
            if ad.get('status') == 'DELETED':
                if (ad.get('creative') or {}).get('id') is not None and str(ad['creative']['id']) != str(basis['creative_id']):
                    return False, 'creative_basis_changed'
                deleted_proof = ad
            elif str((ad.get('creative') or {}).get('id')) != str(basis['creative_id']):
                return False, 'creative_basis_changed'
            else:
                decision = evaluate_ad(ad, identity, evidence)
                return decision['eligible'], decision['reason']
        if not exact_deleted_proof(deleted_proof, basis['ad_id'], basis['account_id']):
            return False, 'deleted_ad_proof_unavailable'
        # Old active-Ad receipts cannot turn an active historical basis into a
        # paused one. The saved pre-deletion snapshot must satisfy every v3 gate.
        prior = basis.get('pre_delete_ad_snapshot') or basis.get('ad_snapshot')
        if (not isinstance(prior, dict) or str(prior.get('id')) != str(basis['ad_id'])
                or str(prior.get('account_id')) != str(basis['account_id'])
                or str((prior.get('creative') or {}).get('id')) != str(basis['creative_id'])):
            return False, 'pre_delete_pause_proof_unavailable'
        decision = evaluate_ad(prior, identity, evidence)
        if not decision['eligible']:
            return False, ('pre_delete_pause_proof_unavailable'
                           if decision['reason'] == 'active_ad_protected' else decision['reason'])
        return True, 'confirmed_deleted_paused_basis'
    except (AttributeError, KeyError, TypeError, ValueError):
        return False, 'creative_basis_unavailable'


def assess_backlog(runner, entries, evidence):
    """Read-only requalification; retain original evidence in every saved entry."""
    from .runner import ledger_error
    qualified = qualified_material_ids(evidence)
    unique, deferred, approved = {}, {}, []
    for entry in entries:
        cid = str((entry.get('basis') or {}).get('creative_id') or '')
        if not cid or cid in unique:
            raise ValueError('backlog_identity_conflict')
        unique[cid] = entry
    pending = []
    for cid, entry in unique.items():
        basis = entry.get('basis') or {}
        if not set(basis.get('material_ids') or []) & qualified:
            deferred[cid] = 'violation_threshold_not_met'
        else:
            pending.append(entry)
    for offset in range(0, len(pending), 64):
        runner.check_stop()
        part = pending[offset:offset + 64]
        bases = [entry['basis'] for entry in part]
        try:
            if hasattr(runner.source, '_session'):
                contexts = current_contexts(runner.source, bases)
            else:
                with runner.sql_snapshot():
                    contexts = {(b['ad_id'], b['creative_id']): runner.source.current_ad_context(b['ad_id'], b['creative_id']) for b in bases}
        except Exception as exc:
            if ledger_error(exc) or isinstance(exc, InterruptedError):
                raise
            deferred.update({b['creative_id']: 'creative_basis_unavailable' for b in bases})
            continue
        proofs, records, targets = {}, {}, []
        for basis in bases:
            cid = basis['creative_id']
            if runner.store is not None:
                with runner.store._transaction(False) as conn:
                    lock = conn.execute('SELECT status FROM fb_asset_delete_v2_object_locks WHERE object_key IN (?,?) AND status IN (?,?) LIMIT 1',
                                        ('creative:' + cid, 'ad:' + basis['ad_id'], 'unknown', 'in_progress')).fetchone()
                if lock:
                    deferred[cid] = 'unknown_fence'
                    continue
            proof = read_deleted_ad_proof(runner.store, basis['ad_id'], basis['account_id'])
            if proof is not None:
                proofs[cid] = proof
            else:
                targets.append(basis)
        if targets:
            try:
                if hasattr(runner.graph, 'iter_target_ads'):
                    for (_, account, _), values, failures in runner.graph.iter_target_ads(targets, runner.stop):
                        for aid, value in values.items():
                            if str(value.get('account_id')) == str(account):
                                records[(str(aid), str(account))] = value
                else:
                    for basis in targets:
                        try:
                            records[(basis['ad_id'], basis['account_id'])] = runner.graph.get_ad(basis['ad_id'], basis['user_id'])
                        except Exception as exc:
                            if ledger_error(exc) or isinstance(exc, InterruptedError):
                                raise
            except Exception as exc:
                if ledger_error(exc) or isinstance(exc, InterruptedError):
                    raise
        for entry in part:
            basis = entry['basis']; cid = basis['creative_id']
            if cid in deferred:
                continue
            valid, reason = validate_basis(basis, evidence, contexts[(basis['ad_id'], cid)],
                                           records.get((basis['ad_id'], basis['account_id'])), proofs.get(cid))
            if not valid:
                deferred[cid] = reason
                continue
            current = deepcopy(entry)
            current['execution_evidence'] = {'materials': {
                mid: deepcopy(evidence['materials'][mid]) for mid in basis['material_ids']
                if mid in evidence['materials']}}
            current['requalification'] = {'policy_version': '20261027-v3',
                'checked_at': now(), 'eligible': True, 'reason': reason}
            approved.append(current)
    return approved, {'total': len(unique), 'eligible_ids': sorted((r['basis']['creative_id'] for r in approved), key=int),
                      'deferred_by_reason': dict(Counter(deferred.values())), 'deferred_ids': deferred}
