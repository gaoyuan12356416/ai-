"""Install the FB template's three static assets; no service or state changes."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

NAMES = ('fb-auto-publish.css', 'fb-auto-publish-template.js', 'fb-auto-publish-template.html')
ROOTS = {'nginx': '/usr/share/nginx/html', 'app': '/root/drama_material_service/static', 'fb': '/opt/fb-auto-post/current/static'}
UNITS = ('drama-material-api.service', 'fb-auto-post-service.service')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def process_state():
    return {unit: run('systemctl', 'show', unit, '-p', 'MainPID', '-p', 'ActiveState') for unit in UNITS}


def replace(path, data):
    stat = path.stat()
    temporary = path.with_name(path.name + '.compact-deploy-tmp')
    with temporary.open('xb') as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, stat.st_mode)
    os.chown(temporary, stat.st_uid, stat.st_gid)
    os.replace(temporary, path)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('action', choices=['check', 'apply', 'rollback'])
    p.add_argument('--source', type=Path)
    p.add_argument('--commit')
    p.add_argument('--baseline', type=Path)
    p.add_argument('--deployment', type=Path, required=True)
    args = p.parse_args()
    folder = args.deployment.resolve()
    if Path('/mnt/data-disk') not in folder.parents:
        raise ValueError('deployment_must_use_data_disk')
    if run('findmnt', '-n', '-o', 'UUID', '/mnt/data-disk') != '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':
        raise ValueError('data_disk_unavailable')
    before = process_state()
    if args.action == 'rollback':
        report = json.loads((folder / 'manifest.json').read_text())
        for row in report['files']:
            if digest(Path(row['path']).read_bytes()) not in (row['before'], row['after']):
                raise ValueError('rollback_live_drift:' + row['path'])
        for row in report['files']:
            data = (folder / row['backup']).read_bytes()
            if digest(data) != row['before']:
                raise ValueError('backup_drift:' + row['path'])
        for row in report['files']:
            replace(Path(row['path']), (folder / row['backup']).read_bytes())
        assert all(digest(Path(r['path']).read_bytes()) == r['before'] for r in report['files'])
        assert process_state() == before, 'service_state_changed'
        print(json.dumps({'rolled_back': str(folder), 'restarted': []}))
        return
    commit = run('git', '-C', str(args.source), 'rev-parse', args.commit + '^{commit}')
    if commit != args.commit:
        raise ValueError('exact_commit_required')
    replacement = {name: subprocess.check_output(['git', '-C', str(args.source), 'show', commit + ':static/' + name]) for name in NAMES}
    expected = {(r['label'], r['name']): r['sha256'] for r in json.loads(args.baseline.read_text())}
    rows = []
    for label, root in ROOTS.items():
        for name in NAMES:
            path = (Path(root) / name).resolve(strict=True)
            old = digest(path.read_bytes())
            if old != expected[label, name]:
                raise ValueError('live_static_drift:' + str(path))
            rows.append({'path': str(path), 'name': name, 'backup': label + '/' + name, 'before': old, 'after': digest(replacement[name])})
    report = {'commit': commit, 'files': rows, 'services_before': before, 'restarted': []}
    if args.action == 'check':
        print(json.dumps({'checked': len(rows), 'commit': commit, 'services': before}))
        return
    folder.mkdir(mode=0o700, parents=True, exist_ok=False)
    for row in rows:
        backup = folder / row['backup']
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(row['path'], backup)
    (folder / 'manifest.json').write_text(json.dumps(report, indent=2))
    for row in rows:
        if digest(Path(row['path']).read_bytes()) != row['before']:
            raise ValueError('live_static_drift:' + row['path'])
        replace(Path(row['path']), replacement[row['name']])
    assert all(digest(Path(r['path']).read_bytes()) == r['after'] for r in rows)
    report['services_after'] = process_state()
    (folder / 'manifest.json').write_text(json.dumps(report, indent=2))
    assert report['services_after'] == before, 'service_state_changed'
    print(json.dumps({'installed': len(rows), 'commit': commit, 'deployment': str(folder), 'services_unchanged': True, 'restarted': []}))


if __name__ == '__main__':
    main()
