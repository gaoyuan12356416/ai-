"""GET-only X metrics collector. OAuth refresh stays with the existing owner."""
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import argparse
import json
import os
from pathlib import Path
import sqlite3
import urllib.error
import urllib.parse
import urllib.request

from . import store
from .metrics import extract
from .model import build_profiles, instant

ACCOUNT_DB='/var/lib/x-post-automation/accounts.sqlite3'
STAGES=((6,'6h'),(24,'24h'),(72,'72h'),(168,'7d'))

@contextmanager
def readonly(path):
    db=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=15)
    db.row_factory=sqlite3.Row
    try:
        yield db
    finally:
        db.close()

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

def request(url, token, payload=None):
    raw=None if payload is None else json.dumps(payload).encode()
    req=urllib.request.Request(url,data=raw,headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
    with urllib.request.build_opener(NoRedirect()).open(req,timeout=25) as response:
        return json.loads(response.read(4*1024*1024))

def access_token(account,now):
    expires=account.get('access_expires_at')
    if not expires or instant(expires)<=now+timedelta(seconds=120):
        actor=dict(user_id=account['owner_user_id'],tenant_key=account['owner_tenant_key'],name='成长画像采集',role='user')
        token=os.environ.get('X_TAG_ACCOUNT_BROKER_TOKEN','')
        if not token:
            raise ValueError('account_broker_unavailable')
        value=request('http://127.0.0.1:8810/internal/accounts/%s/verify'%account['id'],token,
                      dict(actor=actor,scope='mine',only_refresh_required=True,preserve_transient_status=True))
        item=value.get('item') or {}
        if str(item.get('x_user_id'))!=str(account['x_user_id']) or item.get('status')!='active':
            raise ValueError('account_identity_or_authorization_changed')
    # Atomic credential files are read only, never copied or rewritten here.
    target=Path(os.getenv('X_TOKENS_DIR','/var/lib/x-post-automation/tokens'))/(str(account['x_user_id'])+'.json')
    return json.loads(target.read_text())['access_token']

def discover(db,accounts,now):
    row=db.execute("SELECT value_json FROM collector_state WHERE key='started_at'").fetchone()
    if row:
        start=instant(json.loads(row[0]))-timedelta(hours=24)
    else:
        start=now-timedelta(hours=24)
        db.execute('INSERT INTO collector_state VALUES(?,?)',('started_at',store.dumps(now.isoformat())))
    start=max(start,now-timedelta(days=29)).isoformat().replace('+00:00','Z')
    auto_path=os.getenv('X_AUTO_POST_DB_PATH','/mnt/data-disk/x-tag-publisher/auto/x-auto-post.sqlite3')
    frozen={}
    with readonly(auto_path) as auto:
        for row in auto.execute("SELECT execution_queue_id,selection_json FROM x_auto_task WHERE published_at_utc>=? AND execution_queue_id<>''",(start,)):
            value=json.loads(row['selection_json'] or '{}')
            frozen[str(row['execution_queue_id'])]=value
    paths=list(dict.fromkeys([os.getenv('X_POST_DB_PATH','/mnt/data-disk/x-tag-publisher/posts.sqlite3'),ACCOUNT_DB]))
    for source in paths:
        with readonly(source) as posts:
            tables={r[0] for r in posts.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'x_post_publish_log' not in tables:
                continue
            audits={}
            if 'x_tag_queue_selection' in tables:
                for row in posts.execute('SELECT s.queue_id,s.selection_json FROM x_tag_queue_selection s JOIN x_post_queue q ON q.id=s.queue_id WHERE q.created_at>=?',(start,)):
                    audits[str(row[0])]=json.loads(row[1])
            query='''SELECT l.x_post_id,l.published_at,l.x_media_id,q.id queue_id,q.account_id,
                     q.content_id,q.material_language,q.preflight_duration,q.tag
                     FROM x_post_publish_log l JOIN x_post_queue q ON q.id=l.queue_id
                     WHERE l.status='published' AND l.unknown_outcome=0 AND l.x_post_id<>'' AND l.published_at>=?'''
            for row in posts.execute(query,(start,)):
                account=accounts.get(row['account_id'])
                if not account:
                    continue
                selection=frozen.get(str(row['queue_id']),{}) if source!=ACCOUNT_DB else {}
                audit=selection.get('tag_profile') or audits.get(str(row['queue_id']),{})
                metadata=audit.get('classification') or {}
                # Do not infer reliable taxonomy from source tags in other languages.
                if not metadata:
                    metadata={'themes':[],'audience':'__unknown__','label_status':'missing_frozen_classification'}
                catalog=(selection.get('drama') or {}).get('catalog','dramawave')
                db.execute('INSERT OR IGNORE INTO growth_post VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                           (row['x_post_id'],row['account_id'],str(account['x_user_id']),row['content_id'],
                            row['material_language'],catalog,row['published_at'],row['preflight_duration'],
                            store.dumps(metadata),row['x_media_id'],source))

def fill_metadata(accounts):
    """Reuse the verified catalog classifications already held by the revenue reader.

    Revenue itself is not used. That reader also includes published zero-revenue
    dramas, so manual/pool posts can learn without a second SQL collector.
    """
    with store.connect() as db:
        missing=[dict(r) for r in db.execute("SELECT post_id,account_id,content_id,language FROM growth_post WHERE metadata_json LIKE '%missing_frozen_classification%'")]
    ids={r['account_id'] for r in missing}
    if not ids:
        return 0
    from features.x_tag_publish.policy import portrait_snapshot
    data,meta=portrait_snapshot([accounts[aid]['x_user_id'] for aid in ids if aid in accounts])
    if meta.get('unavailable'):
        return 0
    catalog={}
    def visit(nodes):
        for node in nodes:
            for drama in node.get('dramas',[]):
                classification=drama.get('classification') or {}
                if classification.get('themes'):
                    catalog[(str(drama['content_id']),str(drama['language']))]=classification
            visit(node.get('children',[]))
    for profile in data.get('accounts',{}).values():
        visit(profile.get('audiences',[]))
    updated=0
    with store.connect(write=True) as db:
        for row in missing:
            value=catalog.get((row['content_id'],row['language']))
            if value:
                db.execute('UPDATE growth_post SET metadata_json=? WHERE post_id=?',(store.dumps(value),row['post_id']))
                updated+=1
    return updated

def collect(limit_accounts=None):
    now=datetime.now(timezone.utc)
    store.initialize()
    with readonly(ACCOUNT_DB) as source:
        accounts={row['id']:dict(row) for row in source.execute("SELECT * FROM x_authorized_account WHERE status='active' AND publish_approved=1 AND COALESCE(deleted_at,'')=''")}
    report=dict(started_at=now.isoformat(),post_samples=0,follower_samples=0,accounts_ok=0,failures=[])
    with store.connect(write=True) as db:
        discover(db,accounts,now)
    report['labels_filled']=fill_metadata(accounts)
    with store.connect() as db:
        recent=(now-timedelta(days=8)).isoformat()
        snapshots={(r[0],r[1]) for r in db.execute('SELECT s.post_id,s.stage FROM post_snapshot s JOIN growth_post p USING(post_id) WHERE p.published_at>=?',(recent,))}
        latest={r[0]:r[1] for r in db.execute('SELECT account_id,MAX(observed_at) FROM follower_snapshot GROUP BY account_id')}
        retry={r[0].split(':')[1]:json.loads(r[1]) for r in db.execute("SELECT key,value_json FROM collector_state WHERE key LIKE 'retry:%'")}
        posts=[dict(r) for r in db.execute('SELECT * FROM growth_post WHERE published_at>=?',((now-timedelta(days=8)).isoformat(),))]
    due=defaultdict(list)
    active_ids={p['account_id'] for p in posts}
    for post in posts:
        age=(now-instant(post['published_at'])).total_seconds()/3600
        for hours,stage in STAGES:
            if hours<=age<=hours+6 and (post['post_id'],stage) not in snapshots:
                due[post['account_id']].append((post,stage))
    bucket=now.strftime('%Y-%m-%dT')+str(now.hour//6*6).zfill(2)
    selected=sorted(active_ids & set(accounts))
    if limit_accounts is not None:
        selected=selected[:limit_accounts]
    for aid in selected:
        if str(aid) in retry and instant(retry[str(aid)]['until'])>now:
            continue
        followers_due=aid not in latest or now-instant(latest[aid])>=timedelta(hours=6)
        if not due[aid] and not followers_due:
            continue
        account=accounts[aid]
        try:
            token=access_token(account,now)
            if followers_due:
                result=request('https://api.x.com/2/users/me?user.fields=public_metrics',token)['data']
                if str(result['id'])!=str(account['x_user_id']):
                    raise ValueError('account_identity_mismatch')
                count=result.get('public_metrics',{}).get('followers_count')
                if type(count) is int and count>=0:
                    with store.connect(write=True) as db:
                        db.execute('INSERT OR IGNORE INTO follower_snapshot VALUES(?,?,?,?)',(aid,bucket,store.stamp(),count))
                    report['follower_samples']+=1
            for offset in range(0,len(due[aid]),100):
                batch=due[aid][offset:offset+100]
                params={'ids':','.join(p['post_id'] for p,_ in batch),'tweet.fields':'author_id,attachments,public_metrics,non_public_metrics,organic_metrics',
                        'expansions':'attachments.media_keys','media.fields':'type,duration_ms,public_metrics,non_public_metrics,organic_metrics'}
                result=request('https://api.x.com/2/tweets?'+urllib.parse.urlencode(params),token)
                values={str(p['id']):p for p in result.get('data',[])}
                with store.connect(write=True) as db:
                    for post,stage in batch:
                        value=values.get(post['post_id'])
                        if not value:
                            continue
                        if str(value.get('author_id'))!=str(account['x_user_id']):
                            raise ValueError('post_author_mismatch')
                        metrics=extract(value,result.get('includes',{}).get('media',[]))
                        db.execute('INSERT OR IGNORE INTO post_snapshot VALUES(?,?,?,?)',
                                   (post['post_id'],stage,store.stamp(),store.dumps(metrics)))
                        report['post_samples']+=1
            with store.connect(write=True) as db:
                db.execute('DELETE FROM collector_state WHERE key=?',('retry:'+str(aid),))
            report['accounts_ok']+=1
        except Exception as exc:
            code='http_'+str(exc.code) if isinstance(exc,urllib.error.HTTPError) else type(exc).__name__
            attempts=retry.get(str(aid),{}).get('attempts',0)+1
            with store.connect(write=True) as db:
                db.execute('INSERT OR REPLACE INTO collector_state VALUES(?,?)',('retry:'+str(aid),store.dumps(dict(
                    attempts=attempts,until=(now+timedelta(minutes=min(60,5*2**min(attempts,4)))).isoformat(),code=code))))
            report['failures'].append(dict(account_id=aid,code=code))
    with store.connect() as db:
        posts=[]
        for row in db.execute("SELECT p.*,s.stage,s.observed_at,s.metrics_json FROM growth_post p JOIN post_snapshot s USING(post_id) WHERE s.stage='72h' AND p.published_at>=?",((now-timedelta(days=28)).isoformat(),)):
            p=dict(row)
            if p['account_id'] in accounts and p['language']!=accounts[p['account_id']].get('drama_language','en'):
                continue
            p['metrics']=json.loads(p.pop('metrics_json'));p['metadata']=json.loads(p.pop('metadata_json'));posts.append(p)
        followers=defaultdict(list)
        for row in db.execute('SELECT * FROM follower_snapshot WHERE observed_at>=?',((now-timedelta(days=15)).isoformat(),)):
            followers[row['account_id']].append(dict(row))
        previous={r[0]:json.loads(r[1]) for r in db.execute('SELECT account_id,data_json FROM growth_profile')}
    profiles=build_profiles(posts,followers,previous,now)
    report.update(finished_at=store.stamp(),profiles=len(profiles))
    with store.connect(write=True) as db:
        for aid,profile in profiles.items():
            db.execute('INSERT OR REPLACE INTO growth_profile VALUES(?,?,?,?)',(aid,profile['version'],profile['computed_at'],store.dumps(profile)))
        db.execute('INSERT OR REPLACE INTO collector_state VALUES(?,?)',('last_run',store.dumps(report)))
    return report

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--limit-accounts',type=int)
    args=parser.parse_args()
    print(store.dumps(collect(args.limit_accounts)))
