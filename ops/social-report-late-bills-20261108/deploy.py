"""Deploy reporting-only code and bounded incomplete-data retries, with rollback."""
import fcntl, hashlib, json, os, shutil, sqlite3, subprocess
from pathlib import Path
import argparse
ROOT=Path(__file__).resolve().parents[2]
STATE=Path('/mnt/data-disk/social-report-repair-20261108')
LINK=Path('/opt/youtube-publisher-daily-report/current')
DROP=Path('/etc/systemd/system/youtube-publisher-daily-report.service.d/90-data-retry.conf')
FILES=['scripts/youtube_publisher_daily_report.py','features/social_daily_report/billing.py']
def run(*args): return subprocess.check_output(args,text=True).strip()
def digest(p): return hashlib.sha256(p.read_bytes()).hexdigest()
def main():
 action=argparse.ArgumentParser();action.add_argument('action',choices=['stage','apply','rollback']);act=action.parse_args().action
 os.umask(0o077);manifest=STATE/'deployment.json'
 if act=='stage':
  assert not manifest.exists() or json.loads(manifest.read_text()).get('status')=='rolled_back'
  assert run('findmnt','-rn','-o','TARGET','--mountpoint','/mnt/data-disk')=='/mnt/data-disk'
  assert shutil.disk_usage(STATE).free>1024**3
  assert not run('git','-C',str(ROOT),'status','--porcelain')
  commit=run('git','-C',str(ROOT),'rev-parse','HEAD')
  assert run('git','-C',str(ROOT),'branch','-r','--contains',commit)
  old=LINK.resolve();new=old.parent/(commit[:12]+'-data-retry')
  shutil.copytree(old,new,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
  for f in FILES:
   shutil.copy2(ROOT/f,new/f);compile((new/f).read_text(),str(new/f),'exec')
  if DROP.exists(): shutil.copy2(DROP,STATE/'retry-dropin.before')
  state=dict(commit=commit,previous=str(old),release=str(new),dropin_existed=DROP.exists(),
   hashes={f:digest(new/f) for f in FILES},dropin_sha=digest(ROOT/'deploy/social-daily-report-data-retry.conf'))
  manifest.write_text(json.dumps(state,indent=2));print(json.dumps(state));return
 state=json.loads(manifest.read_text());forward=act=='apply'
 assert str(LINK.resolve())==state['previous' if forward else 'release']
 with (Path('/mnt/data-disk/youtube-publisher-daily-report')/'run.lock').open('a') as lock:
  fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
  if forward:
   for f,sha in state['hashes'].items(): assert digest(Path(state['release'])/f)==sha
   assert digest(ROOT/'deploy/social-daily-report-data-retry.conf')==state['dropin_sha']
   with sqlite3.connect('file:/mnt/data-disk/youtube-publisher-daily-report/delivery.sqlite3?mode=ro',uri=True) as src:
    with sqlite3.connect(STATE/'delivery.before-apply.sqlite3') as dst: src.backup(dst)
   shutil.copy2(ROOT/'deploy/social-daily-report-data-retry.conf',DROP)
  elif state['dropin_existed']: shutil.copy2(STATE/'retry-dropin.before',DROP)
  else: DROP.unlink(missing_ok=True)
  temporary=LINK.with_name('current.data-retry-next');temporary.symlink_to(state['release' if forward else 'previous']);os.replace(temporary,LINK)
  subprocess.run(['systemctl','daemon-reload'],check=True)
  subprocess.run(['systemd-analyze','verify','/etc/systemd/system/youtube-publisher-daily-report.service'],check=True)
 state['status']='active' if forward else 'rolled_back';manifest.write_text(json.dumps(state,indent=2));print(state['status'])
if __name__=='__main__':main()
