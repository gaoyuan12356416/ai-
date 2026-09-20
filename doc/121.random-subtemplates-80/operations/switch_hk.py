import hashlib,json,os,shlex,shutil,subprocess,time,urllib.request
from pathlib import Path
SHA='b5df776a88bdfa961e60f60d6076c6915f37c5fd5ed8ade973dc4be7205eea6c'
PREVIOUS='24d6fad3174f765d3433c9ab71322f04241b11acf0224a4e1392f138295adf0c'
ORIGINAL='028326ab211418934b026c227f2e3707553cce7560551dca3c0bfddc681d566f'
B=Path('/data/random-overlay-gpu/backups/20260920-subtemplates80')
units=['drama-synthesis-gpu-worker.service','tt-gpu-publisher.service','fb-page-random-overlay-gpu.service']
specs=[('drama','DRAMA_RANDOM_OVERLAY','/data/drama-synthesis-gpu/assets/catalog-'),('tt','TT_POST_GPU_RANDOM_OVERLAY','/data/random-overlay-gpu/assets/catalog-'),('fb','FB_PAGE_GPU_RANDOM_OVERLAY','/data/random-overlay-gpu/assets/catalog-')]
def run(args,timeout=600):return subprocess.check_output(args,timeout=timeout).decode().strip()
def get_env(u):
 pid=run(['systemctl','show',u,'-p','MainPID','--value']);assert int(pid)>1
 return pid,dict(x.decode().split('=',1) for x in Path('/proc/'+pid+'/environ').read_bytes().split(b'\0') if b'=' in x)
qa=Path('/data/drama-synthesis-gpu/work/catalog-qa-20260920')
rows=json.loads((qa/'drama-report.json').read_text());assert len(rows)==14
assert sum(r['catalog']==SHA for r in rows)==12
assert {r['catalog'] for r in rows}=={SHA,PREVIOUS,ORIGINAL}
for lane in ['tt','fb']:assert json.loads((qa/(lane+'-report.json')).read_text())['catalog']==SHA
configs={}
for unit,(lane,prefix,base) in zip(units,specs):
 pid,env=get_env(unit)
 assert env[prefix+'_MANIFEST_SHA256']==PREVIOUS
 cg=run(['systemctl','show',unit,'-p','ControlGroup','--value'])
 assert set((Path('/sys/fs/cgroup/systemd')/cg.lstrip('/')/'cgroup.procs').read_text().split())=={pid},unit+' has children'
 assert hashlib.sha256((Path(base+SHA[:12])/'manifest.json').read_bytes()).hexdigest()==SHA
 values={prefix+'_ROOT':base+SHA[:12],prefix+'_MANIFEST_SHA256':SHA}
 if lane!='fb':
  registry=json.loads(env['RANDOM_OVERLAY_ASSET_CATALOGS']);assert set(registry)=={ORIGINAL,PREVIOUS}
  registry[SHA]=base+SHA[:12];values['RANDOM_OVERLAY_ASSET_CATALOGS']=json.dumps(registry,separators=(',',':'))
 if lane=='drama':values['DRAMA_GPU_RELEASE_SHA']=env['DRAMA_GPU_RELEASE_SHA']
 configs[lane]=values
for port in [8787,8830,8836]:assert not run(['ss','-Htn','state','established','( sport = :%d )'%port]),'HTTP still draining'
for p in Path('/data/drama-synthesis-gpu/work/jobs/.runtime/jobs').glob('*.json'):
 assert json.loads(p.read_text())['status'] not in ['queued','running'],'Drama must drain'
pointers={p:str(Path(p).resolve()) for p in ['/data/drama-synthesis-gpu/current','/data/tt-post-gpu/random-current','/opt/fb-page-random-overlay/current']}
assert pointers['/data/drama-synthesis-gpu/current'].endswith('/9c4c3cb4eca260d46df8ab23443a32cda667c20c')
assert pointers['/data/tt-post-gpu/random-current'].endswith('/326e16defb8f0b1aec76e8f36d52bb6359275a98-catalog')
assert pointers['/opt/fb-page-random-overlay/current'].endswith('/e3bef98')
for unit,(lane,_,_) in zip(units,specs):
 envfile=Path('/etc/random-overlay-subtemplates')/(lane+'-20260920.env');assert not envfile.exists()
 dropin=Path('/etc/systemd/system')/(unit+'.d')/'99-zz-random-subtemplates.conf'
 assert dropin.read_text()=='[Service]\nEnvironmentFile=/etc/random-overlay-subtemplates/'+lane+'-20260918.env\n'
B.mkdir(mode=0o700);B.chmod(0o700)
(B/'state-before.json').write_text(json.dumps({'pointers':pointers,'services':{u:run(['systemctl','show',u,'-p','ActiveState,MainPID,NRestarts']) for u in units}},indent=2))
for unit,(lane,_,_) in zip(units,specs):
 src=Path('/etc/systemd/system')/unit
 shutil.copy2(src,B/unit);shutil.copytree(str(src)+'.d',B/(unit+'.d'))
 shutil.copy2('/etc/random-overlay-subtemplates/'+lane+'-20260918.env',B/(lane+'.env.before'))
for u in units:
 subprocess.run(['systemctl','stop',u],check=True,timeout=90)
 assert run(['systemctl','show',u,'-p','MainPID','--value'])=='0'
for unit,(lane,_,_) in zip(units,specs):
 envfile=Path('/etc/random-overlay-subtemplates')/(lane+'-20260920.env');assert not envfile.exists()
 envfile.write_text(''.join(k+'='+shlex.quote(v)+'\n' for k,v in configs[lane].items()));envfile.chmod(0o600)
 dropin=Path('/etc/systemd/system')/(unit+'.d')/'99-zz-random-subtemplates.conf'
 assert dropin.read_text()=='[Service]\nEnvironmentFile=/etc/random-overlay-subtemplates/'+lane+'-20260918.env\n'
 tmp=dropin.with_suffix('.new');assert not tmp.exists()
 tmp.write_text('[Service]\nEnvironmentFile='+str(envfile)+'\n');tmp.chmod(0o644);os.replace(str(tmp),str(dropin))
subprocess.run(['systemctl','daemon-reload'],check=True)
for u in units:subprocess.run(['systemctl','start',u],check=True,timeout=600)
health={}
for port,path in [(8787,'/healthz'),(8830,'/health'),(8836,'/health')]:
 deadline=time.monotonic()+120
 while True:
  try:
   with urllib.request.urlopen('http://127.0.0.1:%d%s'%(port,path),timeout=10) as response:health[port]=json.load(response)
   break
  except Exception:
   if time.monotonic()>=deadline:raise
   time.sleep(2)
for u in ['drama-synthesis-gpu-tunnel.service','tt-gpu-reverse-tunnel.service','fb-page-random-overlay-tunnel.service']:
 subprocess.run(['systemctl','restart',u],check=True,timeout=30)
assert health[8830]['random_overlay_asset_set_sha256']==SHA
for p,resolved in pointers.items():assert str(Path(p).resolve())==resolved
for u,(_,prefix,_) in zip(units,specs):assert get_env(u)[1][prefix+'_MANIFEST_SHA256']==SHA
out={'ok':True,'catalog':SHA,'code_pointers_preserved':pointers,'health':health,'services':{u:run(['systemctl','show',u,'-p','ActiveState,MainPID,NRestarts']) for u in units}}
(B/'switch-after.json').write_text(json.dumps(out,indent=2));print(json.dumps(out,indent=2),flush=True)
