"""Install only X editor assets and Nginx response settings; preserve workers."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess

SOURCE = Path(__file__).resolve().parents[1]
FILES = [
    ('deploy/nginx/x-tag-publisher.conf', '/etc/nginx/default.d/x-tag-publisher.conf',
     '01dd559e697b673f4534650eba4c4808dea929ec3a2d775118450114e6912c6f'),
    ('deploy/nginx/backend-api-timing.conf', '/etc/nginx/conf.d/backend-api-timing.conf', None),
    ('deploy/nginx/backend-api-timing.logrotate', '/etc/logrotate.d/backend-api-timing', None),
]
for root, js, html in [
    ('/usr/share/nginx/html', '1e3ea1ac31c27a6f16c4c7399e119e6c721e73b7edd0498223913149dc286d37', '74ca734440e447c690380118ab97c7aa4279c9d9d9e0ea8a4f19a401652c6e28'),
    ('/root/drama_material_service/static', '1e3ea1ac31c27a6f16c4c7399e119e6c721e73b7edd0498223913149dc286d37', '74ca734440e447c690380118ab97c7aa4279c9d9d9e0ea8a4f19a401652c6e28'),
    ('/opt/x-tag-publisher/current/static', '74e2bb265ba42425592c887cc5d65d78c221089bd232c6249e4c9368bc928ce7', 'f36dcfd9f1af56572de6f1984ec075213a154fc51d319f9e67252f84fdaf1d88'),
]:
    for suffix, before in [('js', js), ('html', html)]:
        name = 'x-tag-auto-publish-template.' + suffix
        FILES.append(('static/' + name, root + '/' + name, before))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def pids():
    units = ['drama-material-api', 'x-tag-auto', 'x-tag-post', 'x-tag-gateway',
             'pipeline-node-cpu-main', 'moboreels-library']
    return {u: subprocess.check_output(['systemctl', 'show', u, '-p', 'MainPID', '--value'], text=True).strip() for u in units}


def write(path, content, metadata=None):
    current = metadata or (path.stat() if path.exists() else None)
    stage = path.with_name(path.name + '.backend-performance-stage')
    stage.write_bytes(content)
    os.chmod(stage, stat.S_IMODE(current.st_mode) if current else 0o644)
    if current:
        os.chown(stage, current.st_uid, current.st_gid)
    os.replace(stage, path)


def revert(rows, verify=True):
    for row in rows:
        path = Path(row['path'])
        if verify and digest(path) != row['after']:
            raise RuntimeError('Concurrent update retained: ' + str(path))
    for row in rows:
        path = Path(row['path'])
        if row['backup']:
            backup = Path(row['backup'])
            write(path, backup.read_bytes(), backup.stat())
        else:
            path.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['apply', 'rollback'])
    parser.add_argument('--state', required=True, type=Path)
    args = parser.parse_args()
    mount = subprocess.check_output(['findmnt', '-n', '-o', 'UUID', '/mnt/data-disk'], text=True).strip()
    if mount != '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':
        raise RuntimeError('Required data mount is unavailable')
    state = args.state.resolve()
    if not str(state).startswith('/mnt/data-disk/backend-performance-20261123/'):
        raise ValueError('State must be in the task deployment directory')
    if args.action == 'rollback':
        manifest = json.loads((state / 'manifest.json').read_text())
        revert(manifest['files'])
        subprocess.run(['nginx', '-t'], check=True)
        subprocess.run(['systemctl', 'reload', 'nginx'], check=True)
        print(json.dumps({'rollback': str(state), 'business_workers_restarted': False}))
        return
    rows = []
    # Inspect every destination before touching any active file.
    for relative, target, expected in FILES:
        path = Path(target).resolve()
        if digest(path) != expected:
            raise RuntimeError('Destination changed; inspect before overlay: ' + target)
        rows.append({'path': str(path), 'source': relative, 'before': expected,
                     'after': digest(SOURCE / relative), 'backup': None})
    state.mkdir(parents=True, exist_ok=False, mode=0o700)
    before_pids = pids()
    for i, row in enumerate(rows):
        path = Path(row['path'])
        if path.exists():
            backup = state / ('before-%02d' % i)
            shutil.copy2(path, backup)
            os.chown(backup, path.stat().st_uid, path.stat().st_gid)
            row['backup'] = str(backup)
    manifest = {'source': str(SOURCE), 'files': rows, 'pids_before': before_pids}
    (state / 'manifest.json').write_text(json.dumps(manifest, indent=2))
    log_dir = Path('/mnt/data-disk/backend-performance')
    log_dir.mkdir(mode=0o755, exist_ok=True)
    changed = []
    try:
        for row in rows:
            path = Path(row['path'])
            if digest(path) != row['before']:
                raise RuntimeError('Concurrent destination update: ' + str(path))
            write(path, (SOURCE / row['source']).read_bytes())
            changed.append(row)
        subprocess.run(['nginx', '-t'], check=True)
        subprocess.run(['systemctl', 'reload', 'nginx'], check=True)
    except Exception:
        revert(changed)
        subprocess.run(['nginx', '-t'], check=True)
        subprocess.run(['systemctl', 'reload', 'nginx'], check=True)
        raise
    manifest['pids_after'] = pids()
    manifest['installed_hashes_match'] = all(digest(Path(r['path'])) == r['after'] for r in rows)
    (state / 'manifest.json').write_text(json.dumps(manifest, indent=2))
    print(json.dumps({'state': str(state), 'installed_hashes_match': manifest['installed_hashes_match'],
                      'pids_before': before_pids, 'pids_after': manifest['pids_after']}))


if __name__ == '__main__':
    main()
