#!/usr/bin/env python3
"""Deploy the original-drama reference cover flow without changing SQL or publisher state."""
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
import sys
import time
import urllib.request

ROOT = Path('/root/drama_material_service')
PUBLIC = Path('/usr/share/nginx/html')
BASE = Path('/mnt/data-disk/deploy/youtube-auto-publish')
SQL = Path('/etc/youtube-auto-publish/material-source.sql')
SQL_SHA = 'c301d02c6bfad8fa62b86cc2befe83176a38616c4d42c9e385bfb78e0e838e01'
EXPECTED = {
    'features/youtube_auto_publish/drama.py': 'b88822315d894f31ae500fbb9d5fdbc1161ac8d013445d211bc65052278ef203',
    'features/youtube_auto_publish/service.py': '15b81334dba62ecf34c19bc20616018f13dd8562f5a9e6c42273c6ddd3ed18ac',
    'features/youtube_auto_publish/runtime.py': 'd3ff6a8dd0269934c2aae516c05bcfa4a813e796a849e35d44c03b0f22cb1767',
    'features/youtube_auto_publish/reference.py': None,
    'static/youtube-publish.html': '16f06bde145251fad391d1d1d53bc68b49532d879b1aa5e816952bc2dc576577',
    'static/youtube-publish.js': '38c07d85b2d34f821255d08f5c9b4fecab650d4972d41533bac3225869604b79',
    'static/youtube-publish.css': 'a4bcf3aa10eb80b6fe8c0acf6e143d45a5ebf5aa657c74e9413ba15840677016',
}
API = 'drama-material-api.service'
LEGACY = 'drama-youtube-publish-worker.service'
WORKER = 'youtube-auto-publish-worker.service'
UNITS = [API, LEGACY, WORKER, 'drama-youtube-unified-writer.service']
DB = ROOT / 'data/drama_material_jobs.sqlite3'


def run(*args):
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT).strip()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest() if Path(path).is_file() else None


def install(source, target, mode):
    target = Path(target)
    temp = target.with_name(target.name + '.youtube-reference-new')
    shutil.copyfile(source, temp)
    os.chmod(temp, mode)
    os.replace(temp, target)


def healthy():
    for _ in range(15):
        try:
            with urllib.request.urlopen('http://127.0.0.1:8787/api/auth/status', timeout=2) as response:
                if response.status == 200 and isinstance(json.load(response), dict):
                    return
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError('API health check failed')


def idle_snapshot(require_idle=True):
    with sqlite3.connect('file:' + str(DB) + '?mode=ro', uri=True) as db:
        active = db.execute("SELECT count(*) FROM drama_youtube_publish WHERE status NOT IN ('published','failed','unknown','partial_failed','cancelled') OR comment_status='publishing'").fetchone()[0]
        preparations = db.execute("SELECT count(*) FROM youtube_auto_preparation WHERE state IN ('queued_generation','generating','enqueue_pending') OR lease_until>strftime('%s','now')").fetchone()[0]
        if require_idle and (active or preparations):
            raise RuntimeError('Publishing or generation is active; leave running work untouched')
        return {'publishing': db.execute('SELECT workflow,status,comment_status,count(*) FROM drama_youtube_publish GROUP BY workflow,status,comment_status').fetchall(),
                'preparations': db.execute('SELECT state,count(*) FROM youtube_auto_preparation GROUP BY state').fetchall(),
                'links': db.execute('SELECT count(*) FROM drama_material_short_link').fetchone()[0]}


def stop():
    run('systemctl', 'stop', LEGACY, WORKER, API)


def start():
    run('systemctl', 'start', API)
    healthy()
    run('systemctl', 'start', LEGACY, WORKER)
    for unit in UNITS:
        if run('systemctl', 'is-active', unit) != 'active':
            raise RuntimeError('Inactive service: ' + unit)


