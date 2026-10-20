#!/usr/bin/env python3
"""Install only the independent report; never stop or restart a publisher."""
import argparse
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = Path('/mnt/data-disk/youtube-publisher-daily-report')
OPT = Path('/opt/youtube-publisher-daily-report')
UNITS = ['youtube-publisher-daily-report.service', 'youtube-publisher-daily-report.timer']


def run(*args):
    return subprocess.check_output(list(args), text=True).strip()


def link(target, destination):
    temp = destination.with_name(destination.name + '.next')
    if temp.is_symlink():
        temp.unlink()
    temp.symlink_to(target)
    os.replace(temp, destination)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sha')
    parser.add_argument('--rollback')
    args = parser.parse_args()
    os.umask(0o077)
    assert run('findmnt','-rn','-o','UUID','--mountpoint','/mnt/data-disk') == '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    if args.rollback:
        backup = Path(args.rollback).resolve()
        if BASE / 'backups' not in backup.parents:
            raise RuntimeError('invalid_backup_path')
        manifest = json.loads((backup/'manifest.json').read_text())
        subprocess.run(['systemctl','disable','--now',UNITS[1]], check=True)
        for name in UNITS:
            dest = Path('/etc/systemd/system')/name
            if (backup/name).exists():
                shutil.copy2(backup/name,dest)
            elif dest.exists():
                dest.unlink()
        if manifest['old_release']:
            link(manifest['old_release'],OPT/'current')
        elif (OPT/'current').is_symlink():
            (OPT/'current').unlink()
        subprocess.run(['systemctl','daemon-reload'],check=True)
        if manifest['old_timer_enabled']:
            subprocess.run(['systemctl','enable','--now',UNITS[1]],check=True)
        print(json.dumps({'status':'rolled_back','state_preserved':str(BASE)}))
        return
    sha = run('git','-C',str(ROOT),'rev-parse','HEAD')
    assert args.sha and sha == args.sha and not run('git','-C',str(ROOT),'status','--porcelain')
    subprocess.run(['systemd-analyze','verify',str(ROOT/'deploy'/UNITS[0]),str(ROOT/'deploy'/UNITS[1])],check=True)
    BASE.mkdir(parents=True,exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup = BASE/'backups'/stamp
    backup.mkdir(parents=True)
    old_release = str((OPT/'current').resolve()) if (OPT/'current').exists() else ''
    enabled = subprocess.run(['systemctl','is-enabled','--quiet',UNITS[1]]).returncode == 0
    for name in UNITS:
        old = Path('/etc/systemd/system')/name
        if old.exists():
            shutil.copy2(old,backup/name)
    (backup/'manifest.json').write_text(json.dumps(dict(old_release=old_release,old_timer_enabled=enabled,new_sha=sha),indent=2))
    OPT.mkdir(parents=True,exist_ok=True)
    link(str(ROOT),OPT/'current')
    for name in UNITS:
        shutil.copy2(ROOT/'deploy'/name,Path('/etc/systemd/system')/name)
    subprocess.run(['systemctl','daemon-reload'],check=True)
    # Enabling is explicit after the operator verifies a sandboxed preview.
    print(json.dumps(dict(status='installed_timer_not_enabled',sha=sha,backup=str(backup),
        rollback='python3 '+str(ROOT/'scripts/install_youtube_publisher_daily_report.py')+' --rollback '+str(backup))))


if __name__ == '__main__':
    main()
