"""Read back deployed recipe behavior, UI, health and restored triggers."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.request

ROOT=Path('/root/drama_material_service')
BACKUP=Path('/mnt/data-disk/random-overlay-gpu/backups/20260920-source-overlay')
sys.path.insert(0,str(ROOT))
from features.drama_synthesis.core import SOURCE_OVERLAY_VERSION,freeze_random_recipe,validate_source_overlay
from features.drama_synthesis.catalog import catalog_from_manifest

def run(args):return subprocess.check_output(args).decode().strip()
pid=run(['systemctl','show','drama-material-api.service','-p','MainPID','--value'])
env=dict(x.decode().split('=',1) for x in Path('/proc/'+pid+'/environ').read_bytes().split(b'\0') if b'=' in x)
catalog=catalog_from_manifest(Path(env['DRAMA_RANDOM_OVERLAY_MANIFEST_FILE']),env['DRAMA_RANDOM_OVERLAY_MANIFEST_SHA256'])
recipe=freeze_random_recipe(job_id='e'*32,content_id='offline-deployment-audit',request={'mode':'auto','source':'concat_video'},catalog=catalog)
assert SOURCE_OVERLAY_VERSION==1
overlay=validate_source_overlay(recipe);assert overlay and 200<=overlay['opacity_bp']<=500 and 11000<=overlay['scale_bp']<=15000
assert validate_source_overlay({}) is None
assert recipe==freeze_random_recipe(job_id='e'*32,content_id='offline-deployment-audit',request={'mode':'auto','source':'concat_video'},catalog=catalog)
app=(ROOT/'static/index.html').read_bytes();public=Path('/usr/share/nginx/html/index.html').read_bytes()
assert app==public and '无声原视频叠层'.encode() in public and '原视频叠层'.encode() in public
with urllib.request.urlopen('https://ai.yingliangads.com/',timeout=20) as response:
    body=response.read();assert '原视频叠层'.encode() in body
with urllib.request.urlopen('https://ai.yingliangads.com/api/ui/topbar',timeout=20) as response:assert response.status==200
health={}
for port,path in [(18788,'/healthz'),(18830,'/health'),(18836,'/health')]:
    with urllib.request.urlopen('http://127.0.0.1:%d%s'%(port,path),timeout=15) as response:health[port]=json.load(response)
    assert health[port]['source_overlay_version']==1
services={u:run(['systemctl','show',u,'-p','ActiveState,MainPID,NRestarts']) for u in ['drama-material-api.service','drama-material-job-worker.service','fb-auto-post-prepare.timer']}
for unit in ['drama-material-api.service','drama-material-job-worker.service']:assert run(['systemctl','is-active',unit])=='active'
report={'ok':True,'sample_recipe':recipe,'health':health,'services':services,'ui_sha256':hashlib.sha256(public).hexdigest(),'public_ui_verified':True,'no_business_job_created':True,'resumed':json.loads((BACKUP/'resumed.json').read_text())}
(BACKUP/'final-audit.json').write_text(json.dumps(report,indent=2));print(json.dumps(report,indent=2))
