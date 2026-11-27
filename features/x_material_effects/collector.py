"""Read confirmed ledgers and GET public X metrics; never create or retry a Post."""
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import urllib.error
import urllib.parse
import urllib.request

from .model import extract, instant, interval, METRICS
from .store import encoded

ACCOUNT_DB='/var/lib/x-post-automation/accounts.sqlite3'
LEDGERS=(ACCOUNT_DB,'/mnt/data-disk/x-tag-publisher/posts.sqlite3')

def readonly(path):
    db=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=10)
    db.row_factory=sqlite3.Row
    return db

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None

def request(url,token,payload=None):
    data=None if payload is None else encoded(payload).encode()
    req=urllib.request.Request(url,data=data,headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
    with urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect()).open(req,timeout=25) as response:
        return json.loads(response.read(8*1024*1024))

def credentials():
    """The existing OAuth owner alone can refresh; no token is logged or copied."""
    with closing(readonly(ACCOUNT_DB)) as db:
        accounts=[dict(r) for r in db.execute("SELECT * FROM x_authorized_account WHERE status='active' AND COALESCE(deleted_at,'')='' ORDER BY access_expires_at DESC")]
    now=datetime.now(timezone.utc)
    for account in accounts:
        try:
            if not account.get('access_expires_at') or instant(account['access_expires_at'])<=now+timedelta(seconds=120):
                actor=dict(user_id=account['owner_user_id'],tenant_key=account['owner_tenant_key'],name='X 素材效果采集',role='user')
                result=request('http://127.0.0.1:8810/internal/accounts/%s/verify'%account['id'],os.environ['X_TAG_ACCOUNT_BROKER_TOKEN'],
                    dict(actor=actor,scope='mine',only_refresh_required=True,preserve_transient_status=True))
                if str(result['item']['x_user_id'])!=str(account['x_user_id']) or result['item']['status']!='active':continue
            path=Path('/var/lib/x-post-automation/tokens')/(str(account['x_user_id'])+'.json')
            token=json.loads(path.read_text()).get('access_token')
            if token:return token
        except (OSError,ValueError,KeyError,urllib.error.HTTPError):continue
    raise RuntimeError('x_metrics_credentials_unavailable')

def discover(paths=LEDGERS):
    with closing(readonly(ACCOUNT_DB)) as db:
        accounts={str(r['id']):dict(r) for r in db.execute('SELECT id,x_user_id,username FROM x_authorized_account')}
    posts={};conflicts=set()
    for lane,path in enumerate(paths):
        with closing(readonly(path)) as db:
            for r in db.execute('''SELECT l.x_post_id,l.x_post_url,l.published_at,l.long_url,l.id log_id,
                    q.id queue_id,q.account_id,q.material_id,q.material_key,q.material_name,q.content_id,
                    q.drama_name,q.material_language,q.source_type,q.delivery_mode,q.relay_account_id
                  FROM x_post_publish_log l JOIN x_post_queue q ON q.id=l.queue_id
                  WHERE l.status='published' AND l.unknown_outcome=0 AND l.x_post_id<>'' AND l.published_at<>'' '''):
                r=dict(r);account=accounts.get(str(r['account_id']),{})
                post_id=str(r['x_post_id']);author=account.get('x_user_id','')
                if r['delivery_mode']=='premium_relay_repost':
                    relay=db.execute('SELECT source_post_id FROM x_post_repost_ledger WHERE queue_id=?',(r['queue_id'],)).fetchone()
                    if not relay or not relay[0]:continue
                    post_id=str(relay[0]);author=accounts.get(str(r['relay_account_id']),{}).get('x_user_id','')
                material=str(r['material_key'] or r['material_id'])
                namespace='ads_custom_source' if r['source_type']=='material' and material.isdigit() else 'drama_episode'
                item=dict(post_id=post_id,post_url='https://x.com/i/status/'+post_id,namespace=namespace,
                    material_id=material,material_name=r['material_name'],product='',catalog='unknown',
                    content_id=str(r['content_id']),drama_name=r['drama_name'],language=r['material_language'],
                    account_id=str(r['account_id']),account_ids=[str(r['account_id'])],account_username=account.get('username',''),
                    x_user_id=str(account.get('x_user_id','')),author_id=str(author),published_at=r['published_at'],
                    source='tag' if lane else 'ordinary',queue_id=r['queue_id'],log_id=r['log_id'],
                    campaign_ids=urllib.parse.parse_qs(urllib.parse.urlsplit(r['long_url']).query).get('af_c_id',[]))
                previous=posts.get(post_id)
                if previous and any(previous[k]!=item[k] for k in ('namespace','material_id','content_id')):
                    conflicts.add(post_id);continue
                if previous:
                    previous['account_ids']=sorted(set(previous['account_ids']+item['account_ids']))
                    previous['campaign_ids']=sorted(set(previous['campaign_ids']+item['campaign_ids']))
                    previous['published_at']=min(previous['published_at'],item['published_at'])
                else:posts[post_id]=item
    for key in conflicts:posts.pop(key,None)
    return list(posts.values()),len(conflicts)

