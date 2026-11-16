"""Deploy only the uploader option filter, preserving publishing processes/state."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import py_compile
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[2]
LIVE = Path('/root/drama_material_service')
DATA = Path('/mnt/data-disk/youtube-uploader-login-20261116')
FILES = {
    'features/youtube_auto_publish/source.py': '301830bab04f1753d91d8c59867ce23833fac8d1018d47594783e1df997582cd',
    'features/youtube_auto_publish/runtime.py': '44ac112a8cd309c688dd9d7d1bbbde015187d49b66094bf6ab1efc8949a0c380',
}
UNITS = ('drama-material-api.service', 'youtube-auto-publish-worker.service',
         'drama-youtube-publish-worker.service', 'drama-youtube-unified-writer.service')
SCHEDULERS = '/opt/publish-scheduler-health/current/scripts/publish_scheduler_health.py'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def state():
    return {unit: subprocess.check_output(['systemctl', 'show', unit, '-p', 'MainPID',
                                          '-p', 'ActiveState', '-p', 'SubState'], text=True).splitlines()
            for unit in UNITS}


def install(source, target):
    metadata = target.stat()
    fd, temporary = tempfile.mkstemp(prefix='.uploader-filter-', dir=str(target.parent))
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(source.read_bytes())
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, stat.S_IMODE(metadata.st_mode))
        os.chown(temporary, metadata.st_uid, metadata.st_gid)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def activate(baseline, output):
    subprocess.run(['systemctl', 'restart', 'drama-material-api.service'], check=True)
    subprocess.run([sys.executable, SCHEDULERS, 'restore', '--baseline', str(baseline),
                    '--wait-seconds', '25', '--output', str(output)], check=True)
    for attempt in range(20):
        try:
            with urlopen('http://127.0.0.1:8787/api/auth/status', timeout=3) as response:
                if response.status == 200:
                    return
        except OSError:
            pass
        time.sleep(.5)
    raise RuntimeError('API did not become healthy; inspect deployment and rollback')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('commit')
    parser.add_argument('--rollback', type=Path)
    args = parser.parse_args()
    head = subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip()
    if head != args.commit:
        raise RuntimeError('Checkout differs from requested GitHub commit')
    uuid = subprocess.check_output(['findmnt', '-n', '-o', 'UUID', '--target', '/mnt/data-disk'], text=True).strip()
    if not os.path.ismount('/mnt/data-disk') or uuid != '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':
        raise RuntimeError('Expected data disk is not mounted')
    for name in FILES:
        committed = subprocess.check_output(['git', '-C', str(ROOT), 'show', head + ':' + name])
        if (ROOT / name).read_bytes() != committed:
            raise RuntimeError('Staged module differs from GitHub: ' + name)
        py_compile.compile(str(ROOT / name), doraise=True)
    if args.rollback:
        backup = args.rollback.resolve()
        if backup.parent != (DATA / 'backups').resolve():
            raise RuntimeError('Unexpected backup path')
        record = json.loads((backup / 'manifest.json').read_text())
        if record['commit'] != head:
            raise RuntimeError('Rollback belongs to another release')
        for name, hashes in record['files'].items():
            if digest(LIVE / name) != hashes['after'] or digest(backup / name) != hashes['before']:
                raise RuntimeError('Files changed since deployment; inspect before rollback: ' + name)
        for name in FILES:
            install(backup / name, LIVE / name)
        activate(backup / 'schedulers-before.json', backup / 'schedulers-rollback.json')
        record['rollback'] = {'at': datetime.now(timezone.utc).isoformat(), 'services': state()}
    else:
        for name, expected in FILES.items():
            if digest(LIVE / name) != expected:
                raise RuntimeError('Live module differs from inspected baseline: ' + name)
        backup = DATA / 'backups' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        backup.mkdir(parents=True, mode=0o700)
        record = {'commit': head, 'files': {}, 'services_before': state()}
        for name, expected in FILES.items():
            target = backup / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(LIVE / name, target)
            if digest(target) != expected:
                raise RuntimeError('Backup differs from inspected module: ' + name)
            record['files'][name] = {'before': expected, 'after': digest(ROOT / name)}
        subprocess.run([sys.executable, SCHEDULERS, 'snapshot', '--lane', 'youtube',
                        '--output', str(backup / 'schedulers-before.json')], check=True)
        (backup / 'manifest.json').write_text(json.dumps(record, indent=2) + '\n')
        for name in FILES:
            install(ROOT / name, LIVE / name)
            if digest(LIVE / name) != record['files'][name]['after']:
                raise RuntimeError('Installed digest mismatch: ' + name)
        activate(backup / 'schedulers-before.json', backup / 'schedulers-after.json')
        record['services_after'] = state()
        record['status'] = 'active'
    (backup / 'manifest.json').write_text(json.dumps(record, indent=2) + '\n')
    print(json.dumps({'status': 'rolled_back' if args.rollback else 'active',
                      'commit': head, 'backup': str(backup)}))


if __name__ == '__main__':
    main()
