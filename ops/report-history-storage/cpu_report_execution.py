from collections import Counter
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import socket
import sys

root=Path('/mnt/data-disk')
j=root/'report-storage-20261120'
sys.path.insert(0,str(j/'version-69d284113d8101cc4e7df40c168a56099a8e4dbb/ops/report-history-storage'))
import history

def read_lines(f):
    if not f.exists():return []
    with f.open() as stream:return [json.loads(line) for line in stream if line.endswith('\n') and line.strip()]
def mount(p):
    s=os.statvfs(p)
    return dict(capacity=s.f_blocks*s.f_frsize,used=(s.f_blocks-s.f_bfree)*s.f_frsize,available=s.f_bavail*s.f_frsize)
def accumulate(rows,key,props):
    acc={}
    for v in rows:
        dst=acc.setdefault(v[key],{'files':0,**dict.fromkeys(props,0)})
        dst['files']+=1
        for field in props:dst[field]+=int(v.get(field) or 0)
    return acc
inventory=json.loads((j/'plans/history-v1.json').read_text())
frozen={e['path']:e for e in inventory}
checks=json.loads((j/'status/history-deliveries.json').read_text())
nested=j/'status/history-nested-deliveries.json'
if nested.is_file():checks.update(json.loads(nested.read_text()))
all_jobs={e['path']:{'dataset':history.match(e['path']),'path':e['path'],'bytes':e['size'],'status':'queued'} for e in inventory}
for f in sorted((j/'receipts').glob('history*.jsonl')):
    if f.name.startswith('history-retire-'):continue
    for record in read_lines(f):
        target=all_jobs[record['path']]
        if record['status']=='verified' or (target.get('status')!='verified' and record.get('time',0)>=target.get('time',0)):
            target.update(record)
            if record['status']=='verified':
                target.pop('reason',None)
                target.pop('errno',None)
catalog=[]
catalog_proofs={}
for f in sorted((j/'status').glob('catalog-*.json')):
    if (j/'status'/f.with_suffix('.log').name).is_file():
        catalog_proofs.update(json.loads(f.read_text())['eligible'])
sys.path.insert(0,'/opt/report-material-catalog/current/reports/yuesiying/catalog')
from store import metadata
for f in sorted((j/'status').glob('*.import.json')):
    if not f.stat().st_size:continue
    item=json.loads(f.read_text())
    case=f.name[:-len('.import.json')]
    check=j/'status'/(case+'.validate.json')
    generation=item['generation_id']
    current=metadata(str(history.CONFIG),generation)
    assert current['status']=='verified' and int(current['row_count'])==item['rows'] and current['row_sha256']==item['row_sha256']
    proof=catalog_proofs.get(case,{})
    binding=root/'codex-artifacts'/case/'catalog-generation.json'
    bound=binding.is_file() and json.loads(binding.read_text()).get('generation_id')==generation
    if proof.get('consumer')=='validated_bound_generation':assert bound
    if proof:assert proof['source_sha256']==current['source_sha256']
    catalog.append(dict(case=case,**item,consumer_comparison=json.loads(check.read_text()) if check.is_file() and check.stat().st_size else None,
        replica_status=current['status'],bound_to_generation=bound,source_sha256=current['source_sha256'],retirement_proof=proof))
retired={};failures=[]
for f in sorted((j/'receipts').glob('*.jsonl')):
    if f.stem.startswith('history') and not f.stem.startswith('history-retire'):continue
    for v in read_lines(f):
        if v.get('event')=='failed':failures.append(dict(ledger=f.name,path=v['path'],error=v.get('error_type')));continue
        if v.get('event')!='deleted':continue
        rel=Path(v['path']).relative_to(root).as_posix()
        if rel in retired:continue
        if rel.startswith('reports/freereels-'):kind='freereels_working'
        elif rel.endswith('.csv.gz'):kind='loose_csv_gz'
        elif '/downloads/' in rel:kind='media_download_duplicate'
        elif rel.endswith(('/library.json','/library.ndjson','/extra-library.ndjson')):kind='material_library'
        else:kind='report_db_source'
        retired[rel]=dict(path=rel,kind=kind,bytes=v['size'],released_bytes=v['released_bytes'],sha256=v['sha256'],private_verified=v['private'],path_exists_now=Path(v['path']).exists(),restore_receipt=f.name)
verified=[v for v in all_jobs.values() if v['status']=='verified']
for row in all_jobs.values():
    rel=row['path']
    path=root/rel
    disposition='local'
    delivery=checks.get(path.parent.relative_to(root).as_posix(),{})
    if rel in retired:
        disposition='recreated_after_retirement' if retired[rel]['path_exists_now'] else 'removed'
    elif not path.is_file():disposition='absent_without_deletion_receipt'
    elif row['dataset']=='fb_receipt_index':disposition='audit_original'
    elif row['status']!='verified':disposition='awaiting_replica_verification'
    elif not delivery.get('allow'):disposition=delivery.get('reason','delivery_unverified')
    else:
        st=path.lstat()
        orig=frozen[rel]
        if (st.st_ino,st.st_size,st.st_mtime_ns)!=(orig['inode'],orig['size'],orig['mtime_ns']):
            disposition='source_changed_after_freeze'
        else:disposition='verified_original_present'
    row.update(local_disposition=disposition)
    if delivery.get('allow'):
        row['delivery_verification']=delivery.get('verification','published_edition_and_local_hash')
