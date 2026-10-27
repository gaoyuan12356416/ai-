"""V3 entry point and exact basis helpers; historical state is never authority.

The V3 executor checks shared references before any Creative deletion. Failed
and unknown writes are not replayed inside the same frozen run.
"""
from collections import Counter, defaultdict
from copy import deepcopy
import uuid
import time

from .dependency_store import append_shared_ad
from .graph import GraphClient, GraphError
from .policy import material_identity, evaluate_ad, _feedback_empty
from .runtime import digest, legacy_delete_lock, now
from .skip_policy import SkipPolicy


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
    return (result.get('status')=='failed' and
            result.get('response',{}).get('error_subcode') in (3858800,1487235))


def global_creative_state(store, creative_id):
    with store._transaction(False) as conn:
        key='creative:'+creative_id
        if conn.execute('SELECT 1 FROM fb_asset_delete_v2_receipts WHERE object_key=?',(key,)).fetchone():
            return 'deleted'
        lock=conn.execute('SELECT status FROM fb_asset_delete_v2_object_locks WHERE object_key=?',(key,)).fetchone()
        return lock['status'] if lock else None


def object_state(store,job,key):
    with store._transaction(False) as conn:
        return store._object_data(store._object(conn,job,key))


def has_ad_receipt(store,identifier):
    with store._transaction(False) as conn:
        return conn.execute('SELECT 1 FROM fb_asset_delete_v2_receipts WHERE object_key=?',
                            ('ad:'+identifier,)).fetchone() is not None


def basis_valid(runner,basis,evidence,disabled=False,*,context=None,graph=None,receipt=None):
    """V3 current basis validation; exact proof never bypasses the new rule."""
    from .backlog_policy import validate_basis, read_deleted_ad_proof
    if disabled:
        return False, 'disabled_account_probe_forbidden'
    if context is None:
        runner.serial_context_checked_at=time.monotonic()
        with runner.sql_snapshot():
            context=runner.source.current_ad_context(basis['ad_id'],basis['creative_id'])
    graph=graph or runner.execution_graph
    if hasattr(getattr(graph,'http',None),'account'):
        graph.http.account=basis['account_id']
    proof=receipt if isinstance(receipt,dict) else None
    # Workers receive an owner-thread prefetched proof, including explicit None.
    if receipt is None and context is not None and graph is runner.execution_graph:
        proof=read_deleted_ad_proof(runner.store,basis['ad_id'],basis['account_id'])
    ad=None if proof else graph.get_ad(basis['ad_id'],basis['user_id'])
    return validate_basis(basis,evidence,context,ad,proof)


def execute(runner,run_id,manifest,max_ads=None,start_phase='start'):
    from .execution_v3 import execute as execute_v3
    return execute_v3(runner,run_id,manifest,max_ads,start_phase)
