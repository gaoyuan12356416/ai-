#!/usr/bin/env python3
"""Stage or activate only the independent Post report; never start its sender."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

FILES = ['features/post_daily_report/x.py', 'features/post_daily_report/x_cost.py',
         'features/post_daily_report/plans.py', 'features/post_daily_report/target_plans.py',
         'scripts/post_daily_report.py']
CURRENT = Path('/opt/post-daily-report/current')
DROPIN = Path('/etc/systemd/system/post-daily-report.service.d/x-profile-read.conf')
UNIT = '''[Service]
ReadWritePaths=-/mnt/data-disk/x-tag-publisher
ReadOnlyPaths=-/mnt/data-disk/x-tag-publisher/posts.sqlite3
ReadOnlyPaths=-/mnt/data-disk/x-tag-publisher/auto/x-auto-post.sqlite3
'''


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def call(*args):
    return subprocess.check_output(args, text=True).strip()


def switch(target):
    temp = CURRENT.with_name('current.x-report-next')
    temp.symlink_to(target)
    os.replace(temp, CURRENT)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source', type=Path)
    p.add_argument('--deployment', required=True, type=Path)
    p.add_argument('--activate', action='store_true')
    p.add_argument('--rollback', action='store_true')
    a = p.parse_args()
    root = a.deployment.resolve()
    if not str(root).startswith('/mnt/data-disk/x-report-profile-20261113/'):
        raise RuntimeError('unexpected_deployment_path')
    if call('findmnt', '-rn', '-o', 'UUID', '--mountpoint', '/mnt/data-disk') != '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':
        raise RuntimeError('data_disk_unavailable')
    manifest = root / 'manifest.json'
    if not a.activate and not a.rollback:
        if not a.source or root.exists():
            raise RuntimeError('new_deployment_and_source_required')
        if shutil.disk_usage('/mnt/data-disk').free < 2 * 1024**3:
            raise RuntimeError('insufficient_staging_space')
        source = a.source.resolve()
        old = CURRENT.resolve()
        root.mkdir(parents=True)
        candidate = root / 'report'
        shutil.copytree(old, candidate, symlinks=True, ignore=shutil.ignore_patterns('.git', '__pycache__', '*.pyc'))
        for relative in FILES + ['tests/test_post_daily_report_x_profile_targets.py']:
            shutil.copy2(source / relative, candidate / relative)
        data = {'commit': call('git', '-C', str(source), 'rev-parse', 'HEAD'),
                'old': str(old), 'candidate': str(candidate), 'source': str(source),
                'old_hashes': {f: digest(old / f) for f in FILES},
                'new_hashes': {f: digest(candidate / f) for f in FILES},
                'dropin_old': DROPIN.read_text() if DROPIN.exists() else None}
        manifest.write_text(json.dumps(data, indent=2))
        print(json.dumps({'status': 'staged', 'deployment': str(root), 'commit': data['commit']}))
        return
    data = json.loads(manifest.read_text())
    if subprocess.run(['systemctl', 'is-active', '--quiet', 'post-daily-report.service']).returncode == 0:
        raise RuntimeError('report_sender_currently_running')
    if a.rollback:
        if str(CURRENT.resolve()) != data['candidate']:
            raise RuntimeError('current_release_changed')
        switch(data['old'])
        if data['dropin_old'] is None:
            DROPIN.unlink(missing_ok=True)
        else:
            DROPIN.write_text(data['dropin_old'])
        call('systemctl', 'daemon-reload')
        print(json.dumps({'status': 'rolled_back', 'current': str(CURRENT.resolve())}))
        return
    if str(CURRENT.resolve()) != data['old']:
        raise RuntimeError('report_release_changed_since_staging')
    for relative, expected in data['old_hashes'].items():
        if digest(CURRENT / relative) != expected:
            raise RuntimeError('report_file_changed_since_staging:' + relative)
    for relative, expected in data['new_hashes'].items():
        if digest(Path(data['candidate']) / relative) != expected:
            raise RuntimeError('candidate_file_changed:' + relative)
    DROPIN.parent.mkdir(parents=True, exist_ok=True)
    DROPIN.write_text(UNIT)
    switch(data['candidate'])
    call('systemctl', 'daemon-reload')
    print(json.dumps({'status': 'activated', 'current': str(CURRENT.resolve()), 'commit': data['commit'],
                      'sender_started': False, 'publisher_restarted': False}))


if __name__ == '__main__':
    main()
