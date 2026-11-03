"""Restore the two switch-interrupted original queues after durable no-Post proof.

No credentials or platform requests. No new queues, recipes, or material choices.
The abandoned incomplete media session cannot have produced an X Post: neither
native nor central ledger granted a Post/Repost operation, and both workers ACKed
their terminal failures. Prior attempts remain in both operation/claim ledgers.
"""
import argparse,contextlib,datetime as dt,hashlib,json,sqlite3
from pathlib import Path

BASE=Path('/mnt/data-disk/pipeline-dispatch')
NATIVE=Path('/var/lib/x-post-automation/accounts.sqlite3')
OUTBOX='/mnt/data-disk/pipeline-node-cpu/pipeline-dispatch/receipts.sqlite3'
CASES=((32064,5452,5385,1),(32065,5558,5501,0))
def now():return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds').replace('+00:00','Z')
def connect(path):
 c=sqlite3.connect(str(path),timeout=15);c.row_factory=sqlite3.Row;return c
def validate(c,outbox):
 proofs=[]
 for jid,qid,lid,attempts in CASES:
  key='x:queue:'+str(qid)
  j=dict(c.execute('select * from pipeline_job where id=?',(jid,)).fetchone())
  q=dict(c.execute('select * from native.x_post_queue where id=?',(qid,)).fetchone())
  log=dict(c.execute('select * from native.x_post_publish_log where id=?',(lid,)).fetchone())
  assert j['source_key']==key and j['channel']=='x' and j['stage']=='publish' and j['generation']==1
  assert j['status']==('unknown' if attempts else 'failed') and j['error_code']=='pipeline_lease_lost'
  assert q['status']=='failed' and log['queue_id']==qid and log['status']=='failed'
  assert log['attempt_count']==attempts and log['unknown_outcome']==attempts and not log['x_post_id'] and not log['x_media_id']
  assert log['error_code']=='pipeline_lease_lost'
  assert not c.execute('select 1 from pipeline_publication where source_key=?',(key,)).fetchone()
  assert not c.execute("select 1 from pipeline_job where source_key=? and status='running'",(key,)).fetchone()
  ops=[dict(x) for x in c.execute('select operation_id,operation,request_sha256 from pipeline_operation where job_id=?',(jid,))]
  nativeops=[dict(x) for x in c.execute('select operation_id,operation,request_sha256 from native.pipeline_x_operation where source_key=?',(key,))]
  assert sorted(ops,key=lambda x:x['operation_id'])==sorted(nativeops,key=lambda x:x['operation_id'])
  assert len(ops)==(3 if attempts else 0)
  assert all(x['operation'].startswith('x:POST:/2/media/upload/') and x['operation'].split('/')[-1] in ('initialize','append') for x in ops)
  assert not c.execute("select 1 from pipeline_checkpoint where job_id=? and phase not in ('x_media_initialized')",(jid,)).fetchone()
  r=outbox.execute("select payload,acked_at from receipt_outbox where route='jobs/fail' and json_extract(payload,'$.job_id')=? and json_extract(payload,'$.generation')=1",(jid,)).fetchall()
  assert len(r)==1 and r[0]['acked_at']
  b=json.loads(r[0]['payload']);delivery=c.execute('select * from pipeline_delivery where delivery_id=?',(b['receipt_id'],)).fetchone()
  assert b['error_code']=='pipeline_lease_lost' and b['node_id']=='cpu-main'
  assert delivery and delivery['claim_hash']==hashlib.sha256(b['claim_token'].encode()).hexdigest()
  assert delivery['request_sha256']==hashlib.sha256(json.dumps(b,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
  # Frozen preparation remains mandatory and unchanged by this operation.
  prep=c.execute("select status,artifact_json from pipeline_job where source_key=? and stage='prepare'",(key,)).fetchone()
  assert prep and prep['status'] in ('completed','ready') and json.loads(prep['artifact_json'])
  proofs.append({'job_id':jid,'queue_id':qid,'log_id':lid,'receipt_id':b['receipt_id'],'prior_media_attempts':attempts,'post_permits':0,'operations':ops,'queue_sha256':hashlib.sha256(json.dumps(q,sort_keys=True).encode()).hexdigest(),'artifact_sha256':hashlib.sha256(prep['artifact_json'].encode()).hexdigest()})
 return proofs
def main():
 p=argparse.ArgumentParser();p.add_argument('--apply',action='store_true');args=p.parse_args()
 with contextlib.closing(connect(BASE/'dispatch.sqlite3')) as c,contextlib.closing(connect(OUTBOX)) as outbox:
  c.execute('attach database ? as native',(str(NATIVE),));proof=validate(c,outbox)
  print(json.dumps({'verified':proof,'apply':args.apply}),flush=True)
  if not args.apply:return
  backup=BASE/('x-switch-recovery-'+dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ'));backup.mkdir(mode=0o700)
  for name,path in (('dispatch',BASE/'dispatch.sqlite3'),('native',NATIVE)):
   with contextlib.closing(connect(path)) as src,contextlib.closing(sqlite3.connect(backup/(name+'-before.sqlite3'))) as dst:src.backup(dst,pages=1000)
   (backup/(name+'-before.sqlite3')).chmod(0o600)
  (backup/'proof.json').write_text(json.dumps(proof,indent=2))
  c.execute('begin immediate');assert validate(c,outbox)==proof
  for item in proof:
   jid,qid,lid=item['job_id'],item['queue_id'],item['log_id']
   c.execute("update native.x_post_publish_log set status='reserved',attempt_count=0,unknown_outcome=0,error_code='',error_message='',started_at='',updated_at=? where id=?",(now(),lid))
   c.execute("update native.x_post_queue set status='queued',updated_at=? where id=?",(now(),qid))
   c.execute('delete from pipeline_resource where job_id=?',(jid,))
   c.execute('delete from pipeline_legacy_resource where job_id=?',(jid,))
   c.execute("update pipeline_job set status='ready',source_status='queued',hold_reason='',node_id='',worker_id='',claim_hash='',lease_expires_at='',external_intent=0,started_at='',finished_at='',error_code='',error_message='',canonical_receipt='',updated_at=? where id=?",(now(),jid))
   c.execute('insert into pipeline_event(job_id,event_type,details_json,created_at) values(?,?,?,?)',(jid,'operator_no_post_recovery',json.dumps(item),now()))
  c.commit();print(json.dumps({'restored_original_jobs':[x['job_id'] for x in proof],'backup':str(backup)}),flush=True)
if __name__=='__main__':main()
