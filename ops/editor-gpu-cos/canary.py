"""Isolated real NVENC -> private COS -> metadata callback; never creates production tasks."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import threading
import types
from urllib.error import HTTPError
from urllib.request import Request, urlopen

REPO=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(REPO))
from features.ai_video_editing import output_storage, render_worker
from features.ai_video_editing.cloud_uploads import CloudUploads
from features.ai_video_editing.runtime import Runtime


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--work',type=Path,required=True);a=p.parse_args()
    a.work.mkdir(parents=True,exist_ok=False,mode=0o700)
    rt=Runtime(a.work/'metadata-api');client,bucket=output_storage.worker_client()
    rt.cloud=CloudUploads(rt,client,bucket,'ap-hongkong')
    source=a.work/'fixture.mp4'
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=blue:s=180x320:r=30:d=1',
                    '-f','lavfi','-i','sine=frequency=500:duration=1','-c:v','libx264','-preset','ultrafast',
                    '-c:a','aac','-t','1',str(source)],check=True)
    actor={'tenant_key':'isolated-gpu-cos','user_id':'canary'}
    asset=rt.import_asset(source,output_storage.file_digest(source),'fixture.mp4','',actor)
    project=rt.save(actor,{'operation_id':'gpu_cos_canary','name':'Isolated canary','units':[
        {'id':'fixture','title':'Isolated canary','clips':[{'source_id':asset['id'],'start_ms':0,'end_ms':1000}]}]})['item']
    rt.render(project['id'],actor,{'operation_id':'canary_render','expected_version':1})
    spec=importlib.util.spec_from_file_location('features.ai_video_editing._canary_http',REPO/'features/ai_video_editing/http_api.py')
    module=importlib.util.module_from_spec(spec)
    sys.modules['app']=types.SimpleNamespace(DramaMaterialHandler=BaseHTTPRequestHandler)
    spec.loader.exec_module(module)
    class Handler(module.Handler):
        def log_message(self,*args):pass
    with ThreadingHTTPServer(('127.0.0.1',0),Handler) as http:
        http.runtime=rt;http.worker_token='isolated-local-fixture'
        thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
        gpu=a.work/'gpu';gpu.mkdir()
        try:
            api=render_worker.InternalAPI('http://127.0.0.1:%d/api/ai-video-editing/internal'%http.server_port,
                                         http.worker_token,output_store=(client,bucket))
            job=api.post('/claim')['item']
            render_worker.run_job(api,job,gpu,'h264_nvenc','ffmpeg','ffprobe')
            item=rt.list_records('tasks',actor,job['id'])['item']
            assert item['status']=='completed' and item['output']['storage']=='cos',item
            assert item['output']['preview_available'],'GPU preview missing'
            assert not list((rt.root/'outputs').glob('*.mp4')),'media crossed metadata callback'
            assert not list(gpu.glob('render-*')),'unfinished GPU receipt'
            location=output_storage.location(rt,job['id'],preview=True)
            assert location['sha256']!=item['output']['metadata']['sha256']
            with urlopen(Request(location['url'],headers={'Range':'bytes=0-31'}),timeout=30) as response:
                assert response.status==206 and len(response.read())==32
            try:
                urlopen(Request(location['url'].split('?')[0],headers={'Range':'bytes=0-31'}),timeout=30)
                raise AssertionError('unsigned private media was readable')
            except HTTPError as exc:
                assert exc.code==403
            result={'task':job['id'],'nvenc':True,'cos_verified':True,'range_status':206,
                    'unsigned_status':403,'metadata_only_callback':True,'size':location['size']}
            result['gpu_preview_verified']=True
            (a.work/'receipt.json').write_text(json.dumps(result,indent=2))
            print(json.dumps(result))
        finally:http.shutdown();thread.join(2)


if __name__=='__main__':main()
