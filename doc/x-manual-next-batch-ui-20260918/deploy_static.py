"""Deploy only the verified X manual-publish page from an already fetched commit."""
import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path


EXPECTED_BEFORE = 'cf0b7d73a96bd5005e1acd63607d908dbb23234815560e62b649b9ad646dc79c'
TARGETS = [Path('/usr/share/nginx/html/x-post-material-pool.html'), Path('/root/drama_material_service/static/x-post-material-pool.html')]
DB = Path('/var/lib/x-post-automation/accounts.sqlite3')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def snapshot():
    with sqlite3.connect('file:%s?mode=ro' % DB, uri=True) as conn:
        conn.execute('PRAGMA query_only=ON')
        counts = {table: conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0] for table in ('x_post_manual_run', 'x_post_queue', 'x_post_publish_log')}
        counts['published'] = conn.execute("SELECT COUNT(*) FROM x_post_publish_log WHERE status='published'").fetchone()[0]
        counts['latest_manual'] = conn.execute('SELECT id,status,publish_mode,scheduled_at,updated_at FROM x_post_manual_run ORDER BY id DESC LIMIT 1').fetchone()
    return {'ledger': counts, 'services': subprocess.check_output(['systemctl', 'show', 'drama-material-api.service', 'x-post-automation.service', '-p', 'Id', '-p', 'MainPID', '-p', 'ActiveState'], universal_newlines=True).strip()}


def replace(path, data, metadata):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=str(path.parent), prefix='.' + path.name + '.', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(str(temporary), stat.S_IMODE(metadata.st_mode))
        os.chown(str(temporary), metadata.st_uid, metadata.st_gid)
        os.replace(str(temporary), str(path))
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--repo', required=True)
    parser.add_argument('--commit', required=True)
    args = parser.parse_args()
    if not re.fullmatch('[0-9a-f]{40}', args.commit):
        raise SystemExit('Expected a full verified Git commit')
    mount = subprocess.check_output(['findmnt', '-n', '-o', 'UUID', '/mnt/data-disk'], universal_newlines=True).strip()
    if mount != '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8' or shutil.disk_usage('/mnt/data-disk').free < max(DB.stat().st_size * 3, 1024 ** 3):
        raise SystemExit('Verified data disk missing or insufficient space')
    page = subprocess.check_output(['git', '-C', args.repo, 'show', args.commit + ':static/x-post-material-pool.html'])
    if b'20260918-manual-next-batch' not in page:
        raise SystemExit('Unexpected page version')
    originals = [(target, target.read_bytes(), target.stat()) for target in TARGETS]
    if any(digest(data) != EXPECTED_BEFORE for _, data, _ in originals):
        raise SystemExit('Live page changed; rebase on current production before deploying')
    backup = Path('/mnt/data-disk/x-post-automation/backups') / ('manual-next-batch-' + datetime.utcnow().strftime('%Y%m%dT%H%M%SZ'))
    backup.mkdir(mode=0o700)
    before = snapshot()
    for index, (target, _, _) in enumerate(originals):
        shutil.copy2(str(target), str(backup / ('page-%s.html' % index)))
    with sqlite3.connect('file:%s?mode=ro' % DB, uri=True) as source:
        with sqlite3.connect(str(backup / 'accounts.sqlite3')) as destination:
            source.backup(destination)
            if destination.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise SystemExit('Database backup check failed')
    token_manifest = []
    for path in sorted(Path('/var/lib/x-post-automation/tokens').glob('*.json')):
        meta = path.stat()
        token_manifest.append({'file': path.name, 'sha256': digest(path.read_bytes()), 'mode': oct(stat.S_IMODE(meta.st_mode)), 'uid': meta.st_uid, 'gid': meta.st_gid})
    (backup / 'token-hashes.json').write_text(json.dumps(token_manifest, indent=2))
    rollback = '''import hashlib, os, shutil
from pathlib import Path
base = Path(__file__).resolve().parent
targets = %r
for target in targets:
    if hashlib.sha256(Path(target).read_bytes()).hexdigest() != %r:
        raise SystemExit('Page has changed since this deployment; inspect before rollback')
for i, target in enumerate(targets):
    temporary = target + '.manual-next-rollback'
    shutil.copy2(str(base / ('page-%%s.html' %% i)), temporary)
    os.replace(temporary, target)
print('Restored both static pages; preserved all database and token state')
''' % ([str(p) for p in TARGETS], digest(page))
    (backup / 'rollback.py').write_text(rollback)
    try:
        for target, data, metadata in originals:
            if digest(target.read_bytes()) != EXPECTED_BEFORE:
                raise RuntimeError('Concurrent page deployment detected')
            replace(target, page, metadata)
        if any(digest(path.read_bytes()) != digest(page) for path in TARGETS):
            raise RuntimeError('Deployed page checksum mismatch')
    except Exception:
        for target, data, metadata in originals:
            if digest(target.read_bytes()) == digest(page):
                replace(target, data, metadata)
        raise
    report = {'commit': args.commit, 'backup': str(backup), 'before_sha256': EXPECTED_BEFORE, 'after_sha256': digest(page), 'targets': [str(p) for p in TARGETS], 'before': before, 'after': snapshot(), 'service_restart': False}
    (backup / 'deployment.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report))


if __name__ == '__main__':
    main()
