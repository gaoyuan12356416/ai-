"""Install/roll back only the coordinator's stop/start helper from this checkout."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]
DROPIN = Path('/etc/systemd/system/pipeline-coordinator.service.d/70-upload-continuity.conf')
OLD_HELPER = '/opt/x-upload-continuity/current/scripts/pipeline_service_drain.py'


def sha(data):
    return hashlib.sha256(data).hexdigest()


def install(directory):
    mount = subprocess.check_output(['findmnt', '-no', 'UUID', '/mnt/data-disk'], text=True).strip()
    if mount != '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':
        raise RuntimeError('data_disk_not_mounted')
    directory = directory.resolve()
    if not str(directory).startswith('/mnt/data-disk/'):
        raise RuntimeError('backup_must_use_data_disk')
    revision = subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip()
    if subprocess.check_output(['git', '-C', str(ROOT), 'status', '--porcelain'], text=True).strip():
        raise RuntimeError('source_checkout_not_clean')
    before = DROPIN.read_bytes()
    text = before.decode()
    if text.count(OLD_HELPER) != 2 or 'TimeoutStopSec=infinity' not in text:
        raise RuntimeError('coordinator_helper_configuration_changed')
    directory.mkdir(parents=True, mode=0o700, exist_ok=False)
    (directory / '70-upload-continuity.conf.before').write_bytes(before)
    helper = str(ROOT / 'scripts/pipeline_service_drain.py')
    after = text.replace(OLD_HELPER, helper).encode()
    record = {'revision': revision, 'source': str(ROOT), 'dropin': str(DROPIN),
              'before_sha256': sha(before), 'after_sha256': sha(after), 'state': 'prepared'}
    (directory / 'record.json').write_text(json.dumps(record, indent=2) + '\n')
    if DROPIN.read_bytes() != before:
        raise RuntimeError('concurrent_dropin_change')
    temporary = DROPIN.with_suffix('.handoff-new')
    temporary.write_bytes(after)
    temporary.chmod(0o644)
    temporary.replace(DROPIN)
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    record['state'] = 'installed'
    (directory / 'record.json').write_text(json.dumps(record, indent=2) + '\n')
    print(json.dumps(record))


def rollback(directory):
    record = json.loads((directory / 'record.json').read_text())
    path = Path(record['dropin'])
    before = (directory / '70-upload-continuity.conf.before').read_bytes()
    if path != DROPIN or sha(before) != record['before_sha256'] or sha(path.read_bytes()) != record['after_sha256']:
        raise RuntimeError('rollback_configuration_conflict')
    temporary = path.with_suffix('.handoff-rollback')
    temporary.write_bytes(before)
    temporary.chmod(0o644)
    temporary.replace(path)
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    record['state'] = 'rolled_back'
    (directory / 'record.json').write_text(json.dumps(record, indent=2) + '\n')
    print(json.dumps(record))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('install', 'rollback'))
    parser.add_argument('--directory', type=Path, required=True)
    args = parser.parse_args()
    (install if args.action == 'install' else rollback)(args.directory)
