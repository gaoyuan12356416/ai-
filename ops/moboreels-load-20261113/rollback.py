"""Code/config rollback only: never restore business databases or receipts."""
import json
import os
from pathlib import Path
import shutil
import subprocess

p = Path(__file__).resolve().parent
m = json.loads((p/'manifest.json').read_text())
for item in m['files']:
    target = Path(item['target']); staged = target.with_name(target.name+'.rollback-next')
    shutil.copy2(item['backup'], str(staged)); os.replace(str(staged), str(target))
current = Path('/opt/moboreels-library/current'); staged = current.with_name('rollback-next')
staged.symlink_to(m['previous_current']); os.replace(str(staged), str(current))
subprocess.run(['nginx', '-t'], check=True)
subprocess.run(['systemctl', 'restart', 'moboreels-library.service'], check=True)
subprocess.run(['systemctl', 'reload', 'nginx'], check=True)
print('Restored code/config; business data and receipts retained.')
