"""GitHub-first product-filter overlay; preserve in-flight work and live ledgers."""
import argparse
from contextlib import closing
import difflib
import json
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys

SOURCE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(SOURCE))
from scripts import deploy_hit_drama_launch_window as ops
from scripts.deploy_drama_launch_range import remember
from scripts.deploy_hit_drama_multi_id import atomic

BASE='6f05a250'
FILES={
 'x':['features/x_auto_posts/'+n+'.py' for n in ('validation','repositories','selector','publisher')],
 'fb':['features/fb_auto_posts/'+n+'.py' for n in ('validation','repositories','moboreels')],
}
SHARED='features/material_products.py'
STATIC=['static/'+lane+'-auto-publish-template.'+ext for lane in ('x-tag','fb') for ext in ('html','js')]+['static/material-product-picker.js']
TESTS=['scripts/test_auto_material_products.py','scripts/test_x_auto_post_validation.py','scripts/test_x_auto_post_selector.py','scripts/test_fb_auto_validation.py']

def original(name):
 return subprocess.check_output(['git','-C',str(SOURCE),'show',BASE+':'+name])

def overlay(folder,name,live):
 after=(SOURCE/name).read_bytes().replace(b'\r\n',b'\n')
 if name==SHARED or name.endswith('/material-product-picker.js'):
  assert not live or live==after,'new_module_collision:'+name
  return after
 before=original(name);live=live.replace(b'\r\n',b'\n')
 if live==before:return after
 if name.endswith('.html'):
  if name=='static/fb-auto-publish-template.html':
   # The older runtime copy differs only in the helper text for this replaced
   # field. Replace the reviewed field block, preserving all surrounding HTML.
   field=rb'<label class="required" for="materialProduct">.*?</span>'
   block=re.search(field,before,re.S)
   assert block and len(re.findall(field,live,re.S))==1,'material_field_ambiguous'
   live=re.sub(field,lambda _:block.group(0),live,flags=re.S)
  asset=Path(name).stem+'.js'
  pattern=rb'(/'+re.escape(asset.encode())+rb'\?v=)[^"\s]+'
  old_version=re.search(pattern,before)
  assert old_version
  live=re.sub(pattern,lambda _:old_version.group(0),live)
 scratch=folder/'overlay-scratch';scratch.write_bytes(live)
 patch=''.join(difflib.unified_diff(before.decode().splitlines(True),after.decode().splitlines(True),
     fromfile='a/'+scratch.name,tofile='b/'+scratch.name,n=2))
 result=subprocess.run(['git','apply','--whitespace=nowarn','-'],cwd=folder,input=patch,text=True,capture_output=True)
 assert not result.returncode,'live_variant_requires_review:'+name+':'+result.stderr
 return scratch.read_bytes()

def prepare(folder):
 assert ops.run('findmnt','-rn','-o','UUID','--mountpoint','/mnt/data-disk')=='3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
 assert folder.is_relative_to(Path('/mnt/data-disk')) and shutil.disk_usage('/mnt/data-disk').free>2*1024**3
 assert not ops.run('git','-C',SOURCE,'status','--porcelain')
 commit=ops.run('git','-C',SOURCE,'rev-parse','HEAD')
 assert ops.run('git','-C',SOURCE,'branch','-r','--contains',commit)
 folder.mkdir(parents=True,mode=0o700,exist_ok=False)
 info=dict(commit=commit,files={},lanes={},config_before={},services_before=ops.services())
 ops.save(folder/'manifest.json',info)
 for lane,names in FILES.items():
  root,_,_,unit,db=ops.LANES[lane]
  candidate=folder/('candidate-'+lane)
  shutil.copytree(root.resolve(),candidate,symlinks=True,ignore=shutil.ignore_patterns('__pycache__','*.pyc','.git'))
  for name in names+[SHARED]:
   path=root/name;data=overlay(folder,name,path.read_bytes() if path.exists() else b'')
   (candidate/name).write_bytes(data);compile(data,name,'exec');remember(folder,info,path,data)
  for name in TESTS:shutil.copy2(SOURCE/name,candidate/name)
  info['lanes'][lane]=dict(root=str(root.resolve()),candidate=str(candidate),unit=unit)
  info['config_before'][lane]=ops.config_hash(db,lane)
  with closing(sqlite3.connect(db.as_uri()+'?mode=ro',uri=True)) as src,closing(sqlite3.connect(folder/('ledger-before-'+lane+'.sqlite3'))) as dst:src.backup(dst)
  (folder/('ledger-before-'+lane+'.sqlite3')).chmod(0o600)
  commands=[[sys.executable,'-B','scripts/test_auto_material_products.py','--platform',lane],
   [sys.executable,'-B','-m','unittest',*(['scripts.test_x_auto_post_validation','scripts.test_x_auto_post_selector'] if lane=='x' else ['scripts.test_fb_auto_validation'])]]
  for i,cmd in enumerate(commands):
   result=subprocess.run(cmd,cwd=candidate,capture_output=True,text=True,timeout=90)
   (folder/('tests-'+lane+'-'+str(i)+'.txt')).write_text(result.stdout+result.stderr)
   if result.returncode:print(result.stdout+result.stderr);raise RuntimeError('candidate_tests_failed:'+lane)
  print(lane+' candidate tests passed',flush=True)
 # The main API proxies these editors to the independent owners. Its retired
 # source modules are not executing this path; preserve them and their imports.
 main=Path('/root/drama_material_service')
 for root in [main,Path('/usr/share/nginx/html'),ops.LANES['x'][0],ops.LANES['fb'][0]]:
  for name in STATIC:
   path=root/(Path(name).name if root.name=='html' else name)
   if not path.exists() and not name.endswith('/material-product-picker.js'):continue
   remember(folder,info,path,overlay(folder,name,path.read_bytes() if path.exists() else b''))
 info['status']='prepared';ops.save(folder/'manifest.json',info)
 print(json.dumps(dict(status='prepared',commit=commit,files=len(info['files']))))

