#!/usr/bin/env python3
"""Install the reviewed public readback fix; drain only the automatic YouTube worker."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import py_compile
import shutil
import sqlite3
import subprocess
import time

ROOT = Path('/root/drama_material_service')
BASE = Path('/mnt/data-disk/deploy/youtube-auto-publish')
WORKER = 'youtube-auto-publish-worker.service'
EXPECTED = {
    'features/youtube_auto_publish/engine.py': '1d92238297bac1028ee8e968e577e72b50949f2e3531b043d2f38b5733b8e529',
}


def run(*args):
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT).strip()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def install(source, target, mode):
    temp = target.with_name(target.name + '.public-readback-new')
    shutil.copyfile(source, temp)
    os.chmod(temp, mode)
    os.replace(temp, target)


def ledger():
    with sqlite3.connect('file:' + str(ROOT/'data/drama_material_jobs.sqlite3') + '?mode=ro', uri=True) as db:
        return {'publishing': db.execute('SELECT workflow,status,comment_status,count(*) FROM drama_youtube_publish GROUP BY workflow,status,comment_status').fetchall(),
                'preparations': db.execute('SELECT state,count(*) FROM youtube_auto_preparation GROUP BY state').fetchall(),
                'links': db.execute('SELECT count(*) FROM drama_material_short_link').fetchone()[0]}


def worker_contract():
    if run('systemctl', 'is-active', WORKER) != 'active':
        raise RuntimeError('Worker must be active')
    if (run('systemctl', 'show', WORKER, '-p', 'Restart', '--value') != 'always' or
            run('systemctl', 'show', WORKER, '-p', 'WatchdogUSec', '--value') != '0'):
        raise RuntimeError('Graceful automatic restart contract changed')
    return int(run('systemctl', 'show', WORKER, '-p', 'MainPID', '--value'))


def drain():
    # Existing signal handler exits after the current iteration. Do not terminate
    # the cgroup or interrupt an image tool / upload child process.
    run('systemctl', 'kill', '--kill-who=main', '--signal=SIGTERM', WORKER)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--commit')
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--rollback', type=Path)
    args = parser.parse_args()
    if run('findmnt', '-n', '-o', 'UUID', '/mnt/data-disk') != '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':
        raise RuntimeError('Data mount mismatch')
    with (BASE/'public-readback-deploy.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        old_pid = worker_contract()
        before = ledger()
        if args.rollback:
            backup = args.rollback.resolve()
            if (BASE/'backups').resolve() not in backup.parents:
                raise RuntimeError('Invalid backup path')
            manifest = json.loads((backup/'manifest.json').read_text())
            rows = manifest['files']
            if manifest.get('kind') != 'youtube-public-readback' or {r['path'] for r in rows} != set(EXPECTED):
                raise RuntimeError('Invalid manifest')
            for row in rows:
                if sha(ROOT/row['path']) != row['installed_sha256'] or sha(backup/row['path']) != row['sha256']:
                    raise RuntimeError('Live drift or backup corruption: ' + row['path'])
            if args.check:
                print(json.dumps({'rollback_check': 'passed', 'backup': str(backup)})); return
            # Restore only the reviewed engine; retain all runtime ledgers.
            for row in reversed(rows):
                install(backup/row['path'], ROOT/row['path'], row['mode'])
            drain()
            print(json.dumps({'rollback': str(backup), 'previous_pid': old_pid, 'worker': 'draining', 'database': 'retained'}))
            return
        stage = Path(__file__).resolve().parents[1]
        if not args.commit or len(args.commit) != 40 or (stage/'.github-verified-commit').read_text().strip() != args.commit:
            raise RuntimeError('Exact GitHub-fetched release required')
        for name, expected in EXPECTED.items():
            if sha(ROOT/name) != expected:
                raise RuntimeError('Live baseline changed: ' + name)
            py_compile.compile(str(stage/name), doraise=True)
        if args.check:
            print(json.dumps({'check': 'passed', 'commit': args.commit, 'ledger': before})); return
        backup = BASE/'backups'/('public-readback-' + time.strftime('%Y%m%d-%H%M%S') + '-' + args.commit[:12])
        backup.mkdir(parents=True, exist_ok=False)
        rows = []
        for name, expected in EXPECTED.items():
            source, target = stage/name, ROOT/name
            saved = backup/name; saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, saved)
            if sha(saved) != expected:
                raise RuntimeError('Backup verification failed: ' + name)
            rows.append({'path': name, 'mode': target.stat().st_mode & 0o777,
                         'sha256': expected, 'installed_sha256': sha(source)})
        manifest = {'kind': 'youtube-public-readback', 'commit': args.commit, 'files': rows, 'ledger_before': before}
        (backup/'manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
        installed = []
        try:
            for row in rows:
                if sha(ROOT/row['path']) != row['sha256']:
                    raise RuntimeError('Concurrent live change: ' + row['path'])
                install(stage/row['path'], ROOT/row['path'], row['mode'])
                installed.append(row)
                if sha(ROOT/row['path']) != row['installed_sha256']:
                    raise RuntimeError('Install verification failed')
        except BaseException:
            for row in reversed(installed):
                install(backup/row['path'], ROOT/row['path'], row['mode'])
            raise
        drain()
        print(json.dumps({'commit': args.commit, 'backup': str(backup), 'previous_pid': old_pid,
                          'worker': 'draining', 'files': len(rows), 'ledger_before': before, 'ledger_after': ledger()}))


if __name__ == '__main__':
    main()