def enrich(posts,reader):
    ids=sorted({p['material_id'] for p in posts if p['namespace']=='ads_custom_source'})
    material={}
    for offset in range(0,len(ids),250):
        sql="SELECT HEX(JSON_OBJECT('id',CAST(id AS CHAR),'product',product,'content_id',CAST(data_source_id AS CHAR))) FROM kunlunads_dev.ads_custom_source WHERE id IN ("+','.join(ids[offset:offset+250])+')'
        for row in reader.query([sql],timeout=30):material[str(row['id'])]=row
    for post in posts:
        row=material.get(post['material_id']) if post['namespace']=='ads_custom_source' else None
        if row and str(row['content_id'])==post['content_id']:
            post['product']=str(row.get('product') or '')
            post['catalog']='moboreels' if post['product'].lower() in ('moboreels','moboshort') else 'dramawave'
        elif post['namespace']=='drama_episode':
            post.update(product='Dramawave',catalog='dramawave')

def revenue(posts,warehouse_root,*,daily=False):
    from features.social_analytics.warehouse_reader import current,connect
    from features.social_analytics.report import campaign_index,resolve
    from features.social_analytics.backend_scope import eligible
    meta,path=current(warehouse_root)
    end=min(meta['end'],(datetime.now(timezone.utc).date()-timedelta(days=1)).isoformat())
    if meta.get('catalog_errors',{}).get('x:2116'):raise ValueError('x_revenue_catalog_unavailable')
    days=(datetime.fromisoformat(end)-datetime.fromisoformat(meta['start'])).days+1
    covered=set(meta.get('coverage',{}).get('x:2116',[]))
    missing=[(datetime.fromisoformat(meta['start'])+timedelta(days=n)).date().isoformat() for n in range(days)
             if (datetime.fromisoformat(meta['start'])+timedelta(days=n)).date().isoformat() not in covered]
    if missing and not daily:
        raise ValueError('x_revenue_coverage_incomplete')
    with closing(connect(path)) as db:
        catalog=[json.loads(r[0]) for r in db.execute("SELECT payload FROM catalog WHERE json_extract(payload,'$.platform')='x'")]
        effects=[json.loads(r[0]) for r in db.execute("SELECT payload FROM effects WHERE platform='x' AND dt<=?",(end,))]
    index=campaign_index(catalog);resolved={key:resolve(rows) for key,rows in index.items()}
    owners=defaultdict(set)
    for p in posts:
        p.update(revenue_cents=None,refund_cents=None,revenue_through=end,revenue_status='unmapped')
        for campaign in set(p['campaign_ids']):owners[str(campaign)].add(p['post_id'])
    byid={p['post_id']:p for p in posts};assigned=0;unassigned=0
    for campaign,ids in owners.items():
        key=('x','2116',campaign)
        if len(ids)!=1 or not resolved.get(key):continue
        pid=next(iter(ids));p=byid[pid]
        facts={str(f['id']).rsplit(':',1)[-1] for row in index[key] for f in row.get('publications',[])}
        if pid not in facts:continue
        p.update(revenue_cents=0,refund_cents=0,revenue_status='matched')
        if daily:
            start=max(meta['start'],(datetime.fromisoformat(end)-timedelta(days=31)).date().isoformat())
            p['_window_income']=dict(start=start,end=end,missing=[d for d in missing if d>=start],daily={})
    seen=set()
    for effect in effects:
        campaign=str(effect['campaign_id']);key=('x',str(effect['site_id']),campaign)
        identity=(effect['date'],*key)
        if identity in seen:raise ValueError('duplicate_revenue_fact')
        seen.add(identity)
        ids=owners.get(campaign,set());p=byid[next(iter(ids))] if len(ids)==1 else None
        cents=int(effect['revenue_cents'])
        if not p or p['revenue_status']!='matched' or not any(eligible(r,effect['date']) for r in index.get(key,[])):
            unassigned+=cents;continue
        p['revenue_cents']+=cents;p['refund_cents']+=int(effect['refund_cents']);assigned+=cents
        if daily and effect['date']>=p['_window_income']['start']:
            totals=p['_window_income']['daily']
            totals[effect['date']]=totals.get(effect['date'],0)+cents
    if assigned!=sum(p['revenue_cents'] or 0 for p in posts):raise ValueError('material_revenue_not_conserved')
    return dict(revenue_source_version=meta['version'],revenue_source_generated_at=meta['generated_at'],
                revenue_through=end,revenue_timezone='UTC',currency='USD',
                assigned_revenue_cents=assigned,unassigned_revenue_cents=unassigned)

