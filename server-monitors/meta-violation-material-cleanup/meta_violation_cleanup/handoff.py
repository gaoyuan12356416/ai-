"""Derive a fresh preview with Creative backlog produced after its freeze.

The caller must hold the draining legacy lease while capturing backlog and
installing the handoff. This pure function cannot mutate a ledger or call Meta.
The source manifest and its original expiry remain unchanged.
"""
from copy import deepcopy
from datetime import datetime
import re

from .runtime import CST, digest, validate_run_id


def checked(manifest):
    if digest({k: v for k, v in manifest.items() if k != 'sha256'}) != manifest.get('sha256'):
        raise ValueError('handoff_manifest_hash_mismatch')
    if (manifest.get('policy_version') != '20261024-v2'
            or manifest.get('root_products') != ['1479', '3302'] or manifest.get('comparison_only')):
        raise ValueError('handoff_manifest_scope_invalid')


def derive_with_backlog(source, run_id, backlog, load_origin, *, at=None):
    """Add only missing Creatives with exact provenance; never add an Ad."""
    checked(source)
    validate_run_id(run_id)
    if source['run_id'] == run_id or source.get('scanner') != 'material_indexed':
        raise ValueError('handoff_new_run_id_required')
    frozen = datetime.fromisoformat(source['frozen_at'])
    if frozen.tzinfo is None:
        frozen = frozen.replace(tzinfo=CST)
    age = ((at or datetime.now(CST)) - frozen).total_seconds()
    if not 0 <= age <= 3600:
        raise ValueError('manifest_expired_start_new_scan')
    represented = {c['creative_id'] for c in source['candidates']}
    represented.update(c['creative_id'] for c in source.get('blocked_account_creatives', []))
    represented.update(e['basis']['creative_id'] for e in source.get('recovery_creatives', []))
    additions, seen, origins = [], set(), {}
    for entry in backlog:
        basis = entry['basis']
        cid = basis['creative_id']
        if cid in seen:
            raise ValueError('handoff_duplicate_backlog_identity')
        seen.add(cid)
        if cid in represented:
            continue
        for key in ('creative_id', 'ad_id', 'account_id', 'user_id'):
            if not isinstance(basis.get(key), str) or not re.fullmatch(r'[1-9][0-9]{0,31}', basis[key]):
                raise ValueError('handoff_backlog_identity_invalid')
        if basis.get('root_id') not in ('1479', '3302') or basis.get('eligible') is not True:
            raise ValueError('handoff_backlog_scope_invalid')
        origin_id = validate_run_id(entry['origin_run_id'])
        if origin_id not in origins:
            origin = load_origin(origin_id)
            checked(origin)  # Historical origin may be expired; only its queue is inherited.
            if origin['run_id'] != origin_id:
                raise ValueError('handoff_origin_run_mismatch')
            candidates = origin['candidates'] + origin.get('blocked_account_creatives', [])
            origins[origin_id] = (origin, {c['ad_id']: c for c in candidates})
        origin, candidates = origins[origin_id]
        if origin['sha256'] != entry.get('origin_manifest_sha256'):
            raise ValueError('handoff_backlog_origin_hash_mismatch')
        if candidates.get(basis['ad_id']) != basis:
            raise ValueError('handoff_backlog_basis_mismatch')
        expected = {'materials': {mid: origin['evidence']['materials'][mid]
                                 for mid in basis['material_ids'] if mid in origin['evidence']['materials']}}
        if not expected['materials'] or expected != entry.get('evidence'):
            raise ValueError('handoff_backlog_evidence_mismatch')
        additions.append(deepcopy(entry))
    result = deepcopy(source)
    result['run_id'] = run_id
    result['recovery_creatives'] = result.get('recovery_creatives', []) + additions
    result['handoff'] = {'source_run_id': source['run_id'], 'source_manifest_sha256': source['sha256'],
                         'inherited_frozen_at': source['frozen_at'], 'backlog_snapshot_count': len(backlog),
                         'added_creative_ids': sorted(e['basis']['creative_id'] for e in additions),
                         'backlog_snapshot_sha256': digest(backlog)}
    result['sha256'] = digest({k: v for k, v in result.items() if k != 'sha256'})
    return result
