import json,os,shutil,subprocess
from pathlib import Path
COMMIT='cededc111d10150e9ee378e3d32226e2cd4de6f5'
SHA='b5df776a88bdfa961e60f60d6076c6915f37c5fd5ed8ade973dc4be7205eea6c'
SOURCE=Path('/data/random-overlay-gpu/releases/'+COMMIT+'-assets80')
SHARED=Path('/data/random-overlay-gpu/assets/catalog-'+SHA[:12])
DRAMA=Path('/data/drama-synthesis-gpu/assets/catalog-'+SHA[:12])
def run(args,timeout=1800):
 print(json.dumps({'stage_command':args[:3]}),flush=True)
 subprocess.run(args,check=True,timeout=timeout,env=dict(os.environ,GIT_TERMINAL_PROMPT='0'))
if not SOURCE.exists():
 run(['git','clone','--depth','1','--single-branch','--branch','codex/random-subtemplates-80-20260920','https://github.com/gaoyuan12356416/ai-.git',str(SOURCE)])
assert subprocess.check_output(['git','-C',str(SOURCE),'rev-parse','HEAD']).decode().strip()==COMMIT
assert not subprocess.check_output(['git','-C',str(SOURCE),'status','--porcelain']).strip()
SOURCE.chmod(0o755)
run(['/data/drama-synthesis-gpu/runtime/current/bin/python',str(SOURCE/'scripts/stage_random_subtemplates_20260920.py'),'--existing-assets','/data/random-overlay-gpu/assets/catalog-24d6fad3174f','--target',str(SHARED)])
assert SHARED.resolve()==SHARED and DRAMA.parent.resolve()==DRAMA.parent
if not DRAMA.exists():
 shutil.copytree(SHARED,DRAMA,copy_function=os.link)
 DRAMA.chmod(0o755)
assert not DRAMA.is_symlink()
code="from features.fb_gpu.random_overlay import load_asset_set;from pathlib import Path;a=load_asset_set(Path("+repr(str(DRAMA))+"),"+repr(SHA)+");assert {k:len(v) for k,v in a['categories'].items()}=={'border':20,'light':2,'opacity_video':20,'corners':20,'tint':20};print('80 assets readable by actual service user')"
run(['runuser','-u','drama-synthesis-gpu','--','/data/drama-synthesis-gpu/runtime/current/bin/python','-c',"import sys;sys.path.insert(0,'/data/drama-synthesis-gpu/current');"+code])
print(json.dumps({'staged':True,'commit':COMMIT,'manifest_sha256':SHA,'shared':str(SHARED),'drama':str(DRAMA)}),flush=True)
