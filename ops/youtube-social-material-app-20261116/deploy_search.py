"""Stage the exact-ID read fix; activation uses the existing API drain unit."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]
RELATIVE = 'features/youtube_auto_publish/source.py'
TARGET = Path('/root/drama_material_service') / RELATIVE
BEFORE = 'ddb1f80c3fc3308ae3a0589c880e8d1a2ae565ab155b647a9a3917a78ff10f1b'
spec = importlib.util.spec_from_file_location('sql_deploy', Path(__file__).with_name('deploy.py'))
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('commit')
    parser.add_argument('--rollback', type=Path)
    args = parser.parse_args()
    commit = subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip()
    if commit != args.commit:
        raise RuntimeError('Checkout differs from requested commit')
    value = (ROOT / RELATIVE).read_bytes()
    if value != subprocess.check_output(['git', '-C', str(ROOT), 'show', commit + ':' + RELATIVE]):
        raise RuntimeError('Source differs from GitHub commit')
    compile(value, RELATIVE, 'exec')
    uuid = subprocess.check_output(['findmnt', '-n', '-o', 'UUID', '--target', '/mnt/data-disk'], text=True).strip()
    if not os.path.ismount('/mnt/data-disk') or uuid != '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':
        raise RuntimeError('Expected data disk is not mounted')
    current = TARGET.read_bytes()
    metadata = TARGET.stat()
    if args.rollback:
        backup = args.rollback.resolve()
        if backup.parent != deploy.DATA.resolve():
            raise RuntimeError('Unexpected backup directory')
        record = json.loads((backup / 'search-manifest.json').read_text())
        value = (backup / 'source.py').read_bytes()
        if record['commit'] != commit or deploy.digest(current) != record['after'] or deploy.digest(value) != record['before']:
            raise RuntimeError('Source changed since deployment; inspect before rollback')
        deploy.atomic_install(value, metadata, TARGET)
        print(json.dumps({'status': 'rolled_back_pending_api_activation', 'backup': str(backup)}))
        return
    if deploy.digest(current) != BEFORE:
        raise RuntimeError('Live source differs from inspected baseline')
    backup = deploy.DATA / ('search-' + deploy.datetime.now(deploy.timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    backup.mkdir(mode=0o700)
    shutil.copy2(TARGET, backup / 'source.py')
    if deploy.digest((backup / 'source.py').read_bytes()) != BEFORE:
        raise RuntimeError('Source backup verification failed')
    record = {'commit': commit, 'before': BEFORE, 'after': deploy.digest(value), 'target': str(TARGET),
              'services_before': deploy.units(), 'activation': 'existing systemd API ExecStop/ExecStartPost drain'}
    (backup / 'search-manifest.json').write_text(json.dumps(record, indent=2) + '\n')
    deploy.atomic_install(value, metadata, TARGET)
    if deploy.digest(TARGET.read_bytes()) != record['after']:
        raise RuntimeError('Installed source digest mismatch')
    print(json.dumps({'status': 'installed_pending_api_activation', 'backup': str(backup), 'commit': commit}))


if __name__ == '__main__':
    main()
