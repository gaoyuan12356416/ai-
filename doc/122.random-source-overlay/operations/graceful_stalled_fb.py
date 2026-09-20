"""Preserve one proven-stalled, unpublished FB preparation and request graceful exit."""
import hashlib,json,os,shutil,signal,subprocess,time
from pathlib import Path

PID=2758821
UNIT='fb-page-random-overlay-gpu.service'
JOB=Path('/var/lib/fb-page-random-overlay/jobs/fb-page-03e739bb938593abf39fbf5d4013467621e89d777fbce903')
BACKUP=Path('/data/random-overlay-gpu/backups/20260920-source-overlay-stalled-prepare')
proc=Path('/proc')/str(PID)

def stat():
    fields=(proc/'stat').read_text().rsplit(')',1)[1].split()
    return {'state':fields[0],'parent_pid':int(fields[1]),'start_ticks':fields[19]}

def snapshot():
    out=JOB/'output.tmp.mp4'
    info=out.stat()
    return {'bytes':info.st_size,'mtime_ns':info.st_mtime_ns,'age_seconds':time.time()-info.st_mtime}

identity=stat()
main=int(subprocess.check_output(['systemctl','show',UNIT,'-p','MainPID','--value']).decode())
assert identity['parent_pid']==main and main>1
assert (proc/'comm').read_text().strip()=='ffmpeg'
cmd=(proc/'cmdline').read_bytes()
assert str(JOB/'source.mp4').encode() in cmd.split(b'\0')
assert str(JOB/'output.tmp.mp4').encode() in cmd.split(b'\0')
assert not (JOB/'manifest.json').exists(),'Completed results may not be interrupted'
before=snapshot()
assert before['age_seconds']>3600 and before['bytes']==8650800
time.sleep(10)
after=snapshot()
assert (after['bytes'],after['mtime_ns'])==(before['bytes'],before['mtime_ns'])
assert stat()['start_ticks']==identity['start_ticks']
BACKUP.mkdir(mode=0o700)
files=[]
for name in ['source.mp4','output.tmp.mp4','compositor-1f8af3588be3ef44437f.cl']:
    source=JOB/name
    target=BACKUP/name
    shutil.copy2(str(source),str(target));target.chmod(0o600)
    files.append({'name':name,'bytes':target.stat().st_size,'sha256':hashlib.sha256(target.read_bytes()).hexdigest()})
(BACKUP/'ffmpeg.cmdline').write_bytes(cmd);(BACKUP/'ffmpeg.cmdline').chmod(0o600)
report={'pid':PID,'unit':UNIT,'identity':identity,'before':before,'backup_files':files,'signal':'SIGINT','reason':'Unfinished preparation output unchanged for over one hour; no ready manifest exists','signal_requested':True}
path=BACKUP/'graceful-exit.json'
def save():
    with path.open('w') as f:
        json.dump(report,f,indent=2);f.flush();os.fsync(f.fileno())
    path.chmod(0o600)
save()
assert stat()['start_ticks']==identity['start_ticks']
assert stat()['parent_pid']==main
assert not (JOB/'manifest.json').exists()
assert snapshot()['mtime_ns']==before['mtime_ns']
os.kill(PID,signal.SIGINT)
deadline=time.monotonic()+35
while proc.exists() and time.monotonic()<deadline:
    assert stat()['start_ticks']==identity['start_ticks']
    time.sleep(1)
report.update(exited=not proc.exists(),source_retained=(JOB/'source.mp4').exists(),ready_manifest_exists=(JOB/'manifest.json').exists(),unfinished_output_exists=(JOB/'output.tmp.mp4').exists())
save()
print(json.dumps(report),flush=True)
assert report['exited'],'Graceful exit is pending; do not switch an active worker'
assert report['source_retained'] and not report['ready_manifest_exists']
