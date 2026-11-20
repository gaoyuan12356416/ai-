"""Restore the four backed-up static files, then remove only the cards route."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

manifest = json.loads(Path(sys.argv[1]).read_text())
for item in manifest['files']:
    shutil.copy2(item['backup'], item['target'])
Path('/etc/nginx/default.d/youtube-material-cards.conf').unlink(missing_ok=True)
subprocess.run(['nginx', '-t'], check=True)
subprocess.run(['systemctl', 'reload', 'nginx'], check=True)
subprocess.run(['systemctl', 'disable', '--now', 'youtube-material-cards.service'], check=True)
print('Restored static assets and removed the card route; publishing services untouched.')
