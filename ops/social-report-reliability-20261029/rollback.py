"""Restore this deployment's exact preimages; restart only the API afterwards."""
import json
import os
from pathlib import Path
import subprocess

backup=Path('/mnt/data-disk/social-report-repair-20261029/deploy-backup')
m=json.loads((backup/'deployment.json').read_text())
followup=backup.with_name('deploy-backup-followup')/'deployment.json'
if followup.exists():
    for f in json.loads(followup.read_text())['files']:
        target=Path(f['path']);tmp=target.with_name(target.name+'.report-rollback-next')
        tmp.write_bytes(Path(f['backup']).read_bytes());tmp.chmod(f['old_mode']);os.replace(tmp,target)
for item in m['files']:
    target=Path(item['path'])
    if item['backup']:
        tmp=target.with_name(target.name+'.report-rollback-next')
        tmp.write_bytes(Path(item['backup']).read_bytes());tmp.chmod(item['old_mode']);os.replace(tmp,target)
    else:target.unlink(missing_ok=True)
link=Path('/opt/youtube-publisher-daily-report/current');tmp=link.with_name('current.report-rollback-next')
tmp.symlink_to(m['original_collector_release']);os.replace(tmp,link)
subprocess.run(['bash','/root/drama_material_service/scripts/safe_restart_drama_api.sh'],check=True)
print('Rollback complete; collector timer retained and next run uses original release')
