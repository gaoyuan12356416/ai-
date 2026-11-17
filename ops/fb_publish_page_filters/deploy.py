"""Install only the Page-picker script and update its existing HTML cache keys."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

SOURCE = Path(__file__).resolve().parents[2]
BASE = '2373dc190c0a5c48e152772b0964c921c3cc73fe'
VERSION = '20261117-page-filters1'
LIVE = Path('/opt/fb-auto-post/current')
PAGES = ('fb-video-publish.html', 'fb-pages.html', 'fb-video-publish-records.html')


def run(*args):
    return subprocess.check_output(args).decode().strip()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def pids():
    return run('systemctl', 'show', 'drama-material-api.service', 'fb-auto-post-service.service', '-p', 'Id', '-p', 'MainPID')


def save(folder, record):
    (folder / 'manifest.json').write_text(json.dumps(record, indent=2) + '\n')


def atomic(path, data):
    info = path.stat()
    temp = path.with_name(path.name + '.page-filters-next')
    with temp.open('xb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temp, info.st_mode)
    os.chown(temp, info.st_uid, info.st_gid)
    os.replace(temp, path)


def prepare(folder):
    assert run('findmnt', '-n', '-o', 'UUID', '/mnt/data-disk') == '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    assert folder.is_relative_to(Path('/mnt/data-disk')) and shutil.disk_usage('/mnt/data-disk').free > 100 * 1024**2
    assert not run('git', '-C', str(SOURCE), 'status', '--porcelain'), 'source_not_clean'
    before = subprocess.check_output(['git', '-C', str(SOURCE), 'show', BASE + ':static/fb-video-publish.js'])
    changed = (SOURCE / 'static/fb-video-publish.js').read_bytes()
    runtime = LIVE.resolve()
    replacements = []
    for root in (Path('/usr/share/nginx/html'), Path('/root/drama_material_service/static'), runtime / 'static'):
        path = root / 'fb-video-publish.js'
        assert path.read_bytes().replace(b'\r\n', b'\n') == before.replace(b'\r\n', b'\n'), 'concurrent_script_change:' + str(path)
        replacements.append((path, changed))
        for name in PAGES:
            path = root / name
            data, count = re.subn(rb'(/fb-video-publish\.js\?v=)[^"\s]+', lambda m: m[1] + VERSION.encode(), path.read_bytes())
            assert count == 1, 'script_reference_changed:' + str(path)
            replacements.append((path, data))
    folder.mkdir(parents=True, exist_ok=False)
    record = {'commit': run('git', '-C', str(SOURCE), 'rev-parse', 'HEAD'), 'runtime': str(runtime), 'pids_before': pids(), 'state': 'prepared', 'files': []}
    for index, (path, data) in enumerate(replacements):
        backup, staged = folder / f'{index}.before', folder / f'{index}.after'
        shutil.copy2(path, backup)
        staged.write_bytes(data)
        record['files'].append({'path': str(path), 'backup': str(backup), 'staged': str(staged), 'before': sha(backup.read_bytes()), 'after': sha(data)})
    save(folder, record)
    print(json.dumps({'state': record['state'], 'commit': record['commit'], 'files': len(record['files']), 'manifest': str(folder / 'manifest.json')}))


def apply(folder, rollback=False):
    record = json.loads((folder / 'manifest.json').read_text())
    assert str(LIVE.resolve()) == record['runtime'], 'runtime_changed'
    for item in record['files']:
        current = sha(Path(item['path']).read_bytes())
        allowed = {item['before'], item['after']} if rollback else {item['before']}
        assert current in allowed, 'concurrent_change:' + item['path']
        data = Path(item['backup'] if rollback else item['staged']).read_bytes()
        assert sha(data) == item['before' if rollback else 'after'], 'staged_data_changed'
    record['state'] = 'rolling_back' if rollback else 'installing'
    save(folder, record)
    for item in record['files']:
        path = Path(item['path'])
        assert sha(path.read_bytes()) in {item['before'], item['after']}, 'concurrent_change:' + str(path)
        atomic(path, Path(item['backup'] if rollback else item['staged']).read_bytes())
        assert sha(path.read_bytes()) == item['before' if rollback else 'after']
    record['state'] = 'rolled_back' if rollback else 'installed'
    record['pids_after'] = pids()
    save(folder, record)
    print(json.dumps({'state': record['state'], 'files': len(record['files']), 'pids_unchanged': record['pids_before'] == record['pids_after']}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('prepare', 'apply', 'rollback'))
    parser.add_argument('folder', type=Path)
    args = parser.parse_args()
    if args.action == 'prepare':
        prepare(args.folder.resolve())
    else:
        apply(args.folder.resolve(), rollback=args.action == 'rollback')
