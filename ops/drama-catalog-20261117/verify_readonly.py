"""Read-only production-source acceptance; never creates queues or short links."""
import argparse, importlib, importlib.util, json, subprocess, sys
from pathlib import Path
from types import SimpleNamespace

p=argparse.ArgumentParser()
p.add_argument('--root', required=True)
p.add_argument('--lane', choices=('main','x','fb','tt','tt_auto'), required=True)
p.add_argument('--overlay', type=Path)
p.add_argument('--material', action='append', required=True)
p.add_argument('--youtube-sql', type=Path)
args=p.parse_args()
sys.path.insert(0,args.root)
if args.overlay:
    paths=['features/publishing_products.py','features/x_posts/selector.py',
           'features/fb_video_publish/sources.py','features/fb_video_publish/group_sources.py',
           'features/youtube_auto_publish/source.py']
    for rel in paths:
        file=args.overlay/rel
        if not file.exists():continue
        name=rel[:-3].replace('/','.')
        parent=importlib.import_module(name.rsplit('.',1)[0])
        spec=importlib.util.spec_from_file_location(name,file)
        module=importlib.util.module_from_spec(spec)
        sys.modules[name]=module
        spec.loader.exec_module(module)
        setattr(parent,name.rsplit('.',1)[1],module)

pid=subprocess.check_output(['systemctl','show','drama-material-api.service','-p','MainPID','--value'],text=True).strip()
env={k.decode():v.decode() for k,v in (x.split(b'=',1) for x in Path('/proc/'+pid+'/environ').read_bytes().split(b'\0') if b'=' in x)}
import pymysql
prefix='ADMIN_MAPPING_MYSQL_'
assert env[prefix+'HOST']=='101.32.56.53' and int(env[prefix+'PORT'])==63350
def connect():
    c=pymysql.connect(host=env[prefix+'HOST'],port=63350,user=env[prefix+'USER'],password=env[prefix+'PASSWORD'],
        database=env[prefix+'DATABASE'],connect_timeout=5,read_timeout=12,write_timeout=5,
        charset='utf8mb4',cursorclass=pymysql.cursors.DictCursor)
    with c.cursor() as q:
        q.execute('SELECT @@read_only ro');assert q.fetchone()['ro']==1
        q.execute('SET SESSION MAX_EXECUTION_TIME=8000');q.execute('START TRANSACTION READ ONLY')
    return c

class SQL:
    schema=env[prefix+'DATABASE'];blacklist_schema='ads_setting'
    def select(self,sql,params=()):
        assert sql.lstrip().upper().startswith('SELECT')
        c=connect()
        try:
            with c.cursor() as q:q.execute(sql,params or None);return list(q.fetchall())
        finally:c.close()

db=SQL()
from features.publishing_products import source_app_sql
identities=db.select('SELECT CAST(s.id AS CHAR) material_id,s.product,s.data_source_id,'+
    source_app_sql('s',db.schema)+' app_id FROM `'+db.schema+'`.ads_custom_source s WHERE s.id IN ('+
    ','.join('%s' for _ in args.material)+')',args.material)
print(json.dumps({'identities':identities},ensure_ascii=False))
def safe(items):
    return [{k:v for k,v in item.items() if k in ('material_id','content_id','language','drama_name','material_language','moboreels_story_id')}
            for item in items]
if args.lane in ('main','x'):
    from features.x_video_publish.sources import SourceRepository
    result=SourceRepository(db).resolve(args.material)
    print(json.dumps({'lane':'x','items':safe(result['items'].values()),'errors':result['errors']},ensure_ascii=False))
    assert len(result['items'])==len(args.material),result['errors']
elif args.lane=='fb':
    from features.fb_video_publish.sources import SourceRepository
    from features.fb_video_publish.query_broker import _validate_sources
    class FBSQL(SQL):
        def select(self,sql,params=()):
            _validate_sources(sql)
            return super().select(sql,params)
    source=SourceRepository(None,SimpleNamespace(mysql=FBSQL()))
    result=source.resolve_materials(args.material,need_desc=True,enforce_blacklist=False,manual=True)
    print(json.dumps({'lane':'fb','items':safe(result['items'].values()),'errors':result['errors']},ensure_ascii=False))
    assert len(result['items'])==len(args.material),result['errors']
else:
    from features.tt_posts.service import DramawaveMaterialResolver
    source=DramawaveMaterialResolver(connect)
    results=[source.resolve(mid) for mid in args.material]
    print(json.dumps({'lane':args.lane,'items':safe(results)},ensure_ascii=False))

if args.youtube_sql:
    from features.youtube_auto_publish.source import MaterialSource
    from features.youtube_auto_publish.drama import DramaMetadataResolver
    def query(sql):return [tuple(row.values()) for row in db.select(sql)]
    source=MaterialSource(str(args.youtube_sql),query,drama_resolver=DramaMetadataResolver(query))
    for mid in args.material:
        row=source.get(mid)
        print(json.dumps({'lane':'youtube','material_id':mid,'app_id':row['app_id'],
            'drama_status':row['drama_status'],'name':row['macro_name']},ensure_ascii=False))
        assert row['drama_status'] in ('matched','moboreels')
