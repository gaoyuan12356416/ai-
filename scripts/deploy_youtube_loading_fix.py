#!/usr/bin/env python3
"""Deploy only the YouTube initialization fix from an exact GitHub archive."""
import argparse
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
    'app.py': 'd7ed2c8f41782cb752da14b2fe5b47c88ef04ff6c9a6ec3930f3fd2940a07abb',
    'features/youtube_auto_publish/service.py': '02cc81a419ce365c0a5d195286d2c973196ca404e6b5e392c8a6b7b94585bed4',
    'static/youtube-publish.html': 'ae05f4ec77fb8b1e6eedcf2a6338c67094445cc944193735d155880255134a3b',
    'static/youtube-publish.js': 'e10dc6bd0b010d8a3df1be3180a58417ed4d02d6e6ba20b8d19acfc441f96ae9',
    'static/youtube-publish.css': 'cae7806a488a679d6ae6b5092448f982672622afa1f086ec017645012d884a56',
}
API = 'drama-material-api.service'
LEGACY = 'drama-youtube-publish-worker.service'
UNITS = [API, LEGACY, 'youtube-auto-publish-worker.service', 'drama-youtube-unified-writer.service']


def run(*args):
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT).strip()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def install(source, target, mode):
    target = Path(target)
    temp = target.with_name(target.name + '.youtube-loading-new')
    shutil.copyfile(source, temp)
    os.chmod(temp, mode)
    os.replace(temp, target)


def healthy():
    for attempt in range(15):
        try:
            with urllib.request.urlopen('http://127.0.0.1:8787/api/auth/status', timeout=2) as response:
                if response.status == 200 and isinstance(json.load(response), dict):
                    return
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError('API health failed after restart')


def restart():
    run('systemctl', 'restart', API)
    healthy()
    # Requires= on the legacy worker can stop it when the API is restarted.
    run('systemctl', 'restart', LEGACY)
    for unit in UNITS:
        if run('systemctl', 'is-active', unit) != 'active':
            raise RuntimeError('Inactive service: ' + unit)


def rollback(backup):
    if (BASE / 'backups').resolve() not in backup.resolve().parents:
        raise RuntimeError('Backup path outside deployment backups')
    manifest = json.loads((backup / 'manifest.json').read_text())
    allowed = {str(ROOT / name) for name in EXPECTED}
    allowed.update(str(PUBLIC / Path(name).name) for name in EXPECTED if name.startswith('static/'))
    if manifest.get('kind') != 'youtube-loading-fix' or {r['target'] for r in manifest['files']} != allowed:
        raise RuntimeError('Invalid loading-fix backup manifest')
    for row in manifest['files']:
        source = backup / 'files' / row['target'].lstrip('/')
        if sha(source) != row['sha256']:
            raise RuntimeError('Backup integrity mismatch')
    for row in manifest['files']:
        install(backup / 'files' / row['target'].lstrip('/'), row['target'], row['mode'])
    restart()
    print(json.dumps({'rollback': str(backup), 'database': 'retained', 'material_sql': 'retained'}))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--commit')
    parser.add_argument('--rollback', type=Path)
    args = parser.parse_args()
    if args.rollback:
        rollback(args.rollback)
        return
    stage = Path(__file__).resolve().parents[1]
    if not args.commit or len(args.commit) != 40 or (stage / '.github-verified-commit').read_text().strip() != args.commit:
        raise RuntimeError('Exact GitHub-fetched commit required')
    if run('findmnt', '-n', '-o', 'UUID', '/mnt/data-disk') != '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':
        raise RuntimeError('Data mount mismatch')
    if sha(SQL) != SQL_SHA:
        raise RuntimeError('Material SQL changed; inspect before deployment')
    targets = [(stage / name, ROOT / name, expected) for name, expected in EXPECTED.items()]
    targets += [(stage / name, PUBLIC / Path(name).name, expected) for name, expected in EXPECTED.items() if name.startswith('static/')]
    for source, target, expected in targets:
        if sha(target) != expected:
            raise RuntimeError('Live file changed: ' + str(target))
        if source.suffix == '.py':
            py_compile.compile(str(source), doraise=True)
    run(sys.executable, str(stage / 'scripts/verify_live_feature_guard.py'), '--root', str(stage))
    with sqlite3.connect('file:' + str(ROOT / 'data/drama_material_jobs.sqlite3') + '?mode=ro', uri=True) as db:
        active = db.execute("SELECT count(*) FROM drama_youtube_publish WHERE status NOT IN ('published','failed','unknown','partial_failed','cancelled') OR comment_status='publishing'").fetchone()[0]
        if active:
            raise RuntimeError('YouTube upload/comment active; retry deployment later')
        counts = db.execute('SELECT status,comment_status,count(*) FROM drama_youtube_publish GROUP BY status,comment_status').fetchall()
    healthy()
    backup = BASE / 'backups' / ('loading-' + time.strftime('%Y%m%d-%H%M%S') + '-' + args.commit[:12])
    backup.mkdir(parents=True, exist_ok=False)
    records = []
    for source, target, expected in targets:
        saved = backup / 'files' / str(target).lstrip('/')
        saved.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, saved)
        records.append({'target': str(target), 'mode': target.stat().st_mode & 0o777, 'sha256': sha(saved)})
    manifest = {'kind': 'youtube-loading-fix', 'commit': args.commit, 'files': records, 'ledger_counts_before': counts}
    (backup / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    try:
        for source, target, expected in targets:
            install(source, target, target.stat().st_mode & 0o777)
        for source, target, _ in targets:
            if sha(source) != sha(target):
                raise RuntimeError('Installed bytes mismatch: ' + str(target))
        run(sys.executable, str(stage / 'scripts/verify_live_feature_guard.py'), '--root', str(ROOT), '--public-root', str(PUBLIC))
        restart()
        if sha(SQL) != SQL_SHA:
            raise RuntimeError('Material SQL unexpectedly changed')
    except BaseException:
        rollback(backup)
        raise
    result = {'commit': args.commit, 'backup': str(backup), 'sha256': {str(t): sha(t) for _, t, _ in targets}, 'services': {u: run('systemctl', 'is-active', u) for u in UNITS}, 'material_sql_sha256': sha(SQL)}
    (backup / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