with history.connection(False) as conn,conn.cursor(buffered=True) as cur:
    ids=[v['snapshot'] for v in verified]
    confirmed={}
    for chunk in history.group(ids,80):
        cur.execute('SELECT HEX(snapshot_id),status,record_count,HEX(source_sha256),relative_path,source_format,columns_json FROM '+history.SNAP
                   +' WHERE snapshot_id IN ('+','.join(['UNHEX(%s)']*len(chunk))+')',chunk)
        for sid,st,n,h,p,fmt,cols in cur:
            confirmed[sid.lower()]=(st,n,h.lower(),p)
            all_jobs[p]['source_format']=fmt
            columns=json.loads(cols)
            if fmt=='positional':
                all_jobs[p]['column_mapping']='ordinal_only' if columns and columns[0].startswith('ordinal_') else 'source_sql'
    assert all(confirmed.get(v['snapshot'])==('verified',v['records'],v['source_sha256'],v['path']) for v in verified)
restore=j/'status/media-restore-verified.json';before=json.loads((j/'before.json').read_text())
result=dict(as_of=datetime.now().astimezone().isoformat(),host=socket.gethostname(),database='ads_ai',commits=['a531d97279916b570b7109053dfef659556fc134','0188ceba63769c433affb80a8208b0c5e83d96d4'],data_disk_before=before,data_disk_now=mount(root),system_disk_now=mount('/'),catalog=catalog,history_plan_files=len(inventory),history_plan_bytes=sum(v['size'] for v in inventory),history_status=Counter(v['status'] for v in all_jobs.values()),history_verified_by_dataset=accumulate(verified,'dataset',['records','bytes']),replica_verified=len(verified),retention_reasons=Counter(v.get('reason','other') for v in all_jobs.values() if v['status'] not in ('queued','verified')),retired_by_kind=accumulate(retired.values(),'kind',['bytes','released_bytes']),retired_files=len(retired),released_bytes=sum(v['released_bytes'] for v in retired.values()),private_restore_check=json.loads(restore.read_text()) if restore.exists() else None,archive_failures=failures,case_delivery_checks=Counter(v.get('reason','verified_delivery') for v in json.loads((j/'status/history-deliveries.json').read_text()).values()),receipt_directory=str(j/'receipts'),history_sources=list(all_jobs.values()),retired_sources=list(retired.values()))
result['local_disposition_counts']=Counter(v['local_disposition'] for v in all_jobs.values())
result['case_delivery_checks']=Counter(v.get('verification',v.get('reason','published_edition_and_local_hash')) for v in checks.values())
result['commits'].append('1c780466f9c0ba63ccefda8c152d30ac2941c2e2')
if (j/'version-d17a1d05b7ab4d5e61cef1940f37a5212975d377').is_dir():
    result['commits'].append('d17a1d05b7ab4d5e61cef1940f37a5212975d377')
if (j/'version-964bced9cd44231590211f1fe6449bd1f48f662d').is_dir():
    result['commits'].append('964bced9cd44231590211f1fe6449bd1f48f662d')
for key,name in [('report_restore_check','report-and-catalog-restore-check.json'),('ordinal_roundtrips','ordinal-roundtrips.json'),
                 ('checkpoint_roundtrip','checkpoint-roundtrip.json'),('final_db_audit','final-db-audit.json')]:
    proof=j/'status'/name
    if proof.is_file():result[key]=json.loads(proof.read_text())
current=j/'handoff-cpu/current-full-roundtrips.json'
if current.is_file():
    roundtrips=json.loads(current.read_text())
    ordinal={r['path']:r for r in roundtrips['results'] if '/read-checkpoints/' not in r['path']}
    checkpoints=[r for r in roundtrips['results'] if '/read-checkpoints/' in r['path']]
    assert len(ordinal)==17 and len(checkpoints)==8 and all(r['ordered_values_equal'] for r in roundtrips['results'])
    result['ordinal_roundtrips']=dict(reviewed=17,pending=0,checked=ordinal)
    result['checkpoint_roundtrip']=dict(scope=8,pending=0,checks=checkpoints)
for name in ('numeric-repair-deployment.json','report-review-deployment.json'):
    deployment=j/'handoff-cpu'/name
    if deployment.is_file():
        commit=json.loads(deployment.read_text())['commit']
        if commit not in result['commits']:result['commits'].append(commit)
print(json.dumps(result,ensure_ascii=False,separators=(',',':')))
