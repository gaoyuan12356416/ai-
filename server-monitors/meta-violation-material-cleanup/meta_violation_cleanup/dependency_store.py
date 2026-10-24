"""Append only proven shared-Ad dependencies to this cleanup's active job.

Frozen initial objects, completed receipts and global unknown fences remain
immutable. Every appended object records the exact parent Creative and GET.
"""
import json
from features.fb_ad_asset_delete.store import StoreError, _json, _now


def append_shared_ad(store, job_id, run_id, creative_id, ad, user_id, actor):
    key='ad:'+str(ad['id']); cid=str(creative_id); now=_now()
    with store._transaction() as conn:
        job=store._require(conn,'jobs','job_id',job_id)
        run=store._require(conn,'runs','run_id',run_id)
        metadata=json.loads(job['metadata'])
        if (job['actor']!=actor or run['actor']!=actor or run['job_id']!=job_id or
                run['status']!='running' or metadata.get('policy_version')!='20261024-v2'):
            raise StoreError('Shared dependency is outside the active cleanup','invalid_state')
        creative=store._object(conn,job_id,'creative:'+cid)
        parent=json.loads(creative['payload'])
        account=str(ad.get('account_id','')).removeprefix('act_')
        if str((ad.get('creative') or {}).get('id'))!=cid or parent.get('account_ids')!=[account]:
            raise StoreError('Shared dependency identity mismatch','invalid_input')
        existing=conn.execute('SELECT * FROM fb_asset_delete_v2_objects WHERE job_id=? AND object_key=?',
                              (job_id,key)).fetchone()
        if existing:
            payload=json.loads(existing['payload'])
            if cid not in payload.get('creative_ids',[]) or payload.get('account_ids')!=[account]:
                raise StoreError('Shared Ad conflicts with frozen object','invalid_input')
            return
        payload={'account_ids':[account],'user_ids':[str(user_id)],'creative_ids':[cid],
                 'dependency_creative_id':cid,'scope_rule':'shared_reference_cleanup_20261024',
                 'discovered_ad':ad}
        ordinal=conn.execute('SELECT COALESCE(MAX(ordinal),-1)+1 FROM fb_asset_delete_v2_objects WHERE job_id=?',
                             (job_id,)).fetchone()[0]
        conn.execute('INSERT INTO fb_asset_delete_v2_objects '
            '(job_id,object_key,kind,object_id,status,ordinal,payload,result,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',
            (job_id,key,'ad',str(ad['id']),'pending',ordinal,_json(payload),'{}',now))
        store._audit(conn,job_id,'shared_dependency_authorized',
                     {'run_id':run_id,'object_key':key,'creative_id':cid,'account_id':account,'ad':ad})
