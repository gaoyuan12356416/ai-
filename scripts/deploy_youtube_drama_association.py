#!/usr/bin/env python3
"""Apply the standalone-material fix without replaying first-install configuration."""
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
    'features/drama_synthesis/core.py': '426c42375865a146aa22843ef5dbaf6962bb2555175807c7b7cb51c185b2872d',
    'features/youtube_auto_publish/templates.py': '0a02abef88f7da39dfa27946550276b8eca8752c380acff026292ae233b44a9c',
    'features/youtube_auto_publish/source.py': 'e371eaec8ad25a9c0d3390eddb2afac2a6e05123b13749832968c1bac3a71c27',
    'features/youtube_auto_publish/service.py': 'e5b72fce1f45bb549bb4e01fb887ce138b0acf4798d662d7af5c3b3f1a193c04',
    'features/youtube_auto_publish/runtime.py': 'ceeebe824d1b61f0d87da934f820e29ae37839efff097ffdb26192fafb235726',
    'features/youtube_auto_publish/drama.py': None,
    'static/youtube-publish.html': '45776fcf2a8d38eb5115d11acbaedbc5be1f0df39b5968e4592919fd0bd725ff',
    'static/youtube-publish.js': '4044663f7221222b436d77a79af31282d004dab89149902acd06cfbf8fab7e0b',
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
    temp = target.with_name(target.name + '.youtube-association-new')
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
    with (BASE / 'association-deploy.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        targets = [ROOT / name for name in EXPECTED]
        targets += [PUBLIC / Path(name).name for name in EXPECTED if name.startswith('static/')]
        if args.rollback:
            backup = args.rollback.resolve()
            if (BASE / 'backups').resolve() not in backup.parents:
                raise RuntimeError('Invalid backup path')
            manifest = json.loads((backup / 'manifest.json').read_text())
            if manifest.get('kind') != 'youtube-drama-association' or {r['target'] for r in manifest['files']} != {str(t) for t in targets}:
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
        backup = BASE / 'backups' / ('association-' + time.strftime('%Y%m%d-%H%M%S') + '-' + args.commit[:12])
        backup.mkdir(parents=True, exist_ok=False)
        records = []
        for source, target, _ in inputs:
            saved = backup / 'files' / str(target).lstrip('/')
            if target.exists():
                saved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, saved)
            records.append({'target': str(target), 'mode': target.stat().st_mode & 0o777 if target.exists() else 0o644,
                            'sha256': sha(target), 'installed_sha256': sha(source)})
        manifest = {'kind': 'youtube-drama-association', 'commit': args.commit, 'files': records, 'ledger_before': before}
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
