"""Restore source/unit configuration only, retaining all job and output facts."""
from pathlib import Path
import hashlib
import json
import shutil
import subprocess

from deploy import STATE, LIVE, UNITS, OVERRIDE, assert_drained


def main():
    assert_drained()
    metadata = json.loads((STATE / 'deployment.json').read_text(encoding='utf-8'))
    assert hashlib.sha256(LIVE.read_bytes()).hexdigest() == metadata['new_sha256'], 'newer source exists'
    for unit in UNITS:
        override = Path('/etc/systemd/system') / (unit + '.d') / OVERRIDE
        assert metadata['release'] in override.read_text(encoding='utf-8'), 'unit drift'
    backup = Path(metadata['backup']) / LIVE.name
    assert hashlib.sha256(backup.read_bytes()).hexdigest() == metadata['old_sha256']
    shutil.copy2(str(backup), str(LIVE))
    for unit in UNITS:
        (Path('/etc/systemd/system') / (unit + '.d') / OVERRIDE).unlink()
    subprocess.check_call(['systemctl', 'daemon-reload'])
    subprocess.check_call(['systemctl', 'restart'] + UNITS)
    print('Code and unit configuration restored; database and outputs retained.')
    print('The former gpt-5.5 model is rejected: stop these workers or set a supported model before resuming work.')


if __name__ == '__main__': main()
