"""Grant only the existing TT preflight spool to the service mount namespace."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

BASE=Path('/mnt/data-disk/production-audit-20261123')
TARGET=Path('/etc/systemd/system/tt-auto-post-service.service.d/zzzzzzzz-media-preflight-write.conf')
UNIT='tt-auto-post-service.service'


def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=['apply','rollback']);a=p.parse_args()
    source=Path(__file__).parent/'systemd/tt-auto-media-preflight.conf'
    state=BASE/'tt-preflight-directory.json'
    if a.action=='apply':
        if state.exists() or TARGET.exists():raise RuntimeError('deployment_already_exists')
        spool=Path('/mnt/data-disk/social-480-20261119/tmp/tt-post')
        if not spool.is_dir():raise RuntimeError('preflight_spool_missing')
        pid=int(subprocess.check_output(['systemctl','show',UNIT,'-p','MainPID','--value']))
        env=dict(x.decode().split('=',1) for x in Path('/proc/%d/environ'%pid).read_bytes().split(b'\0') if b'=' in x)
        if env.get('PIPELINE_MEDIA_PREFLIGHT_TMP')!=str(spool):raise RuntimeError('preflight_spool_changed')
        baseline=BASE/'schedulers-before-tt-preflight.json'
        subprocess.run(['python3','/opt/publish-scheduler-health/current/scripts/publish_scheduler_health.py','snapshot','--lane','tt','--output',str(baseline)],check=True,stdout=subprocess.PIPE)
        result={'unit':UNIT,'previous_pid':pid,'previous_file_existed':False,'path':str(TARGET),'sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'rollback':'remove only this drop-in; daemon-reload; graceful restart'}
        state.write_text(json.dumps(result,indent=2));shutil.copyfile(str(source),str(TARGET));os.chmod(str(TARGET),0o644)
    else:
        result=json.loads(state.read_text())
        if hashlib.sha256(TARGET.read_bytes()).hexdigest()!=result['sha256']:raise RuntimeError('drop_in_changed')
        TARGET.unlink()
    subprocess.run(['systemctl','daemon-reload'],check=True)
    subprocess.run(['systemctl','restart','--no-block',UNIT],check=True)
    print(json.dumps({'action':a.action,'unit':UNIT,'graceful_restart_requested':True}))


if __name__=='__main__':main()
