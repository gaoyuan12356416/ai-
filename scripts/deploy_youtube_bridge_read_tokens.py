"""Exact-source, quiet YouTube-only bridge switch; coordinator never restarted."""
import argparse,datetime as dt,json,re,sqlite3,subprocess,sys,time,urllib.request
from pathlib import Path

BASE=Path('/mnt/data-disk/pipeline-dispatch');REPO=BASE/'github'
UNIT=Path('/etc/systemd/system/pipeline-youtube-canonical-recovery.service')
OLD=BASE/'youtube-bridge-c1e8645d97b2'
def run(cmd,**kw):return subprocess.run(cmd,check=True,**kw)
def pid(unit):return subprocess.check_output(['systemctl','show',unit,'-p','MainPID','--value'],text=True).strip()
def main():
 p=argparse.ArgumentParser();p.add_argument('--sha',required=True);args=p.parse_args();assert re.fullmatch('[a-f0-9]{40}',args.sha)
 run(['git','-C',str(REPO),'fetch','origin',args.sha],stdout=subprocess.DEVNULL)
 assert subprocess.check_output(['git','-C',str(REPO),'rev-parse',args.sha],text=True).strip()==args.sha
 target=BASE/('youtube-bridge-'+args.sha[:12])
 if not target.exists():run(['git','-C',str(REPO),'worktree','add','--detach',str(target),args.sha],stdout=subprocess.DEVNULL)
 run(['python3.9','-m','unittest','scripts.test_youtube_publish_recovery','scripts.test_pipeline_youtube_canonical_service'],cwd=str(target))
 sys.path.insert(0,str(REPO));from features.pipeline_dispatch.core import DispatchStore
 s=DispatchStore(BASE/'dispatch.sqlite3');cfg=s.config();assert 'youtube' in cfg['enabled_channels']
 units=('pipeline-coordinator','pipeline-node-cpu-main','youtube-auto-publish-worker');pids={u:pid(u) for u in units}
 original=UNIT.read_text();assert str(OLD) in original and '--port 18848' in original
 with sqlite3.connect(BASE/'dispatch.sqlite3') as c:assert not c.execute("select 1 from pipeline_job where channel='youtube' and status='running'").fetchone(),'wait_for_youtube_jobs'
 backup=BASE/('youtube-bridge-read-cache-'+dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ'));backup.mkdir(mode=0o700)
 (backup/'unit-before.txt').write_text(original);(backup/'config-before.json').write_text(json.dumps(cfg,indent=2))
 s.channel_enabled('youtube',False)
 try:
  with sqlite3.connect(BASE/'dispatch.sqlite3') as c:assert not c.execute("select 1 from pipeline_job where channel='youtube' and status='running'").fetchone(),'claim_raced_pause'
  UNIT.write_text(original.replace(str(OLD),str(target)))
  run(['systemctl','daemon-reload']);run(['systemctl','restart','pipeline-youtube-canonical-recovery'],timeout=90)
  healthy=False
  for _ in range(20):
   try:
    with urllib.request.urlopen('http://127.0.0.1:18848/health',timeout=2) as r:healthy=r.status==200
   except Exception:pass
   if healthy:break
   time.sleep(1)
  assert healthy,'new_bridge_health_failed'
 except Exception:
  UNIT.write_text(original);run(['systemctl','daemon-reload']);run(['systemctl','restart','pipeline-youtube-canonical-recovery'],timeout=90)
  with urllib.request.urlopen('http://127.0.0.1:18848/health',timeout=3) as r:assert r.status==200
  s.channel_enabled('youtube',True);raise
 s.channel_enabled('youtube',True)
 after=s.config();assert {u:pid(u) for u in units}==pids,'unrelated_service_changed'
 assert after['hosts']==cfg['hosts'] and after['channel_limits']==cfg['channel_limits'] and after['enabled_channels']==cfg['enabled_channels']
 report={'sha':args.sha,'backup':str(backup),'unchanged_processes':pids,'health':200,'youtube_admission':'enabled','shared_coordinator_restarts':0}
 (backup/'completed.json').write_text(json.dumps(report,indent=2));print(json.dumps(report),flush=True)
if __name__=='__main__':main()
