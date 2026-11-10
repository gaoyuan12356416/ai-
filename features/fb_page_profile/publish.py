"""Explicit one-shot writer and COS publisher for one verified frozen snapshot.

No generic SQL interface, scheduler, production service mutation, or automatic retry.
All verification reads use 63350. DDL must be explicitly requested with --create-table.
"""
import argparse
import hashlib
import json
from pathlib import Path
import time
import pymysql

TABLE='ads_ai.fb_page_effect_profile'

def envfile(path):
    result={}
    for line in Path(path).read_text().splitlines():
        if line.strip() and not line.lstrip().startswith('#') and '=' in line:
            k,v=line.split('=',1);result[k.strip()]=v.strip().strip('"').strip("'")
    return result

def connect(env,writer=False):
    return pymysql.connect(host='101.32.56.53',port=63353 if writer else 63350,
        user=env['ADMIN_MAPPING_MYSQL_USER'],password=env['ADMIN_MAPPING_MYSQL_PASSWORD'],
        database='ads_ai',charset='utf8mb4',connect_timeout=3,read_timeout=10,write_timeout=10,
        cursorclass=pymysql.cursors.DictCursor,autocommit=False)

def encode(x):return json.dumps(x,ensure_ascii=False,sort_keys=True,separators=(',',':'))

def publish(args):
    env=envfile(args.env);data=json.loads(Path(args.snapshot).read_text(encoding='utf-8'))
    version=data['meta']['version'];pages=data['pages']
    assert len(pages)==236 and len({p['page_id'] for p in pages})==236
    assert data['meta']['group_id']==62
    payloads={p['page_id']:encode(p) for p in pages}
    hashes={k:hashlib.sha256(v.encode()).hexdigest() for k,v in payloads.items()}
    # Scope/schema proof on reader, before opening writer. Existing same-version
    # rows are reconciled; conflicting or partial outcomes are never overwritten.
    with connect(env) as read:
        with read.cursor() as q:
            q.execute('SELECT @@read_only AS ro');assert q.fetchone()['ro']==1
            q.execute("SHOW TABLES LIKE 'fb_page_effect_profile'");exists=bool(q.fetchall())
            if exists:
                q.execute('SELECT page_id,payload_sha256 FROM '+TABLE+' WHERE profile_version=%s',(version,))
                existing={r['page_id']:r['payload_sha256'] for r in q.fetchall()}
            else:existing={}
    if existing:
        assert existing==hashes,'snapshot_exists_but_does_not_match_full_expected_payload'
    else:
        assert exists or args.create_table,'explicit_create_table_flag_required'
        with connect(env,True) as writer:
            with writer.cursor() as q:
                q.execute('SELECT DATABASE() AS db,@@read_only AS ro');proof=q.fetchone()
                assert proof==dict(db='ads_ai',ro=0)
                if not exists:q.execute(Path(__file__).with_name('schema.sql').read_text())
                for p in pages:
                    summary={k:v for k,v in p.items() if k not in ('dramas','tags')}
                    summary['meta']=data['meta']
                    q.execute('INSERT INTO '+TABLE+' (profile_version,group_id,page_id,language,window_start,window_end,window_kind,spend_cents,revenue_d0_cents,summary_json,profile_json,payload_sha256) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                              (version,62,p['page_id'],p['language'],p['window']['start'],p['window']['end'],p['window']['kind'],p['spend'],p['af_revenue0'],encode(summary),payloads[p['page_id']],hashes[p['page_id']]))
            writer.commit()
    verified=False
    for attempt in range(5):
        with connect(env) as read:
            with read.cursor() as q:
                q.execute('SELECT @@read_only AS ro');assert q.fetchone()['ro']==1
                q.execute('SELECT page_id,spend_cents,revenue_d0_cents,SHA2(profile_json,256) AS payload_sha256 FROM '+TABLE+' WHERE profile_version=%s',(version,))
                rows=q.fetchall()
        verified={r['page_id']:r['payload_sha256'] for r in rows}==hashes
        if verified:break
        time.sleep(2)
    assert verified,'reader_verification_pending_do_not_retry_writes'
    assert sum(r['spend_cents'] for r in rows)==data['meta']['audit']['spend']
    assert sum(r['revenue_d0_cents'] for r in rows)==data['meta']['audit']['af_revenue0']
    receipt=dict(version=version,table=TABLE,verified_rows=len(rows),spend_cents=sum(r['spend_cents'] for r in rows),revenue_d0_cents=sum(r['revenue_d0_cents'] for r in rows))
    Path(args.receipt).write_text(encode(receipt),encoding='utf-8')
    if args.html:
        from qcloud_cos import CosConfig,CosS3Client
        body=Path(args.html).read_bytes();digest=hashlib.sha256(body).hexdigest()
        key=env.get('COS_PREFIX','drama-materials').strip('/')+'/reports/fb-page-effect-profile/'+version+'/index.html'
        client=CosS3Client(CosConfig(Region=env['COS_REGION'],SecretId=env['COS_SECRET_ID'],SecretKey=env['COS_SECRET_KEY'],Scheme='https',Timeout=60))
        # Fixed version key + preflight HEAD reconciles an earlier unknown upload.
        found=None
        try:found=client.head_object(Bucket=env['COS_BUCKET'],Key=key)
        except Exception as exc:
            if not hasattr(exc,'get_status_code') or str(exc.get_status_code())!='404':raise
        if found:
            assert found.get('x-cos-meta-sha256')==digest,'existing_cos_object_differs'
        else:
            client.put_object(Bucket=env['COS_BUCKET'],Key=key,Body=body,
                              ContentType='text/html; charset=utf-8',CacheControl='private, max-age=300',
                              Metadata={'x-cos-meta-sha256':digest})
        response=client.get_object(Bucket=env['COS_BUCKET'],Key=key)
        returned=response['Body'].get_raw_stream().read()
        assert hashlib.sha256(returned).hexdigest()==digest
        url='https://'+env['COS_DOMAIN'].strip().strip('/')+'/'+key
        receipt.update(cos_url=url,cos_key=key,html_sha256=digest,html_bytes=len(body))
        Path(args.receipt).write_text(encode(receipt),encoding='utf-8')
    return receipt

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--snapshot',required=True);p.add_argument('--html');p.add_argument('--receipt',required=True)
    p.add_argument('--env',default='/root/drama_material_service/.env');p.add_argument('--create-table',action='store_true');args=p.parse_args()
    print(json.dumps(publish(args),ensure_ascii=False))
