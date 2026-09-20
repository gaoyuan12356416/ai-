"""Finish draining only the backed-up FFmpeg that ignored graceful SIGINT."""
import json,os,signal,subprocess,time
from pathlib import Path

B=Path('/data/random-overlay-gpu/backups/20260920-source-overlay-stalled-prepare')
record=json.loads((B/'graceful-exit.json').read_text())
assert record['pid']==2758821 and record['signal']=='SIGINT' and not record['exited']
pid=record['pid'];proc=Path('/proc')/str(pid)
args=(B/'ffmpeg.cmdline').read_bytes().split(b'\0')
job=Path(args[args.index(b'-i')+1].decode()).parent
assert job.parent==Path('/var/lib/fb-page-random-overlay/jobs')
events=[]
def guard():
    fields=(proc/'stat').read_text().rsplit(')',1)[1].split()
    assert fields[19]==record['identity']['start_ticks']
    assert int(fields[1])==record['identity']['parent_pid']
    assert (proc/'cmdline').read_bytes()==(B/'ffmpeg.cmdline').read_bytes()
    assert not (job/'manifest.json').exists()
    info=(job/'output.tmp.mp4').stat()
    assert info.st_size==record['before']['bytes'] and info.st_mtime_ns==record['before']['mtime_ns']
    assert time.time()-info.st_mtime>3600
def save():
    with (B/'finish-drain.json').open('w') as f:
        json.dump({'pid':pid,'start_ticks':record['identity']['start_ticks'],'events':events},f,indent=2);f.flush();os.fsync(f.fileno())
    (B/'finish-drain.json').chmod(0o600)
for sig,wait in [(signal.SIGTERM,15),(signal.SIGKILL,10)]:
    if not proc.exists():break
    guard()
    events.append({'signal':sig.name,'requested_at':time.time(),'output_unchanged':True});save()
    os.kill(pid,sig)
    deadline=time.monotonic()+wait
    while proc.exists() and time.monotonic()<deadline:time.sleep(.5)
assert not proc.exists(),'Exact stalled process did not exit; do not stop worker'
deadline=time.monotonic()+5
while (job/'output.tmp.mp4').exists() and time.monotonic()<deadline:time.sleep(.2)
assert (job/'source.mp4').exists() and not (job/'manifest.json').exists()
assert not (job/'output.tmp.mp4').exists(),'Wait for worker failure cleanup'
events.append({'exited':True,'source_retained':True,'no_ready_manifest':True,'worker_removed_unfinished_output':True});save()
print(json.dumps({'ok':True,'pid':pid,'backup':str(B),'events':events}),flush=True)
