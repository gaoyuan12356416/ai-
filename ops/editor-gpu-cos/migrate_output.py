"""Run on the GPU: copy one existing completed film to private COS, then attach its receipt."""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from features.ai_video_editing.render_worker import InternalAPI, fingerprint, require
from features.ai_video_editing.output_storage import object_key, upload, upload_bundle, worker_client


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--task',required=True)
    p.add_argument('--work',type=Path,required=True)
    p.add_argument('--api',default='https://ai.yingliangads.com/api/ai-video-editing/internal')
    p.add_argument('--prepare-preview',action='store_true')
    a=p.parse_args()
    token=Path('/etc/ai-video-editing/worker.token').read_text().strip()
    api=InternalAPI(a.api,token)
    with api.open('/tasks/'+a.task+'/output-location') as r:loc=json.loads(r.read(65536))
    object_key(a.task,loc['sha256'])
    if loc.get('url') and not a.prepare_preview:
        print(json.dumps({'task':a.task,'already_on_cos':True}));return
    a.work.mkdir(parents=True,exist_ok=True,mode=0o700)
    target=a.work/(a.task+'.mp4')
    if not target.exists():
        partial=target.with_suffix('.part')
        if loc.get('url'):
            from features.ai_video_editing.cloud_media import download
            download(loc,partial,output=True)
        else:
            with api.open('/tasks/'+a.task+'/output-source',timeout=120) as r,partial.open('wb') as out:
                require(int(r.headers.get('Content-Length',0))==loc['size'],'迁移来源大小不符')
                size=0
                while True:
                    block=r.read(1024**2)
                    if not block:break
                    size+=len(block);require(size<=loc['size'],'迁移来源超出回执');out.write(block)
            require(size==loc['size'] and fingerprint(partial)==loc['sha256'],'迁移文件校验失败')
        os.replace(partial,target)
    body=(upload_bundle(*worker_client(),a.task,target,loc['sha256'],require_preview=True) if a.prepare_preview else
          upload(*worker_client(),a.task,target,loc['sha256']))
    with api.open('/tasks/'+a.task+'/attach-output',data=json.dumps(body).encode(),
                  headers={'Content-Type':'application/json'},timeout=60) as r:
        require(json.loads(r.read(65536)).get('ok'),'成片回执待核对')
    receipt={'task':a.task,'size':body['size'],'sha256':loc['sha256'],'gpu_upload_verified':True}
    if body.get('preview'):receipt['preview']=body['preview']
    (a.work/(a.task+'.receipt.json')).write_text(json.dumps(receipt,indent=2))
    print(json.dumps(receipt))


if __name__=='__main__':main()
