"""Read-only release acceptance. Never prints or persists session credentials."""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from features.youtube_analytics.report import parameters,read_catalog,summarize,unique_catalog,visible,empty,add
from features.youtube_analytics.routes import query_metrics


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--start',required=True);parser.add_argument('--end',required=True)
    parser.add_argument('--http',action='store_true')
    args=parser.parse_args()
    root=Path('/root/drama_material_service');db=root/'data/drama_material_jobs.sqlite3'
    config={}
    for line in (root/'.env').read_text().splitlines():
        if line.startswith('ADMIN_MAPPING_MYSQL_') and '=' in line:
            k,v=line.split('=',1);config[k]=v.strip().strip('"').strip("'")
    params=parameters(dict(start=[args.start],end=[args.end],group_by=['owner,drama,channel']))
    catalog=read_catalog(db);metrics=query_metrics(config,params['start'],params['end'])
    unique,_=unique_catalog(catalog);seen=set();source=empty();matched=empty();unmatched=empty()
    for row in metrics:
        key=(row.get('campaign_id') or '',row.get('campaign') or '')
        assert (row['date'],key) not in seen
        seen.add((row['date'],key));add(source,row)
        add(matched if key in unique else unmatched,row)
    for k in source:assert source[k]==matched[k]+unmatched[k]
    tenants=sorted({r['tenant'] for r in unique.values()})
    checks=[]
    for tenant in tenants:
        actor=dict(tenant_key=tenant,user_id='acceptance-readonly',role='admin')
        report=summarize(catalog,metrics,actor,params,'acceptance')
        expected=empty()
        for m in metrics:
            dim=unique.get((m.get('campaign_id') or '',m.get('campaign') or ''))
            if dim and visible(dim,actor):add(expected,m)
        if metrics:
            for k in ('clicks','views','installs','conversions'):assert report['totals'][k]==expected[k]
        checks.append(dict(scope='tenant',groups=report['pagination']['total'],clicks=report['totals']['clicks']))
        for owner in {r['owner'] for r in unique.values() if r['tenant']==tenant}:
            personal=summarize(catalog,metrics,dict(tenant_key=tenant,user_id=owner,role='user'),params,'acceptance')
            assert all(r['owner']==owner for r in personal['all_rows'])
    output=dict(catalog_rows=len(catalog),unique_campaigns=len(unique),source_campaign_days=len(metrics),
                source_totals=source,matched_totals=matched,unmatched_totals=unmatched,conservation=True,scopes=checks)
    if args.http:
        results=[]
        def request(path,cookie=None):
            req=urllib.request.Request('http://127.0.0.1:8787'+path,headers={'Cookie':'drama_admin_session='+cookie} if cookie else {})
            try:
                with urllib.request.urlopen(req,timeout=40) as response:
                    return response.status,response.headers,response.read()
            except urllib.error.HTTPError as exc:return exc.code,exc.headers,exc.read()
        for suffix in ('options','report','export.csv'):
            status,_,_=request('/api/youtube-analytics/'+suffix);assert status==401
        with sqlite3.connect('file:'+str(db)+'?mode=ro',uri=True) as conn:
            sessions=conn.execute('''SELECT s.session_token,s.user_id,s.tenant_key,u.role,u.permissions_json
                FROM drama_admin_session s JOIN drama_admin_user u ON s.user_id=u.user_id AND s.tenant_key=u.tenant_key
                WHERE s.expires_at>? ORDER BY s.expires_at DESC''',(int(time.time()),)).fetchall()
        tested=set()
        for token,owner,tenant,role,raw_permissions in sessions:
            if role in tested:continue
            permissions=json.loads(raw_permissions or '{}')
            if role!='admin' and not permissions.get('youtube_auto_publish'):continue
            status,headers,body=request('/api/youtube-analytics/options',token);assert status==200
            value=json.loads(body)
            if role!='admin':assert all(r['value']==owner for r in value['options']['owner'])
            query='?start='+params['start']+'&end='+params['end']+'&group_by=owner,drama,channel'
            status,headers,body=request('/api/youtube-analytics/report'+query,token);assert status==200
            value=json.loads(body);assert 'no-store' in headers['Cache-Control']
            if role!='admin':assert all(r['owner']==owner for r in value['rows'])
            expected_report=summarize(catalog,metrics,dict(tenant_key=tenant,user_id=owner,role=role),params,'acceptance')
            for k in ('clicks','installs','conversions','revenue'):assert value['totals'][k]==expected_report['totals'][k]
            status,headers,body=request('/api/youtube-analytics/export.csv'+query,token)
            assert status==200 and body.startswith(b'\xef\xbb\xbf') and 'text/csv' in headers['Content-Type']
            status,_,_=request('/api/youtube-analytics/report?start=invalid',token);assert status==400
            results.append(dict(role=role,options=True,report=True,csv=True,invalid_400=True,groups=value['pagination']['total']))
            tested.add(role)
        assert results,'No existing authorized session available for HTTP acceptance'
        output['http']=dict(anonymous_401=True,authenticated=results)
    print(json.dumps(output,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
