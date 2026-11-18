"""Exact-baseline overlay and scoped graceful restart of the tag lane."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

SOURCE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(SOURCE))
from scripts import deploy_hit_drama_launch_window as ops
from scripts.deploy_drama_launch_range import remember
from scripts.deploy_hit_drama_multi_id import atomic

BASE='ad6cfc7b'
CHANGED=['features/x_auto_posts/'+x+'.py' for x in ('repositories','selector','service','publisher')]+[
 'features/x_posts/selector.py','features/x_posts/service.py',
 'features/x_tag_publish/selection.py','features/x_tag_publish/policy.py']
NEW=['features/x_tag_publish/material_catalog.py','features/x_posts/moboreels.py']+[
 'features/moboreels_library/'+x+'.py' for x in ('__init__','macro_client','settings')]
UNITS=['x-tag-auto.service','x-tag-post.service','x-tag-gateway.service']
TRIGGERS=['x-tag-auto-scheduler.timer','x-tag-auto-runner.timer','x-tag-auto-runner.path']

def prepare(folder):
 assert not ops.run('git','-C',SOURCE,'status','--porcelain')
 commit=ops.run('git','-C',SOURCE,'rev-parse','HEAD')
 assert ops.run('git','-C',SOURCE,'branch','-r','--contains',commit)
 root=Path('/opt/x-tag-publisher/current').resolve()
 folder.mkdir(parents=True,exist_ok=False,mode=0o700)
 info=dict(commit=commit,root=str(root),files={},status='preparing',
           pids={u:ops.run('systemctl','show',u,'-p','MainPID','--value') for u in UNITS},
           triggers={u:ops.run('systemctl','is-active',u) for u in TRIGGERS})
 ops.save(folder/'manifest.json',info)
 candidate=folder/'candidate'
 shutil.copytree(root,candidate,symlinks=True,ignore=shutil.ignore_patterns('__pycache__','*.pyc','.git'))
 for name in CHANGED+NEW:
  dest=root/name
  before=dest.read_bytes().replace(b'\r\n',b'\n') if dest.exists() else b''
  after=(SOURCE/name).read_bytes().replace(b'\r\n',b'\n')
  if name in CHANGED:
   expected=subprocess.check_output(['git','-C',str(SOURCE),'show',BASE+':'+name]).replace(b'\r\n',b'\n')
   assert before==expected,'live_file_changed:'+name
  else:assert not before or before==after,'module_collision:'+name
  p=candidate/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(after)
  compile(after,name,'exec');remember(folder,info,dest,after)
 for name in ['test_tag_mobo_catalog','test_auto_material_products','test_x_auto_post_selector','test_x_auto_post_validation']:
  shutil.copy2(SOURCE/'scripts'/(name+'.py'),candidate/'scripts'/(name+'.py'))
 for cmd in [[sys.executable,'-B','-m','unittest','scripts.test_tag_mobo_catalog','scripts.test_x_auto_post_selector','scripts.test_x_auto_post_validation'],
             [sys.executable,'-B','scripts/test_auto_material_products.py','--platform','x']]:
  result=subprocess.run(cmd,cwd=candidate,text=True,capture_output=True,timeout=90)
  with (folder/'tests.txt').open('a') as f:f.write(result.stdout+result.stderr)
  assert not result.returncode,result.stdout+result.stderr
 for name in ('x_auto_post','x_post'):
  # Resolve each actual database from the owner environment, never from defaults.
  unit='x-tag-auto.service' if name=='x_auto_post' else 'x-tag-post.service'
  pid=info['pids'][unit]
  env=dict(x.split('=',1) for x in Path('/proc/'+pid+'/environ').read_text().split('\0') if '=' in x)
  path=Path(env['X_AUTO_POST_DB_PATH' if name=='x_auto_post' else 'X_POST_DB_PATH'])
  with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as src,closing(sqlite3.connect(folder/(name+'.sqlite3'))) as dst:src.backup(dst)
  (folder/(name+'.sqlite3')).chmod(0o600)
 info['status']='prepared';ops.save(folder/'manifest.json',info)
 print(json.dumps(dict(status='prepared',commit=commit,files=len(info['files']))))

def apply(folder,rollback=False):
 info=json.loads((folder/'manifest.json').read_text())
 assert str(Path('/opt/x-tag-publisher/current').resolve())==info['root']
 for path,row in info['files'].items():
  assert (ops.digest(path) if Path(path).exists() else '') in (row['before'],row['after']),'preserve_newer_file:'+path
 for path,row in info['files'].items():
  if rollback and not row['existed']:continue
  Path(path).parent.mkdir(parents=True,exist_ok=True)
  atomic(Path(path),Path(row['backup' if rollback else 'next']).read_bytes(),row)
 info['status']='rollback_requested' if rollback else 'switch_requested';ops.save(folder/'manifest.json',info)
 # All three owners drain active HTTP handlers on SIGTERM; no force-stop timeout.
 ops.run('systemctl','restart','--no-block',*UNITS)
 print(json.dumps(dict(status=info['status'],commit=info['commit'])))

def verify(folder):
 info=json.loads((folder/'manifest.json').read_text())
 for unit in UNITS:
  assert ops.run('systemctl','is-active',unit)=='active'
  assert ops.run('systemctl','show',unit,'-p','MainPID','--value')!=info['pids'][unit],unit+':old_pid'
 for unit,before in info['triggers'].items():
  if before=='active' and ops.run('systemctl','is-active',unit)!='active':ops.run('systemctl','start',unit)
  assert ops.run('systemctl','is-active',unit)==before,unit
 for port in ('18843','8812','8898'):
  # Native/gateway port health is checked from the unit listener separately.
  if port=='18843':ops.run('curl','--max-time','5','-fsS','http://127.0.0.1:'+port+'/health')
 info['status']='active';ops.save(folder/'manifest.json',info)
 print(json.dumps(dict(status='active',commit=info['commit'],triggers=info['triggers'])))

if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('action',choices=['prepare','apply','verify','rollback']);p.add_argument('--folder',type=Path,required=True);a=p.parse_args()
 if a.action=='prepare':prepare(a.folder.resolve())
 elif a.action=='verify':verify(a.folder.resolve())
 else:apply(a.folder.resolve(),a.action=='rollback')
