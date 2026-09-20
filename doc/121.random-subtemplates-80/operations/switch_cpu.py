import hashlib,json,os,signal,subprocess,time,urllib.request
from pathlib import Path
SHA='b5df776a88bdfa961e60f60d6076c6915f37c5fd5ed8ade973dc4be7205eea6c'
PREVIOUS='24d6fad3174f765d3433c9ab71322f04241b11acf0224a4e1392f138295adf0c'
B=Path('/mnt/data-disk/random-overlay-gpu/backups/20260920-subtemplates80')
def run(args,timeout=120):return subprocess.check_output(args,timeout=timeout).decode().strip()
health={}
for port,path in [(18788,'/healthz'),(18830,'/health'),(18836,'/health')]:
 deadline=time.monotonic()+90
 while True:
  try:
   with urllib.request.urlopen('http://127.0.0.1:%d%s'%(port,path),timeout=10) as response:health[port]=json.load(response)
   break
  except Exception:
   if time.monotonic()>=deadline:raise
   time.sleep(2)
assert health[18788]['release_sha']=='9c4c3cb4eca260d46df8ab23443a32cda667c20c'
assert health[18830]['random_overlay_asset_set_sha256']==SHA
manifest='/mnt/data-disk/drama-synthesis-catalog/catalog-'+SHA[:12]+'/manifest.json'
assert hashlib.sha256(Path(manifest).read_bytes()).hexdigest()==SHA
envfile=Path('/etc/drama-synthesis/cpu.env')
before=envfile.read_bytes();assert before==(B/'cpu.env.before').read_bytes(),'CPU config drift'
lines=before.decode().splitlines();replaced=set()
for i,line in enumerate(lines):
 if line.startswith('DRAMA_RANDOM_OVERLAY_MANIFEST_FILE='):
  lines[i]='DRAMA_RANDOM_OVERLAY_MANIFEST_FILE='+manifest;replaced.add('file')
 if line.startswith('DRAMA_RANDOM_OVERLAY_MANIFEST_SHA256='):
  assert line.split('=',1)[1]==PREVIOUS
  lines[i]='DRAMA_RANDOM_OVERLAY_MANIFEST_SHA256='+SHA;replaced.add('sha')
assert replaced=={'file','sha'}
tmp=envfile.with_name('cpu.env.subtemplates80-new');assert not tmp.exists()
tmp.write_text('\n'.join(lines)+'\n');tmp.chmod(envfile.stat().st_mode&0o777);os.replace(str(tmp),str(envfile))
for u in ['drama-material-api.service','drama-material-job-worker.service']:
 subprocess.run(['systemctl','restart',u],check=True,timeout=120)
 assert run(['systemctl','is-active',u])=='active'
 pid=run(['systemctl','show',u,'-p','MainPID','--value'])
 env=dict(x.decode().split('=',1) for x in Path('/proc/'+pid+'/environ').read_bytes().split(b'\0') if b'=' in x)
 assert env['DRAMA_RANDOM_OVERLAY_MANIFEST_SHA256']==SHA
 python=os.readlink('/proc/'+pid+'/exe')
 code="import sys,json;sys.path.insert(0,'/root/drama_material_service');from features.drama_synthesis.catalog import catalog_from_manifest;c=catalog_from_manifest("+repr(manifest)+","+repr(SHA)+");assert {k:len(v) for k,v in c['categories'].items()}=={'border':20,'opacity_video':20,'corners':20,'tint':20};print('80 CPU catalog assets verified')"
 run([python,'-c',code])
deadline=time.monotonic()+90
while True:
 try:
  with urllib.request.urlopen('https://ai.yingliangads.com/api/ui/topbar',timeout=10) as response:assert response.status==200
  break
 except Exception:
  if time.monotonic()>=deadline:raise
  time.sleep(2)
pause=json.loads((B/'fb-claim-pause.json').read_text());pid=pause['pid']
if pause.get('stop_requested') or pause['paused']:
 proc=Path('/proc/%d/stat'%pid)
 if proc.exists():
  assert proc.read_text().rsplit(')',1)[1].split()[19]==pause['start_ticks']
  assert int(run(['systemctl','show',pause['unit'],'-p','MainPID','--value']))==pid
  os.kill(pid,signal.SIGCONT);pause['resumed']=True
 else:pause['resumed']='process_already_exited'
else:pause['resumed']='no_client_was_running'
(B/'fb-claim-resume.json').write_text(json.dumps(pause,indent=2))
script='/mnt/data-disk/random-overlay-gpu/backups/20260908-cache/maintenance.py'
for action in ['resume','gate-off']:subprocess.run(['python3',script,action,'tt','--apply'],check=True,timeout=90)
timer=json.loads((B/'fb-prepare-timer.json').read_text())
if timer['active_state']=='active':subprocess.run(['systemctl','start','fb-auto-post-prepare.timer'],check=True,timeout=30)
out={'ok':True,'health':health,'cpu_default_sha256':SHA,'fb_claims_resumed':pause['resumed'],'fb_prepare_timer':run(['systemctl','show','fb-auto-post-prepare.timer','-p','ActiveState','--value']),'services':{u:run(['systemctl','show',u,'-p','ActiveState,MainPID,NRestarts']) for u in ['drama-material-api.service','drama-material-job-worker.service']}}
(B/'switch-after.json').write_text(json.dumps(out,indent=2));print(json.dumps(out,indent=2))
