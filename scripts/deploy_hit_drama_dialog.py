"""Publish the shared hit-drama dialog only; no restart or ledger mutation."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
from scripts.deploy_pipeline_runtime import verify_source
from scripts.deploy_hit_drama import atomic, run, save, sha

BASE = '1e2689e6'
UNITS = ('fb-auto-post-service.service', 'drama-material-api.service', 'x-tag-auto.service', 'x-tag-post.service')


def pids():
    return {unit: run('systemctl', 'show', unit, '-p', 'MainPID', '--value') for unit in UNITS}


def prepare(folder):
    assert run('findmnt', '-rn', '-o', 'UUID', '--mountpoint', '/mnt/data-disk') == '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    commit = verify_source(SOURCE, 'HEAD')
    asset = (SOURCE / 'static/hit-drama-publish.js').read_bytes()
    old = subprocess.check_output(['git', '-C', str(SOURCE), 'show', BASE + ':static/hit-drama-publish.js'])
    version = hashlib.sha256(asset).hexdigest()[:12]
    folder.mkdir(parents=True, exist_ok=False)
    info = dict(commit=commit, source=str(SOURCE), status='prepared', files={}, pids_before=pids())
    roots = [(Path('/usr/share/nginx/html'), ('fb', 'x-tag')),
             (Path('/root/drama_material_service/static'), ('fb', 'x-tag')),
             (Path('/opt/fb-auto-post/current').resolve() / 'static', ('fb',)),
             (Path('/opt/x-tag-publisher/current').resolve() / 'static', ('x-tag',))]
    for root, platforms in roots:
        js = root / 'hit-drama-publish.js'
        assert js.read_bytes().replace(b'\r\n', b'\n') == old, 'asset_changed:' + str(root)
        values = [(js, asset)]
        for platform in platforms:
            html = root / (platform + '-auto-publish-templates.html')
            text, count = re.subn(r'(/hit-drama-publish\.js)(?:\?[^"\']*)?', r'\1?v=' + version, html.read_text())
            assert count == 1, 'asset_reference_changed:' + str(html)
            values.append((html, text.encode()))
        for path, data in values:
            index = str(len(info['files']))
            backup, new = folder / (index + '.old'), folder / (index + '.new')
            shutil.copy2(path, backup)
            new.write_bytes(data)
            info['files'][str(path)] = dict(before=sha(path), after=hashlib.sha256(data).hexdigest(),
                                          backup=str(backup), next=str(new))
    save(folder / 'manifest.json', info)
    print(json.dumps(dict(status='prepared', commit=commit, files=len(info['files']))))


def apply(folder, rollback=False):
    info = json.loads((folder / 'manifest.json').read_text())
    for name, row in info['files'].items():
        assert sha(name) in {row['before'], row['after']}, 'live_asset_changed:' + name
    for name, row in info['files'].items():
        atomic(name, Path(row['backup' if rollback else 'next']).read_bytes())
    info['status'] = 'rolled_back' if rollback else 'active'
    info['pids_after'] = pids()
    save(folder / 'manifest.json', info)
    print(json.dumps(dict(status=info['status'], commit=info['commit'],
                          pids_unchanged=info['pids_before'] == info['pids_after'])))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'apply', 'rollback'))
    parser.add_argument('--folder', type=Path, required=True)
    args = parser.parse_args()
    folder = args.folder.resolve()
    assert folder.is_relative_to('/mnt/data-disk')
    if args.action == 'prepare':
        prepare(folder)
    else:
        apply(folder, args.action == 'rollback')
