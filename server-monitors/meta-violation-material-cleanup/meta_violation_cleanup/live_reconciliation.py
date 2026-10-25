"""GET-only verification and atomic ledger synchronization beside a live worker.

No Runner, DELETE, run recovery or object claim is performed by this module.
Pending legacy objects may be settled only with an exact DELETED readback.
"""
import json

from features.fb_ad_asset_delete.store import StoreError, _json, _now
from .graph import GraphError, _object_id


def read_status_batch(graph, kind, identifiers, user_id, account_id, stop=lambda:False):
    if kind not in ('ad','creative') or not 1 <= len(identifiers) <= 50:
        raise ValueError('invalid_reconciliation_batch')
    ids=[_object_id(value) for value in identifiers]
    account=_object_id(account_id,account=True)
    results={};pending=[ids]
    while pending:
        if stop():raise InterruptedError('stop_requested')
        part=pending.pop()
        try:
            data=graph._get('',user_id,{'ids':','.join(part),'fields':'id,account_id,status'})
        except GraphError as exc:
            if len(part)>1 and exc.code in (100,803):
                middle=len(part)//2;pending.extend((part[middle:],part[:middle]));continue
            results.update({identifier:{'status':'unknown','error':exc.as_dict()} for identifier in part});continue
        for identifier in part:
            try:
                value=data.get(identifier) if isinstance(data,dict) else None
                if isinstance(value,dict) and value.get('error'):
                    error=value['error']
                    results[identifier]={'status':'unknown','error':{'code':error.get('code'),
                        'error_subcode':error.get('error_subcode')}};continue
                record=graph._record(value,identifier,account,kind=kind)
                results[identifier]={'status':'deleted_verified' if record.get('status')=='DELETED' else
                                     'present' if record.get('status') else 'unknown','object':record}
            except GraphError as exc:
                results[identifier]={'status':'unknown','error':exc.as_dict()}
    return results


def sync_observation(store, job_id, key, outcome, user_id, actor, *, allow_pending=False,
                     expected_updated_at=None):
    """Release a fence only on exact proof; atomically reject live/stale claims."""
    with store._transaction() as conn:
        job=store._require(conn,'jobs','job_id',job_id)
        if job['actor']!=actor or not job_id.startswith('policy-cleanup-'):
            raise StoreError('Reconciliation job ownership mismatch','invalid_input')
        row=store._object(conn,job_id,key);payload=json.loads(row['payload'])
        if row['kind'] not in ('ad','creative') or key!=row['kind']+':'+row['object_id']:
            raise StoreError('Reconciliation kind mismatch','invalid_input')
        accounts=payload.get('account_ids',[]);users=payload.get('user_ids',[])
        previous=json.loads(row['result'])
        selected=previous.get('credential_user_id')
        if len(accounts)!=1 or str(user_id) not in users or (selected and str(user_id)!=str(selected)):
            raise StoreError('Reconciliation credential mismatch','invalid_input')
        if row['status'] in ('deleted','already_deleted'):
            return {'status':'already_resolved','ledger_status':row['status']}
        lock=conn.execute('SELECT status FROM fb_asset_delete_v2_object_locks WHERE object_key=?',(key,)).fetchone()
        if row['status']=='in_progress' or (lock and lock['status']=='in_progress'):
            return {'status':'active_claim_untouched'}
        if expected_updated_at is not None and row['updated_at']!=expected_updated_at:
            return {'status':'changed_untouched'}
        if row['status']!='unknown' and not (allow_pending and row['status']=='pending'):
            return {'status':'ineligible_untouched','ledger_status':row['status']}
        verified=outcome.get('status')=='deleted_verified'
        proof=outcome.get('object') or {}
        if verified and (str(proof.get('id'))!=row['object_id'] or proof.get('status')!='DELETED'
                         or str(proof.get('account_id'))!=str(accounts[0])):
            raise StoreError('Exact account/object DELETED proof required','proof_required')
        now=_now()
        result=dict(previous,verification=outcome,read_only_reconciled_at=now,
                    credential_user_id=str(user_id),confirmed_deleted=verified)
        if not verified:
            encoded=_json(result)
            conn.execute('UPDATE fb_asset_delete_v2_objects SET result=?,updated_at=? WHERE job_id=? AND object_key=?',
                         (encoded,now,job_id,key))
            store._audit(conn,job_id,'live_read_only_observation',{'key':key,'outcome':outcome})
            return {'status':outcome.get('status','unknown'),'ledger_status':row['status']}
        status='already_deleted' if row['status']=='pending' else 'deleted'
        result.update(status='deleted_verified',proof=proof,
                      prior_delete_request_sent=previous.get('request_sent'),request_sent=False,
                      reconciliation_only=True)
        encoded=_json(result)
        store._receipt(conn,key,job_id,row['run_id'],row['attempt_id'],status,encoded)
        # A positive exact proof applies to every unknown fence for this global
        # object. Never rewrite any active claim or reset anything to pending.
        affected=conn.execute("SELECT * FROM fb_asset_delete_v2_objects WHERE object_key=? AND status='unknown'",(key,)).fetchall()
        if row['status']=='pending':affected.append(row)
        for item in affected:
            final='already_deleted' if item['status']=='pending' else 'deleted'
            item_result=dict(json.loads(item['result']),verification=outcome,
                read_only_reconciled_at=now,confirmed_deleted=True,status='deleted_verified',
                proof=proof,reconciliation_only=True,
                verification_credential_user_id=str(user_id))
            item_encoded=_json(item_result)
            conn.execute('UPDATE fb_asset_delete_v2_objects SET status=?,result=?,updated_at=? WHERE job_id=? AND object_key=?',
                         (final,item_encoded,now,item['job_id'],key))
            if item['attempt_id']:
                conn.execute("UPDATE fb_asset_delete_v2_attempts SET status='deleted',finished_at=?,result=? WHERE attempt_id=? AND status='unknown'",
                             (now,item_encoded,item['attempt_id']))
            store._audit(conn,item['job_id'],'live_read_only_reconciled',{'key':key,'source_job_id':job_id,
                         'status':final,'proof':proof,'credential_user_id':str(user_id)})
        conn.execute("DELETE FROM fb_asset_delete_v2_object_locks WHERE object_key=? AND status='unknown'",(key,))
        return {'status':'deleted_verified','ledger_status':status,'affected':len(affected)}
