"""Apply approved Page copy through the normal version-checked Page API only."""
import argparse,hashlib,json,sqlite3,sys,threading,urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

SOURCE=Path(__file__).resolve().parents[1]
sys.path.insert(0,'/opt/fb-auto-post/current')
from features.fb_video_publish.client import request

DB=Path('/mnt/data-disk/fb-auto-post-publisher/fb-auto-post.sqlite3')
TENANT='149bb4a14b57975f'
CREATOR='892fd2e8'

def envfile(path):
    result={}
    for line in Path(path).read_text().splitlines():
        if line.strip() and not line.startswith('#') and '=' in line:
            key,value=line.split('=',1);result[key.strip()]=value.strip().strip('"').strip("'")
    return result

def rows():
    with closing(sqlite3.connect(DB.as_uri()+'?mode=ro',uri=True,timeout=10)) as db:
        db.row_factory=sqlite3.Row
        return [dict(row) for row in db.execute('SELECT * FROM fb_video_page WHERE tenant_key=? AND deleted=0 ORDER BY page_id',(TENANT,))]

def save(path,value):
    temp=path.with_suffix('.tmp');temp.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8');temp.chmod(0o600);temp.replace(path)

def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=('prepare','apply','resume','resume-local'));p.add_argument('--folder',type=Path,required=True);p.add_argument('--sha256');a=p.parse_args()
    folder=a.folder.resolve();assert folder.is_relative_to('/mnt/data-disk/fb-page-caption-20261113')
    bank=json.loads((SOURCE/'ops/fb-page-caption/approved-20261113.json').read_text())
    if a.action=='prepare':
        folder.mkdir(parents=True,exist_ok=False);folder.chmod(0o700)
        before=rows();targets=[row for row in before if row['created_by']==CREATOR]
        assert len(targets)==236 and sum(not row['language'] for row in targets)==20
        plan=[]
        for row in targets:
            hook,label,tag=bank[row['language'] or 'en']
            caption=f'💕{hook} 👉 {{url}}\n\n📺{label}\n{{desc}}\n\n#shortdrama #shortdrama{tag} #dramaseries #dramawave'
            plan.append(dict(before=row,caption=caption))
        save(folder/'plan.json',plan);save(folder/'all-before.json',before)
        digest=hashlib.sha256((folder/'plan.json').read_bytes()).hexdigest()
        print(json.dumps(dict(status='prepared',pages=len(plan),english=sum((r['before']['language'] or 'en')=='en' for r in plan),sha256=digest)));return
    plan=json.loads((folder/'plan.json').read_text());assert a.sha256==hashlib.sha256((folder/'plan.json').read_bytes()).hexdigest()
    previous=json.loads((folder/'receipt.json').read_text()) if (folder/'receipt.json').exists() else None
    if previous and previous.get('status')=='verified':
        print(json.dumps({k:v for k,v in previous.items() if k!='results'}));return
    env=envfile('/etc/fb-auto-post.env')
    env.update({k:v for k,v in envfile('/etc/fb-auto-post-app.env').items() if k.startswith('FB_AUTO_POST_')})
    req=urllib.request.Request('http://127.0.0.1:8787/internal/fb-video-publish/identity',data=json.dumps(dict(tenant_key=TENANT,user_id=CREATOR)).encode(),headers={'Content-Type':'application/json','Authorization':'Bearer '+env['FB_AUTO_POST_INTERNAL_TOKEN']},method='POST')
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req,timeout=30) as response:actor=json.load(response)['actor']
    assert actor['user_name']=='郜远' and actor['is_admin']
    current={r['page_id']:r for r in rows()}
    pending=plan;completed=[]
    if a.action in ('resume','resume-local'):
        assert previous and previous['status']=='partial','only_completed_partial_batch_may_resume'
        pending=[]
        for item in plan:
            old=item['before'];now=current[old['page_id']]
            if now==old:pending.append(item);continue
            assert now['caption_template']==item['caption'] and now['version']==old['version']+1,'configuration_changed_since_plan'
            assert all(now[k]==v for k,v in old.items() if k not in {'caption_template','version','updated_by','updated_at_utc'}),'unrelated_page_field_changed'
            completed.append(dict(page_id=old['page_id'],status='saved_readback',error=''))
        archive=folder/('receipt-before-'+a.action+'-'+str(len(completed))+'.json')
        assert not archive.exists(),'resume_without_progress_requires_review'
        (folder/'receipt.json').rename(archive)
    else:
        assert all(current[item['before']['page_id']]==item['before'] for item in plan),'configuration_changed_since_plan'
    if previous and a.action not in ('resume','resume-local'):
        assert previous.get('verified')==0 and all(r['status']=='failed' for r in previous['results']),'partial_receipt_requires_reconciliation'
        assert not (folder/'receipt-route-rejected.json').exists(),'only_one_reconciled_retry_allowed'
        (folder/'receipt.json').rename(folder/'receipt-route-rejected.json')
    if not (folder/'before.sqlite3').exists():
        with closing(sqlite3.connect(DB.as_uri()+'?mode=ro',uri=True)) as source,closing(sqlite3.connect(folder/'before.sqlite3')) as target:source.backup(target)
    (folder/'before.sqlite3').chmod(0o600)
    receipt=dict(status='applying',expected=len(plan),results=completed);lock=threading.Lock();save(folder/'receipt.json',receipt)
    local_store=None
    if a.action=='resume-local':
        # The sidecar is unavailable during a concurrent metric migration.
        # Only the two reconciled, unchanged configurations in this approved
        # creator/tenant manifest may use the normal domain save directly.
        from features.fb_video_publish.core import Store,actor_checked
        actor=actor_checked(actor)
        assert actor['is_admin'] and actor['tenant_key']==TENANT and actor['user_id']==CREATOR
        assert len(pending)<=2 and len(completed)>=234
        assert all(item['before']['created_by']==CREATOR and item['before']['tenant_key']==TENANT for item in pending)
        local_store=Store(DB)
    def update(item):
        old=item['before'];pid=old['page_id'];error=''
        try:
            if local_store:
                local_store.save_configs(actor,[pid],old['language'],item['caption'],expected=(old['config_instance_id'],old['version']),title=old['title'])
            else:
                request('POST','/api/fb-video-publish/pages/'+pid,actor=actor,environ=env,payload=dict(config_instance_id=old['config_instance_id'],expected_version=old['version'],language=old['language'],caption_template=item['caption'],title=old['title']))
            status='saved'
        except Exception as exc:
            # Do not replay an uncertain write; reconcile the persistent row.
            now=next((r for r in rows() if r['page_id']==pid),None)
            status='saved_readback' if now and now['caption_template']==item['caption'] and now['version']==old['version']+1 else 'failed'
            error=str(getattr(exc,'code',type(exc).__name__))[:80]
        with lock:
            receipt['results'].append(dict(page_id=pid,status=status,error=error));save(folder/'receipt.json',receipt)
            if len(receipt['results'])%20==0:print(json.dumps(dict(done=len(receipt['results']),failed=sum(r['status']=='failed' for r in receipt['results']))),flush=True)
    with ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(update,pending))
    after={r['page_id']:r for r in rows()};target_ids={r['before']['page_id'] for r in plan}
    changed_fields={'caption_template','version','updated_by','updated_at_utc'}
    good=[]
    for item in plan:
        old=item['before'];now=after[old['page_id']]
        assert all(now[k]==v for k,v in old.items() if k not in changed_fields),'unrelated_page_field_changed'
        good.append(now['caption_template']==item['caption'] and now['version']==old['version']+1)
    unrelated=json.loads((folder/'all-before.json').read_text())
    receipt.update(status='verified' if all(good) else 'partial',verified=sum(good),unrelated_unchanged=all(after.get(r['page_id'])==r for r in unrelated if r['page_id'] not in target_ids),blank_language_preserved=sum(not after[pid]['language'] for pid in target_ids))
    save(folder/'receipt.json',receipt);save(folder/'after.json',list(after.values()))
    print(json.dumps({k:v for k,v in receipt.items() if k!='results'}));assert all(good),'partial_update_read_receipt'

if __name__=='__main__':main()