def collect_metrics(posts,state_path,*,limit=0,closing_pass=False):
    from features.social_material_effects import sampling
    now=datetime.now(timezone.utc);stamp=now.isoformat();calls=0;success=0;failures=0;upstream=''
    boundary=sampling.closing_start(now) if closing_pass else None
    with closing(sqlite3.connect(state_path)) as db:
        db.execute('CREATE TABLE IF NOT EXISTS observations(post_id TEXT PRIMARY KEY,metrics_json TEXT,metrics_at TEXT,attempt_at TEXT,error_code TEXT,retry_at TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS collection_state(id INTEGER PRIMARY KEY CHECK(id=1),error_code TEXT,retry_at TEXT)')
        sampling.prepare(db)
        previous={r[0]:r for r in db.execute('SELECT * FROM observations')}
        hold=db.execute('SELECT error_code,retry_at FROM collection_state WHERE id=1').fetchone()
        if not hold:
            # Upgrade an initial refresh which observed depleted credits.
            stopped=[r for r in previous.values() if r[4]=='x_http_402' and r[3]]
            if stopped:
                latest=max(stopped,key=lambda r:r[3]);hold=('x_http_402',(instant(latest[3])+timedelta(hours=6)).isoformat())
                db.execute('INSERT INTO collection_state VALUES(1,?,?)',hold);db.commit()
        due=[]
        for post in posts:
            old=previous.get(post['post_id'])
            needed = not old or not old[2] or (instant(old[2])<boundary if boundary else
                     (now-instant(old[2])).total_seconds()>=interval(post['published_at'],now))
            if needed and (not old or not old[5] or instant(old[5])<=now):
                due.append(post)
        due.sort(key=lambda p:p['post_id'] if boundary else
                 (p['post_id'] in previous,-instant(p['published_at']).timestamp()))
        if limit:due=due[:limit]
        pending=len(due)
        if hold and instant(hold[1])>now:upstream=hold[0];due=[]
        token=credentials() if due else None
        for offset in range(0,len(due),100):
            batch=due[offset:offset+100];values={};code='';retry=now+timedelta(hours=1)
            try:
                url='https://api.x.com/2/tweets?'+urllib.parse.urlencode(dict(ids=','.join(p['post_id'] for p in batch),**{'tweet.fields':'author_id,public_metrics'}))
                response=request(url,token);calls+=1;values={str(v['id']):v for v in response.get('data',[])}
            except urllib.error.HTTPError as exc:
                code='x_http_'+str(exc.code)
                if exc.code in (401,402,403,429):retry=now+timedelta(hours=6)
            except (OSError,ValueError):code='x_metrics_transport_error'
            stamp=datetime.now(timezone.utc).isoformat()
            for post in batch:
                value=values.get(post['post_id']);old=previous.get(post['post_id'])
                error=code or ('post_unavailable' if value is None else '')
                if value and post['author_id'] and str(value.get('author_id'))!=post['author_id']:error='post_author_mismatch'
                if error:
                    failures+=1;metrics=old[1] if old else '{}';at=old[2] if old else ''
                else:
                    success+=1;fresh=extract(value);metrics=encoded(fresh);at=stamp
                    sampling.save(db,post['post_id'],fresh,stamp)
                next_attempt=now+timedelta(seconds=interval(post['published_at'],now)) if error=='post_unavailable' else retry
                db.execute('INSERT INTO observations VALUES(?,?,?,?,?,?) ON CONFLICT(post_id) DO UPDATE SET metrics_json=excluded.metrics_json,metrics_at=excluded.metrics_at,attempt_at=excluded.attempt_at,error_code=excluded.error_code,retry_at=excluded.retry_at',
                    (post['post_id'],metrics,at,stamp,error,next_attempt.isoformat() if error else ''))
            if code in ('x_http_401','x_http_402','x_http_403','x_http_429'):
                upstream=code
                db.execute('INSERT INTO collection_state VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET error_code=excluded.error_code,retry_at=excluded.retry_at',(code,retry.isoformat()))
                db.commit();break
            if values:db.execute('DELETE FROM collection_state WHERE id=1')
            db.commit()
        observations={r[0]:r for r in db.execute('SELECT * FROM observations')}
        for post in posts:
            row=observations.get(post['post_id'])
            post.update({k:None for k in METRICS if k not in ('revenue_cents','refund_cents')})
            post.update(json.loads(row[1]) if row else {})
            post.update(metrics_at=row[2] if row else None,error_code=row[4] if row else 'not_collected')
        sampling.prune(db)
        db.commit()
    return dict(api_calls=calls,metrics_updated=success,metrics_failed=failures,metrics_due=pending,metrics_upstream_error=upstream)
