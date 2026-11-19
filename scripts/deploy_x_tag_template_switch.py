"""Static-only status switch and banner cleanup. No service restarts."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil

from deploy_x_tag_template_ui import atomic, digest, run

SOURCE = Path(__file__).resolve().parents[1]
BASE = {
    'x-tag-auto-publish-templates.html': {'3f9ead6811508f83a6bc13de9ecf36a00f936c8bce112986be7d92a2333f230d'},
    'x-tag-auto-publish-templates.js': {'3412a3cdf2ea29ed16044c6bd7b354c5bab9ac60cf91ea1eaa1c4dde18e513f9'},
    'x-tag-template-layout.css': {'79dc7ba1c1183f20892b05d444c45f9a8573a135aa8de462369e11aa9dc3f80f'},
    'x-tag-auto-publish-template.html': {'e6b5cce97dd650fd361f46b847cc3fc8a27a5bd2a890f24e267a570886f33bea'},
    'x-tag-material-pool.html': {'dd8c8cdc13080fa371adb98e912e09005507af76ee1d05c1c252c5940dcc244d', 'b0d9be4e12dbd55d19cc73aae874f10fb7e1dda86564ea88bfb22469e4753e83'},
}

def states():
    return {unit: run('systemctl', 'show', unit, '-p', 'ActiveState', '-p', 'MainPID') for unit in (
        'x-tag-auto.service', 'x-tag-post.service', 'x-tag-gateway.service',
        'x-tag-auto-scheduler.timer', 'x-tag-auto-runner.timer', 'x-tag-auto-runner.path')}

def install(folder):
    assert run('findmnt', '-no', 'UUID', '/mnt/data-disk') == '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    assert not run('git', '-C', str(SOURCE), 'status', '--porcelain')
    commit = run('git', '-C', str(SOURCE), 'rev-parse', 'HEAD')
    assert run('git', '-C', str(SOURCE), 'branch', '-r', '--contains', commit)
    folder.mkdir(parents=True, exist_ok=False)
    os.chmod(folder, 0o700)
    runtime = Path('/opt/x-tag-publisher/current').resolve()
    info = dict(commit=commit, runtime=str(runtime), units=states(), files={})
    roots = [Path('/root/drama_material_service/static'), runtime / 'static', Path('/usr/share/nginx/html')]
    for name, allowed in BASE.items():
        data = (SOURCE / 'static' / name).read_bytes()
        for root in roots:
            dest = root / name
            before = digest(dest)
            assert before in allowed, 'preserve_live_changes:' + str(dest)
            stat = dest.stat()
            row = dict(before=before, after=hashlib.sha256(data).hexdigest(), mode=stat.st_mode & 0o777,
                       uid=stat.st_uid, gid=stat.st_gid, source=name,
                       backup=str(folder / (str(len(info['files'])) + '.before')))
            shutil.copy2(dest, row['backup'])
            info['files'][str(dest)] = row
    (folder / 'manifest.json').write_text(json.dumps(info, indent=2))
    for dest, row in info['files'].items():
        assert digest(dest) == row['before'], 'concurrent_change:' + dest
    for dest, row in info['files'].items():
        atomic(Path(dest), (SOURCE / 'static' / row['source']).read_bytes(), row)
    assert states() == info['units'], 'service state changed'
    assert all(digest(dest) == row['after'] for dest, row in info['files'].items())
    print(json.dumps(dict(commit=commit, files=len(info['files']), status='active', restarted=False, backup=str(folder))))

def rollback(folder):
    info = json.loads((folder / 'manifest.json').read_text())
    assert str(Path('/opt/x-tag-publisher/current').resolve()) == info['runtime']
    before = states()
    for dest, row in info['files'].items():
        assert digest(dest) == row['after'], 'preserve_newer_changes:' + dest
    for dest, row in info['files'].items():
        atomic(Path(dest), Path(row['backup']).read_bytes(), row)
    assert states() == before
    print('Static rollback complete; no service restart or data change')

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['install', 'rollback'])
    parser.add_argument('--folder', type=Path, required=True)
    args = parser.parse_args()
    {'install': install, 'rollback': rollback}[args.action](args.folder.resolve())