def switch(folder,rollback=False):
 info=json.loads((folder/'manifest.json').read_text())
 assert info['status'] in ('prepared','active','switch_requested','rolled_back')
 for lane,row in info['lanes'].items():assert str(ops.LANES[lane][0].resolve())==row['root'],'runtime_changed:'+lane
 for name,row in info['files'].items():
  current=ops.digest(name) if Path(name).exists() else ''
  assert current in (row['before'],row['after']),'preserve_later_file:'+name
 action='rollback' if rollback else 'apply'
 if rollback:
  for lane,h in info['config_before'].items():
   assert ops.config_hash(ops.LANES[lane][4],lane)==h,'templates_changed_requires_forward_fix:'+lane
 for lane in FILES:
  ops.run(sys.executable,ops.HEALTH,'snapshot','--lane',lane,'--output',folder/(action+'-schedulers-before-'+lane+'.json'))
 for name,row in info['files'].items():
  if '/static/' in name or name.startswith('/usr/share/nginx/html/'):continue
  if rollback and not row['existed']:continue
  path=Path(row['backup' if rollback else 'next']);atomic(Path(name),path.read_bytes(),row)
 info.update(status='switch_requested',requested_action=action);ops.save(folder/'manifest.json',info)
 ops.restart_fb(folder,action)
 ops.run('systemctl','restart','--no-block',ops.LANES['x'][3])
 print(json.dumps(dict(status='switch_requested',action=action,commit=info['commit'])))

def verify(folder):
 info=json.loads((folder/'manifest.json').read_text());rollback=info['requested_action']=='rollback';action=info['requested_action']
 for lane,row in info['lanes'].items():
  unit=row['unit'];assert ops.run('systemctl','show',unit,'-p','ActiveState','--value')=='active',unit+':pending'
  pid=ops.run('systemctl','show',unit,'-p','MainPID','--value')
  assert 'MainPID='+pid+'\n' not in info['services_before'][unit]+'\n',unit+':old_pid'
  ops.run('curl','--max-time','5','-fsS','http://127.0.0.1:'+('18835' if lane=='fb' else '18843')+'/health')
  ops.run(sys.executable,ops.HEALTH,'restore','--baseline',folder/(action+'-schedulers-before-'+lane+'.json'),
          '--wait-seconds','25','--output',folder/(action+'-schedulers-after-'+lane+'.json'))
 for name,row in info['files'].items():
  if '/static/' in name or name.startswith('/usr/share/nginx/html/'):
   if rollback and not row['existed']:continue
   current=ops.digest(name) if Path(name).exists() else ''
   assert current in (row['before'],row['after']),'static_changed:'+name
   atomic(Path(name),Path(row['backup' if rollback else 'next']).read_bytes(),row)
 for name,row in info['files'].items():
  assert ops.digest(name)==(row['before'] if rollback and row['existed'] else row['after']),'hash_mismatch:'+name
 info['status']='rolled_back' if rollback else 'active';ops.save(folder/'manifest.json',info)
 result=dict(status=info['status'],commit=info['commit'],files=len(info['files']),services_after=ops.services(),
  config_unchanged={lane:ops.config_hash(ops.LANES[lane][4],lane)==h for lane,h in info['config_before'].items()})
 ops.save(folder/('verification-'+action+'.json'),result);print(json.dumps(result))

if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('action',choices=['prepare','apply','rollback','verify']);p.add_argument('--folder',type=Path,required=True);a=p.parse_args()
 if a.action=='prepare':prepare(a.folder.resolve())
 elif a.action=='verify':verify(a.folder.resolve())
 else:switch(a.folder.resolve(),a.action=='rollback')
