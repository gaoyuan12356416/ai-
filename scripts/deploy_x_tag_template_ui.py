"""Install only the template assets and metadata gateway from an exact Git checkout."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

SOURCE = Path(__file__).resolve().parents[1]
BASE = {
    'static/x-tag-auto-publish-templates.html': {'5d4958ff28abaafc32d38d9e601bf773ac3a236f9d68d8bf4deaf1f2eff965c7'},
    'static/x-tag-auto-publish-templates.js': {'19280f3f20345e8a314c4845e7efc4c73caf5ee9d79d6ab65137f71584766215'},
    'static/x-tag-auto-publish-template.html': {'9929bd11aa56e5063d9c0410706521b57d43dc9e130b4667bbd2edaf55dccff9', '49e88fd89fc3c02753c37d15ceb5ad2ca441167c712c9913efa530f9395a41db'},
    'static/x-tag-auto-publish-template.js': {'b2195cf219b2b960d43ed7fce5f29c30bd28c3667b9af97c6a1d833914b7a903'},
    'static/x-tag-template-layout.css': {''},
    'features/x_tag_publish/gateway.py': {'baa186195b9cb6d9de4a75d398ad8fe61c53358eb5b9602c6ebc94870c1de429'},
}
UNITS = ['x-tag-auto.service', 'x-tag-post.service', 'x-tag-auto-scheduler.timer',
         'x-tag-auto-runner.timer', 'x-tag-auto-runner.path']

def run(*args):
    return subprocess.check_output(args, text=True).strip()

def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest() if Path(path).exists() else ''

def states():
    return {unit: run('systemctl', 'show', unit, '-p', 'ActiveState', '-p', 'MainPID') for unit in UNITS}

def atomic(path, data, metadata):
    temp = path.with_name(path.name + '.template-ui-next')
    temp.write_bytes(data)
    os.chmod(temp, metadata['mode'])
    os.chown(temp, metadata['uid'], metadata['gid'])
    os.replace(temp, path)

def restart_gateway():
    # This process brokers writes as well as reads. Let active HTTP clients finish.
    for _ in range(60):
        active = run('ss', '-Htn', 'state', 'established', '( sport = :8898 )')
        if not active:
            subprocess.check_call(['systemctl', 'restart', 'x-tag-gateway.service'])
            return
        time.sleep(1)
    raise RuntimeError('Gateway still has active clients; installed files await a scoped restart')

def install(folder):
    assert run('findmnt', '-no', 'UUID', '/mnt/data-disk') == '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    assert not run('git', '-C', str(SOURCE), 'status', '--porcelain')
    commit = run('git', '-C', str(SOURCE), 'rev-parse', 'HEAD')
    assert run('git', '-C', str(SOURCE), 'branch', '-r', '--contains', commit)
    folder.mkdir(parents=True, exist_ok=False)
    os.chmod(folder, 0o700)
    runtime = Path('/opt/x-tag-publisher/current').resolve()
    info = dict(commit=commit, runtime=str(runtime), units=states(), files={}, status='preparing')
    for name in BASE:
        roots = [Path('/root/drama_material_service'), runtime]
        if name.startswith('static/'):
            roots.append(Path('/usr/share/nginx/html'))
        data = (SOURCE / name).read_bytes()
        if name.endswith('.py'):
            compile(data, name, 'exec')
        for root in roots:
            dest = root / (name.removeprefix('static/') if root.name == 'html' else name)
            before = digest(dest)
            assert before in BASE[name], 'preserve_live_changes:' + str(dest)
            stat = dest.stat() if dest.exists() else dest.parent.stat()
            row = dict(before=before, after=hashlib.sha256(data).hexdigest(),
                       mode=stat.st_mode & 0o777 if before else 0o644, uid=stat.st_uid, gid=stat.st_gid,
                       backup=str(folder / (str(len(info['files'])) + '.before')), source=name)
            if before:
                shutil.copy2(dest, row['backup'])
            info['files'][str(dest)] = row
    (folder / 'manifest.json').write_text(json.dumps(info, indent=2))
    for name, row in info['files'].items():
        assert digest(name) == row['before'], 'concurrent_change:' + name
    for name, row in info['files'].items():
        atomic(Path(name), (SOURCE / row['source']).read_bytes(), row)
    info['status'] = 'installed'
    (folder / 'manifest.json').write_text(json.dumps(info, indent=2))
    restart_gateway()
    assert states() == info['units'], 'publishing owner or trigger changed'
    assert run('systemctl', 'is-active', 'x-tag-gateway.service') == 'active'
    for name, row in info['files'].items():
        assert digest(name) == row['after'], name
    info['status'] = 'active'
    (folder / 'manifest.json').write_text(json.dumps(info, indent=2))
    print(json.dumps(dict(commit=commit, status='active', files=len(info['files']), backup=str(folder))))

def rollback(folder):
    info = json.loads((folder / 'manifest.json').read_text())
    assert str(Path('/opt/x-tag-publisher/current').resolve()) == info['runtime']
    baseline = states()
    for name, row in info['files'].items():
        assert digest(name) == row['after'], 'preserve_newer_changes:' + name
    for name, row in info['files'].items():
        if row['before']:
            atomic(Path(name), Path(row['backup']).read_bytes(), row)
        else:
            Path(name).unlink()
    restart_gateway()
    assert states() == baseline
    print('Code rollback complete; publishing data preserved')

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['install', 'rollback'])
    parser.add_argument('--folder', type=Path, required=True)
    args = parser.parse_args()
    {'install': install, 'rollback': rollback}[args.action](args.folder.resolve())
