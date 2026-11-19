"""Deploy only X/FB template static assets; preserve service processes and data."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil

from deploy_x_tag_template_ui import atomic, digest, run

SOURCE = Path(__file__).resolve().parents[1]
BASE = {
    'x-tag-auto-publish-template.html': {'b609d5c18a1ae3b6d7d4524b025940f9ab05c9aeea414b5439f96ee03bf5db4f'},
    'x-tag-auto-publish-template.js': {'871a0590049d9627b2e40240a2ad8d1ccd01c2b5e7ce9cbfc9915832f04084e0'},
    'x-tag-auto-publish-common.js': {'b37c85c27260016675dab68117d6925f21d46e6225e85cd278bb18d8bef14fc3'},
    'x-tag-auto-publish-templates.html': {'80e4f4ead4726f0087f1f62000bc1a914b71651885f36202d031fe090fbbb928'},
    'x-tag-auto-publish-templates.js': {'17a7dffd89ad1d21a7683297065dd74892947b52df35f6014e8ad13c3c41ed1d'},
    'fb-auto-publish-template.html': {'27dcc884135bf5143e6e37fbeeda03fe16a6e4ee2ec6867b136ee99eff7b4300', 'b8fdbfd1227ab06e3e6b0ba78c5f69c336d751cadc02b6eeb9ba4bd3ec6824b6'},
    'fb-auto-publish-template.js': {'e79a3eff3807a193f5650590fb9821e1a83a62b765ca9143977db44f6af06735'},
    'fb-auto-publish-common.js': {'3caf225f301796d8134a3620ab2e3272d76f8610b67a5703f409f16a6882d24e'},
}


def runtimes():
    return {key: str(Path(path).resolve()) for key, path in {
        'x-tag': '/opt/x-tag-publisher/current', 'fb': '/opt/fb-auto-post/current',
    }.items()}


def states():
    return {unit: run('systemctl', 'show', unit, '-p', 'ActiveState', '-p', 'MainPID') for unit in (
        'x-tag-auto.service', 'x-tag-post.service', 'x-tag-gateway.service',
        'x-tag-auto-scheduler.timer', 'x-tag-auto-runner.timer', 'x-tag-auto-runner.path',
        'fb-auto-post-service.service')}


def install(folder):
    assert run('findmnt', '-no', 'UUID', '/mnt/data-disk') == '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    assert not run('git', '-C', str(SOURCE), 'status', '--porcelain')
    commit = run('git', '-C', str(SOURCE), 'rev-parse', 'HEAD')
    assert run('git', '-C', str(SOURCE), 'branch', '-r', '--contains', commit)
    folder.mkdir(parents=True, exist_ok=False)
    os.chmod(folder, 0o700)
    info = dict(commit=commit, runtimes=runtimes(), units=states(), files={}, status='prepared')
    for name, allowed in BASE.items():
        data = (SOURCE / 'static' / name).read_bytes()
        runtime = Path(info['runtimes']['fb' if name.startswith('fb-') else 'x-tag'])
        roots = [Path('/root/drama_material_service/static'), runtime / 'static', Path('/usr/share/nginx/html')]
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
    manifest = folder / 'manifest.json'
    manifest.write_text(json.dumps(info, indent=2))
    assert runtimes() == info['runtimes'], 'runtime changed'
    for dest, row in info['files'].items():
        assert digest(dest) == row['before'], 'concurrent_change:' + dest
    # JavaScript first, then HTML with its new cache version.
    for dest, row in sorted(info['files'].items(), key=lambda item: item[0].endswith('.html')):
        atomic(Path(dest), (SOURCE / 'static' / row['source']).read_bytes(), row)
    assert states() == info['units'], 'service state changed'
    assert runtimes() == info['runtimes'], 'runtime changed'
    assert all(digest(dest) == row['after'] for dest, row in info['files'].items())
    info['status'] = 'active'
    manifest.write_text(json.dumps(info, indent=2))
    print(json.dumps(dict(commit=commit, files=len(info['files']), status='active', restarted=False, backup=str(folder))))


def rollback(folder):
    info = json.loads((folder / 'manifest.json').read_text())
    assert runtimes() == info['runtimes'], 'runtime changed'
    before = states()
    for dest, row in info['files'].items():
        assert digest(dest) in {row['after'], row['before']}, 'preserve_newer_changes:' + dest
    for dest, row in info['files'].items():
        atomic(Path(dest), Path(row['backup']).read_bytes(), row)
    assert states() == before
    assert all(digest(dest) == row['before'] for dest, row in info['files'].items())
    print('Static rollback complete; no service restart or data change')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['install', 'rollback'])
    parser.add_argument('--folder', type=Path, required=True)
    args = parser.parse_args()
    {'install': install, 'rollback': rollback}[args.action](args.folder.resolve())
