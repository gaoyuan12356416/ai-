#!/usr/bin/env python3
"""Isolated real media/GPU verification. X upload/Post requests are always simulated."""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def service_env(name):
    pid = subprocess.check_output(['systemctl','show',name,'--property=MainPID','--value'],text=True).strip()
    assert pid != '0'
    for field in Path('/proc/'+pid+'/environ').read_bytes().split(b'\0'):
        key, sep, value = field.partition(b'=')
        if sep: os.environ[key.decode()] = value.decode()


def fixture(destination, duration):
    service_env('x-post-media-repair.service')
    from features.x_posts import media_repair as m
    config=m.WorkerConfig.from_env()
    destination.mkdir(parents=True,exist_ok=True,mode=0o700)
    path=destination/('source-%s.mp4' % duration)
    subprocess.run([config.ffmpeg_bin,'-y','-nostdin','-v','error','-f','lavfi','-i',
        'testsrc2=size=720x1280:rate=30:duration='+str(duration),'-f','lavfi','-i',
        'sine=frequency=440:sample_rate=48000:duration='+str(duration),'-c:v','h264_nvenc',
        '-profile:v','high','-pix_fmt','yuv420p','-b:v','1500k','-c:a','aac','-ar','48000','-ac','2',
        '-movflags','+faststart','-t',str(duration),str(path)],capture_output=True,check=True,timeout=240)
    sha,size=m._file_sha256(path)
    cos=m.CosObjectStore(config)
    key=config.cos_prefix+'/random-template-validation/'+sha+'.mp4'
    cos.upload(key,path,sha,size)
    value={'url':cos.url(key),'sha256':sha,'size':size,'duration':duration,'synthetic':True}
    (destination/('source-%s.json'%duration)).write_text(json.dumps(value,indent=2))
    print(json.dumps(value),flush=True)


def chain(source_url, destination, premium=False):
    service_env('x-post-automation.service')
    # Use the deployed GPU catalog, independent of whether CPU cutover happened yet.
    os.environ['X_POST_RANDOM_TEMPLATE_MANIFEST_SHA256']='b5df776a88bdfa961e60f60d6076c6915f37c5fd5ed8ade973dc4be7205eea6c'
    from features.x_posts import service as s
    from features.x_posts.random_template import prepare_random_template
    from features.x_posts.random_template_store import PreparationStore
    from scripts.test_x_post_priority_manual_store import material_candidate,ACTOR
    from scripts.test_x_posts import ScriptedHttpClient,response
    from urllib.parse import urlsplit
    destination.mkdir(parents=True,exist_ok=False,mode=0o700)
    hosts=tuple(dict.fromkeys((urlsplit(source_url).hostname,'advertising-1306474899.cos.ap-hongkong.myqcloud.com','img.tianmai.cn','socialkit-cdn.yingliang.tech')))
    source=s.download_media(source_url,destination/'source.mp4',hosts,timeout=120)
    probe=s.probe_media(source['path'],timeout=120,max_duration_seconds=14400 if premium else 140)
    db=destination/'isolated.sqlite3';store=s.XPostStore(db)
    with contextlib.closing(sqlite3.connect(db)) as conn:
        conn.execute("INSERT OR REPLACE INTO sqlite_sequence(name,seq) VALUES('x_post_queue',9000000000000000000)");conn.commit()
    run=store.create_manual_run(['501'],[2],'isolated-gpu-'+source['sha256'][:32],ACTOR,random_template_enabled=True)
    store.claim_manual_run();item=material_candidate(run,2,'501')
    item.update(material_url=source_url,preflight_sha256=source['sha256'],preflight_size=source['size'],preflight_duration=probe['duration'])
    store.create_manual_plan(run['id'],[item])
    with contextlib.closing(sqlite3.connect(db)) as conn:qid=conn.execute('SELECT id FROM x_post_queue').fetchone()[0]
    queue=store.get_queue(qid);log=store.reserve_log(qid)
    account={'id':2,'username':queue['account_username'],'long_video_publish_eligible':premium,'subscription_type':'premium' if premium else 'none','protected':False}
    kwargs=dict(queue=queue,log=log,account=account,db_path=db,public_root=destination/'public',allowed_media_hosts=hosts,timeout=120)
    print(json.dumps({'stage':'render_requested','duration':probe['duration'],'queue_id':qid}),flush=True)
    with prepare_random_template(**kwargs) as prepared:
        import shutil
        shutil.copy2(prepared.media['path'],destination/'output.mp4')
    record=PreparationStore(db).get(qid)
    # Repeat preparation must reuse frozen recipe/bytes; never call the GPU again.
    with mock.patch('features.x_posts.random_template.RandomTemplateClient.prepare',side_effect=AssertionError('unexpected GPU remake')):
        with prepare_random_template(**kwargs) as prepared:
            segments=(Path(prepared.media['path']).stat().st_size+s.DEFAULT_CHUNK_BYTES-1)//s.DEFAULT_CHUNK_BYTES
            http=ScriptedHttpClient([response(200,{'data':{'id':'fixture-media'}})]
                +[response(200,{'data':{'expires_at':1}}) for _ in range(segments)]
                +[response(200,{'data':{'id':'fixture-media'}}),response(201,{'data':{'id':'190002','text':'fixture only'}})])
            result=s.publish_canary(db_path=db,queue_id=qid,account=account,access_token='fixture-never-used-on-network',
                public_root=destination/'public',short_base_url='https://gy.g2flow.com/s2l',allowed_media_hosts=hosts,
                http_client=http,prepared_media=prepared,sleeper=lambda _:None,timeout=120)
    assert result['status']=='published'
    streams=json.loads(subprocess.check_output(['/usr/bin/ffprobe','-v','error','-show_streams','-show_format','-of','json',str(destination/'output.mp4')],text=True))
    timing=[{'type':x['codec_type'],'start':float(x.get('start_time',0)), 'duration':float(x.get('duration',probe['duration']))} for x in streams['streams']]
    assert abs(timing[0]['duration']-timing[1]['duration'])<=0.05
    assert abs(timing[0]['start']-timing[1]['start'])<=0.05
    evidence={'source_duration':probe['duration'],'output_sha256':record['result']['output_sha256'],
        'output_size':record['result']['output_size'],'probe':record['result']['probe'],'recipe':record['result']['recipe'],
        'source_url':source_url,'output_url':record['result']['output_url'],'av_timing':timing,
        'real_x_posts':0,'simulated_x_requests':len(http.requests),'repeat_reused':True}
    (destination/'result.json').write_text(json.dumps(evidence,indent=2))
    print(json.dumps(evidence),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--fixture',action='store_true');p.add_argument('--duration',type=float,default=12.5)
    p.add_argument('--source-url');p.add_argument('--premium',action='store_true');p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    if a.fixture:fixture(a.output,a.duration)
    else:chain(a.source_url,a.output,a.premium)
