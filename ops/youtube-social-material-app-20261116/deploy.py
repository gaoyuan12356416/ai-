"""Install or roll back only the hot-read YouTube material SELECT."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
SOURCE = 'ops/post-product-scope-20261113/youtube-material-source.sql'
TARGET = Path('/etc/youtube-auto-publish/material-source.sql')
DATA = Path('/mnt/data-disk/youtube-material-product-20261116')
BEFORE = '2d1270ce6c961dc813e325da2afc6805db69e9e1f3e072f0d4f47098d8c00906'
UNITS = ('drama-material-api.service', 'youtube-auto-publish-worker.service',
         'pipeline-youtube-canonical.service')


def digest(value):
    return hashlib.sha256(value).hexdigest()


def units():
    return {unit: subprocess.check_output(
        ['systemctl', 'show', unit, '-p', 'MainPID', '-p', 'ActiveState'],
        text=True).strip().splitlines() for unit in UNITS}


def atomic_install(value, metadata):
    fd, name = tempfile.mkstemp(prefix='.material-source-', dir=str(TARGET.parent))
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, stat.S_IMODE(metadata.st_mode))
        os.chown(name, metadata.st_uid, metadata.st_gid)
        os.replace(name, TARGET)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('commit')
    parser.add_argument('--rollback', type=Path)
    args = parser.parse_args()
    commit = subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip()
    if commit != args.commit:
        raise RuntimeError('Checkout differs from requested commit')
    source = (ROOT / SOURCE).read_bytes()
    committed = subprocess.check_output(['git', '-C', str(ROOT), 'show', commit + ':' + SOURCE])
    if source != committed:
        raise RuntimeError('SQL differs from committed source')
    sys.path.insert(0, str(ROOT))
    from features.youtube_auto_publish.source import validate_sql
    validate_sql(source.decode('utf-8'))
    mount = subprocess.check_output(['findmnt', '-n', '-o', 'UUID', '--target', '/mnt/data-disk'], text=True).strip()
    if not os.path.ismount('/mnt/data-disk') or mount != '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':
        raise RuntimeError('Expected data disk is not mounted')
    current = TARGET.read_bytes()
    metadata = TARGET.stat()
    before_units = units()
    if args.rollback:
        backup = args.rollback.resolve()
        if backup.parent != DATA.resolve():
            raise RuntimeError('Unexpected backup directory')
        record = json.loads((backup / 'manifest.json').read_text())
        value = (backup / 'material-source.sql').read_bytes()
        if record['commit'] != commit or digest(current) != record['after'] or digest(value) != record['before']:
            raise RuntimeError('SQL changed since deployment; inspect before rollback')
        atomic_install(value, metadata)
        record['rollback'] = {'at': datetime.now(timezone.utc).isoformat(), 'sha256': digest(TARGET.read_bytes()),
                              'services_before': before_units, 'services_after': units()}
        (backup / 'manifest.json').write_text(json.dumps(record, indent=2) + '\n')
        print(json.dumps({'status': 'rolled_back', 'backup': str(backup), 'sha256': digest(value)}))
        return
    if digest(current) != BEFORE:
        raise RuntimeError('Current SQL differs from inspected baseline')
    DATA.mkdir(parents=True, exist_ok=True)
    backup = DATA / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup.mkdir(mode=0o700)
    shutil.copy2(TARGET, backup / 'material-source.sql')
    if digest((backup / 'material-source.sql').read_bytes()) != BEFORE:
        raise RuntimeError('SQL backup verification failed')
    record = {'commit': commit, 'source': str(ROOT / SOURCE), 'target': str(TARGET),
              'before': BEFORE, 'after': digest(source), 'services_before': before_units}
    (backup / 'manifest.json').write_text(json.dumps(record, indent=2) + '\n')
    atomic_install(source, metadata)
    if digest(TARGET.read_bytes()) != record['after']:
        raise RuntimeError('Installed SQL digest mismatch')
    record.update(status='installed', services_after=units())
    (backup / 'manifest.json').write_text(json.dumps(record, indent=2) + '\n')
    print(json.dumps({'status': 'installed', 'commit': commit, 'backup': str(backup),
                      'sha256': record['after'], 'services_unchanged': before_units == record['services_after']}))


if __name__ == '__main__':
    main()