def restore(backup, manifest):
    for row in manifest['files']:
        if row['sha256'] is not None and sha(backup / 'files' / row['target'].lstrip('/')) != row['sha256']:
            raise RuntimeError('Backup integrity mismatch')
    for row in manifest['files']:
        target = Path(row['target'])
        if row['sha256'] is None:
            if target.exists():
                target.unlink()
        else:
            install(backup / 'files' / row['target'].lstrip('/'), target, row['mode'])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--commit')
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--rollback', type=Path)
    args = parser.parse_args()
    if run('findmnt', '-n', '-o', 'UUID', '/mnt/data-disk') != '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':
        raise RuntimeError('Data mount mismatch')
    with (BASE / 'reference-deploy.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        targets = [ROOT / name for name in EXPECTED]
        targets += [PUBLIC / Path(name).name for name in EXPECTED if name.startswith('static/')]
        if args.rollback:
            backup = args.rollback.resolve()
            if (BASE / 'backups').resolve() not in backup.parents:
                raise RuntimeError('Invalid backup path')
            manifest = json.loads((backup / 'manifest.json').read_text())
            if manifest.get('kind') != 'youtube-cover-reference' or {r['target'] for r in manifest['files']} != {str(t) for t in targets}:
                raise RuntimeError('Invalid backup manifest')
            for row in manifest['files']:
                if sha(row['target']) != row['installed_sha256']:
                    raise RuntimeError('Newer live change exists; do not overwrite it')
            idle_snapshot()
            stop()
            try:
                idle_snapshot()
                restore(backup, manifest)
            finally:
                start()
            print(json.dumps({'rollback': str(backup), 'database': 'retained', 'material_sql': 'retained'}))
            return
        stage = Path(__file__).resolve().parents[1]
        if not args.commit or len(args.commit) != 40 or (stage / '.github-verified-commit').read_text().strip() != args.commit:
            raise RuntimeError('Exact GitHub-fetched commit required')
        inputs = [(stage / name, ROOT / name, expected) for name, expected in EXPECTED.items()]
        inputs += [(stage / name, PUBLIC / Path(name).name, expected) for name, expected in EXPECTED.items() if name.startswith('static/')]
        def check_baseline():
            if sha(SQL) != SQL_SHA:
                raise RuntimeError('Material SQL changed; inspect before deployment')
            for source, target, expected in inputs:
                if not source.is_file() or sha(target) != expected:
                    raise RuntimeError('Baseline changed: ' + str(target))
        check_baseline()
        for source, _, _ in inputs:
            if source.suffix == '.py':
                py_compile.compile(str(source), doraise=True)
        run(sys.executable, str(stage / 'scripts/verify_live_feature_guard.py'), '--root', str(stage))
        before = idle_snapshot()
        healthy()
        if args.check:
            print(json.dumps({'check': 'passed', 'commit': args.commit, 'ledger': before, 'files': len(inputs)}))
            return
        if shutil.disk_usage(BASE).free < DB.stat().st_size * 2 + 64 * 1024 * 1024:
            raise RuntimeError('Insufficient backup disk space')
        backup = BASE / 'backups' / ('reference-' + time.strftime('%Y%m%d-%H%M%S') + '-' + args.commit[:12])
        backup.mkdir(parents=True, exist_ok=False)
        records = []
        for source, target, _ in inputs:
            saved = backup / 'files' / str(target).lstrip('/')
            if target.exists():
                saved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, saved)
            records.append({'target': str(target), 'mode': target.stat().st_mode & 0o777 if target.exists() else 0o644,
                            'sha256': sha(target), 'installed_sha256': sha(source)})
        manifest = {'kind': 'youtube-cover-reference', 'commit': args.commit, 'files': records, 'ledger_before': before}
        (backup / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
        stopped = False
        changed = False
        try:
            stopped = True
            stop()
            check_baseline()
            before = idle_snapshot()
            with sqlite3.connect(DB) as src, sqlite3.connect(backup / 'jobs.sqlite3') as dst:
                src.backup(dst)
            os.chmod(backup / 'jobs.sqlite3', 0o600)
            changed = True
            for (source, target, _), row in zip(inputs, records):
                install(source, target, row['mode'])
            for source, target, _ in inputs:
                if sha(source) != sha(target):
                    raise RuntimeError('Installed byte mismatch')
            run(sys.executable, str(stage / 'scripts/verify_live_feature_guard.py'), '--root', str(ROOT), '--public-root', str(PUBLIC))
            start()
            if sha(SQL) != SQL_SHA:
                raise RuntimeError('Material SQL unexpectedly changed')
        except BaseException:
            if changed:
                stop()
                restore(backup, manifest)
            if stopped:
                start()
            raise
        result = {'commit': args.commit, 'backup': str(backup), 'services': {u: run('systemctl', 'is-active', u) for u in UNITS},
                  'ledger_before': before, 'ledger_after': idle_snapshot(require_idle=False), 'material_sql_sha256': sha(SQL),
                  'sha256': {str(target): sha(target) for _, target, _ in inputs}}
        (backup / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result))


if __name__ == '__main__':
    main()
