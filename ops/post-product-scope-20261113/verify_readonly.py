"""Read-only source acceptance: no queue, link, credential refresh or platform writes."""
import argparse
import json
from pathlib import Path
import sys
from types import SimpleNamespace

parser=argparse.ArgumentParser()
parser.add_argument('--root',required=True)
parser.add_argument('--lane',choices=('main','fb','tt','tt_auto'),required=True)
parser.add_argument('--material',action='append',required=True)
args=parser.parse_args()
sys.path.insert(0,args.root)
import pymysql
env={}
for line in Path('/root/drama_material_service/.env').read_text().splitlines():
    if line.strip() and not line.lstrip().startswith('#') and '=' in line:
        key,value=line.split('=',1);env[key.strip()]=value.strip().strip('\"').strip("'")

def connect():
    c=pymysql.connect(host='101.32.56.53',port=63350,user=env.get('ADMIN_MAPPING_MYSQL_USER',env.get('MYSQL_USER')),
        password=env.get('ADMIN_MAPPING_MYSQL_PASSWORD',env.get('MYSQL_PASSWORD')),database='kunlunads_dev',
        connect_timeout=3,read_timeout=12,cursorclass=pymysql.cursors.DictCursor)
    with c.cursor() as q:
        q.execute('SELECT @@read_only ro');assert q.fetchone()['ro']==1
        q.execute('SET SESSION MAX_EXECUTION_TIME=8000')
        q.execute('START TRANSACTION READ ONLY')
    return c

class SQL:
    schema='kunlunads_dev';blacklist_schema='ads_setting'
    def select(self,sql,params):
        if not sql.lstrip().upper().startswith('SELECT'):raise ValueError('Read only')
        if args.lane=='fb':
            from features.fb_video_publish.query_broker import _validate_sources
            _validate_sources(sql)
        c=connect()
        try:
            with c.cursor() as q:q.execute(sql,params);return list(q.fetchall())
        finally:c.close()

sql=SQL()
from features.publishing_products import source_app_sql
identity=sql.select('SELECT CAST(s.id AS CHAR) material_id,s.product,s.data_source_id,'+
    source_app_sql('s',sql.schema)+' app_id FROM `kunlunads_dev`.ads_custom_source s WHERE s.id IN ('+
    ','.join('%s' for _ in args.material)+')',args.material)
print(json.dumps({'identities':identity},ensure_ascii=False,default=str))
if args.lane=='main':
    from features.x_video_publish.sources import SourceRepository
    result=SourceRepository(sql).resolve(args.material)
    safe={mid:{k:v for k,v in row.items() if k in ('material_id','content_id','language','drama_name','duration_seconds')}
          for mid,row in result['items'].items()}
    print(json.dumps({'lane':'x-main-source','items':safe,'errors':result['errors']},ensure_ascii=False,default=str))
    assert len(result['items'])==len(args.material),result['errors']
    from features.youtube_auto_publish.source import MaterialSource
    from features.youtube_auto_publish.drama import DramaMetadataResolver
    def query(statement):
        c=connect()
        try:
            with c.cursor() as q:q.execute(statement);return [tuple(row.values()) for row in q.fetchall()]
        finally:c.close()
    source=MaterialSource(str(Path(__file__).with_name('youtube-material-source.sql')),query,drama_resolver=DramaMetadataResolver(query))
    for mid in args.material:
        row=source.get(mid)
        print(json.dumps({'lane':'youtube-source','material_id':mid,'app_id':row['app_id'],'drama_status':row['drama_status'],'name':row['macro_name']},ensure_ascii=False))
        assert row['drama_status']=='matched'
elif args.lane=='fb':
    from features.fb_video_publish.sources import SourceRepository
    source=SourceRepository(None,SimpleNamespace(mysql=sql))
    result=source.resolve_materials(args.material,need_desc=True,enforce_blacklist=False,manual=True)
    print(json.dumps({'lane':'fb-source','items':[{k:v for k,v in row.items() if k in ('material_id','content_id','language','drama_name')} for row in (result['items'].values() if isinstance(result['items'],dict) else result['items'])], 'errors':result['errors']},ensure_ascii=False,default=str))
    assert len(result['items'])==len(args.material),result['errors']
else:
    from features.tt_posts.service import DramawaveMaterialResolver
    source=DramawaveMaterialResolver(connect)
    failed=[]
    for mid in args.material:
        try:
            row=source.resolve(mid)
            print(json.dumps({'lane':args.lane,'material_id':mid,'status':'resolved','name':row.get('drama_name')},ensure_ascii=False))
        except Exception as exc:
            code=getattr(exc,'code',type(exc).__name__)
            print(json.dumps({'lane':args.lane,'material_id':mid,'status':'rejected','code':code,'message':str(exc)[:250]},ensure_ascii=False))
            failed.append(code)
    assert not failed,failed
