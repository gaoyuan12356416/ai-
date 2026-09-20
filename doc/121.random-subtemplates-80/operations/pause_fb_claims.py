import json,os,signal,subprocess,time
from pathlib import Path
B=Path('/mnt/data-disk/random-overlay-gpu/backups/20260920-subtemplates80')
unit='fb-auto-post-prepare.service'
pid=int(subprocess.check_output(['systemctl','show',unit,'-p','MainPID','--value']).decode().strip())
record={'pid':pid,'unit':unit,'paused':False,'stop_requested':False}
path=B/'fb-claim-pause.json';assert not path.exists()
def save():
 temporary=path.with_suffix('.new');assert not temporary.exists()
 with temporary.open('w') as f:
  json.dump(record,f,indent=2);f.flush();os.fsync(f.fileno())
 temporary.chmod(0o600);os.replace(str(temporary),str(path))
 fd=os.open(str(B),os.O_DIRECTORY)
 try:os.fsync(fd)
 finally:os.close(fd)
if pid>1:
 cmd=Path('/proc/%d/cmdline'%pid).read_bytes().split(b'\0')
 assert b'scripts/fb_auto_post_runner.py' in cmd and b'prepare' in cmd
 stat=Path('/proc/%d/stat'%pid).read_text().rsplit(')',1)[1].split()
 assert stat[0] not in ['T','t'],'Do not take ownership of an already stopped client'
 record.update(start_ticks=stat[19],initial_state=stat[0],stop_requested=True,purpose='Pause only further CPU claims; accepted GPU media completes')
 # Durable intention precedes SIGSTOP: recovery must never rely on the later
 # confirmation flag alone, since signal delivery and filesystem writes race.
 save()
 assert int(subprocess.check_output(['systemctl','show',unit,'-p','MainPID','--value']).decode().strip())==pid
 assert Path('/proc/%d/stat'%pid).read_text().rsplit(')',1)[1].split()[19]==record['start_ticks']
 os.kill(pid,signal.SIGSTOP)
 deadline=time.monotonic()+5
 while True:
  stat=Path('/proc/%d/stat'%pid).read_text().rsplit(')',1)[1].split()
  assert stat[19]==record['start_ticks']
  if stat[0] in ['T','t']:break
  if time.monotonic()>=deadline:raise RuntimeError('stop confirmation pending; durable intention requires exact identity resume')
  time.sleep(.05)
 record.update(state_after=stat[0],paused=True);save()
else:save()
print(json.dumps(record),flush=True)
