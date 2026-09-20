import json,subprocess
from pathlib import Path
backup=Path('/mnt/data-disk/random-overlay-gpu/backups/20260920-subtemplates80')
assert (backup/'cpu.env.before').exists()
script='/mnt/data-disk/random-overlay-gpu/backups/20260908-cache/maintenance.py'
for action in ['gate-on','pause']:
    subprocess.run(['python3',script,action,'tt','--apply'],check=True,timeout=90)
timer='fb-auto-post-prepare.timer'
before=subprocess.check_output(['systemctl','show',timer,'-p','ActiveState','--value']).decode().strip()
path=backup/'fb-prepare-timer.json'
assert not path.exists()
path.write_text(json.dumps({'active_state':before}))
if before=='active':subprocess.run(['systemctl','stop',timer],check=True,timeout=30)
units=['tt-post-prepare.service','tt-post-runner.service','tt-auto-post-runner.service','fb-auto-post-prepare.service']
print(json.dumps({'gate':'tt','fb_prepare_timer_before':before,'services':{u:subprocess.check_output(['systemctl','show',u,'-p','ActiveState,SubState,MainPID']).decode().strip() for u in units}}),flush=True)
