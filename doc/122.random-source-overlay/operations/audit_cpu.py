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
control=Path('/mnt/data-disk/migrations/gpu-service-migration-20260828T1502/control')
triggers=json.loads((control/'tt-triggers.json').read_text())
assert triggers['restored'] and triggers['phase']=='restored'
trigger_states={}
for unit,before in triggers['original'].items():
    current=run(['systemctl','show',unit,'-p','ActiveState','--value'])
    assert current==before,(unit,before,current)
    trigger_states[unit]=current
assert 'tt' not in json.loads((control/'gates.json').read_text())['groups']
timer_before=json.loads((BACKUP/'fb-prepare-timer.json').read_text())['active_state']
assert run(['systemctl','show','fb-auto-post-prepare.timer','-p','ActiveState','--value'])==timer_before
pauses=[json.loads((BACKUP/'fb-claim-resume.json').read_text())]+json.loads((BACKUP/'extra-prepare-clients.json').read_text())
restored_clients=[]
for row in pauses:
    assert row.get('resumed'),row['pid']
    stat=Path('/proc/%d/stat'%row['pid'])
    if stat.exists():
        fields=stat.read_text().rsplit(')',1)[1].split()
        if fields[19]==row['start_ticks']:assert fields[0] not in ['T','t'],row['pid']
    restored_clients.append(row['pid'])
report={'ok':True,'sample_recipe':recipe,'health':health,'services':services,'ui_sha256':hashlib.sha256(public).hexdigest(),'public_ui_verified':True,'no_business_job_created':True,'resumed':json.loads((BACKUP/'resumed.json').read_text()),'tt_triggers':trigger_states,'tt_gate_removed':True,'restored_prepare_clients':restored_clients}
(BACKUP/'final-audit.json').write_text(json.dumps(report,indent=2));print(json.dumps(report,indent=2))
