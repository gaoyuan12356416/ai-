"""Restore the public ordinary-X retirement on the main API and its two web roots."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
from scripts.deploy_drama_launch_range import remember
from scripts.deploy_hit_drama_multi_id import atomic
from scripts.deploy_x_material_effects import digest, run

BASE = '52a218fc0579d5fa144becdd6ed13ac5d1714494'
MAIN = Path('/root/drama_material_service')
WEB = Path('/usr/share/nginx/html')
HEALTH = '/opt/publish-scheduler-health/current/scripts/publish_scheduler_health.py'
API = 'drama-material-api.service'
NAV_SRC = re.compile(br'<script src="/quick-nav\.js(?:\?[^"]*)?"></script>')
CHARSET = b'  <meta charset="utf-8" />'
REDIRECT = b'<script src="/x-legacy-publish-redirect.js?v=20261112retirement1"></script>'


def merge_change(relative, old, new, live):
    if live == old or live == new:
        return new
    if relative.endswith('.html'):
        # Apply or reverse only the script tags that this release changed.
        old_tags, new_tags = NAV_SRC.findall(old), NAV_SRC.findall(new)
        if len(old_tags) != 1 or len(new_tags) != 1:
            raise ValueError('Ambiguous navigation script in ' + relative)
        def update(html):
            if NAV_SRC.findall(html) not in (old_tags, new_tags):
                raise ValueError('Conflicting navigation version in ' + relative)
            html = NAV_SRC.sub(lambda _: new_tags[0], html)
            newline = b'\r\n' if CHARSET + b'\r\n' in html else b'\n'
            redirect_line = b'  ' + REDIRECT + newline
            if REDIRECT in new and REDIRECT not in old and REDIRECT not in html:
                if html.count(CHARSET) != 1:
                    raise ValueError('Missing charset anchor in ' + relative)
                html = html.replace(CHARSET, CHARSET + newline + b'  ' + REDIRECT)
            elif REDIRECT in old and REDIRECT not in new and REDIRECT in html:
                if html.count(redirect_line) != 1:
                    raise ValueError('Conflicting redirect script in ' + relative)
                html = html.replace(redirect_line, b'')
            return html
        if update(old) == new:
            return update(live)
    raise ValueError('The live change needs to be merged for ' + relative)


def prepare(folder):
    commit = run('git', '-C', SOURCE, 'rev-parse', 'HEAD')
    if run('git', '-C', SOURCE, 'status', '--porcelain') or not run('git', '-C', SOURCE, 'branch', '-r', '--contains', commit):
        raise ValueError('Use a clean checkout of the pushed commit')
    folder.mkdir(parents=True, mode=0o700, exist_ok=False)
    info = {'commit': commit, 'files': {}}
    names = run('git', '-C', SOURCE, 'diff', '--name-only', BASE, commit, '--',
                'features/retired_modules.py', 'static/quick-nav.js', 'static/*.html').splitlines()
    for relative in names:
        paths = [MAIN / relative]
        if relative.startswith('static/'):
            paths.append(WEB / Path(relative).name)
        for path in paths:
            live = path.read_bytes()
            old = subprocess.check_output(['git', '-C', SOURCE, 'show', f'{BASE}:{relative}'])
            new = merge_change(relative, old, (SOURCE / relative).read_bytes(), live)
            if live == new:
                continue
            remember(folder, info, path, new)
            row = info['files'][str(path.resolve())]
            if Path(row['backup']).read_bytes() != live or digest(row['backup']) != row['before']:
                raise ValueError('File changed during backup: ' + str(path))
    (folder / 'manifest.json').write_text(json.dumps(info, indent=2) + '\n')
    print(f"Prepared {len(info['files'])} files from {commit}", flush=True)


def planned_changes(folder, action):
    rows = json.loads((folder / 'manifest.json').read_text())['files']
    updates = []
    for name, row in rows.items():
        old, new = Path(row['backup']).read_bytes(), Path(row['next']).read_bytes()
        if hashlib.sha256(old).hexdigest() != row['before'] or hashlib.sha256(new).hexdigest() != row['after']:
            raise ValueError('Changed release backup: ' + name)
        if action == 'rollback':
            old, new = new, old
        path = Path(name)
        live = path.read_bytes()
        data = merge_change(name, old, new, live)
        if data != live:
            updates.append((path, row, live, data))
    return updates


def switch(folder, action):
    updates = planned_changes(folder, action)
    baseline = folder / (action + '-schedulers-before.json')
    result = folder / (action + '-schedulers-after.json')
    if result.exists() and json.loads(result.read_text()).get('ok') and not updates:
        print(f'{action}: already applied and verified')
        return
    if not baseline.exists():
        run(sys.executable, HEALTH, 'snapshot', '--lane', 'youtube', '--output', baseline)
    for path, row, live, data in updates:
        if path.read_bytes() != live:
            raise ValueError('Concurrent change: ' + str(path))
        atomic(path, data, row)
    print(f'{action}: installed files, requesting managed restart of {API}', flush=True)
    run('systemctl', 'restart', API)
    run(sys.executable, HEALTH, 'restore', '--baseline', baseline, '--wait-seconds', '25', '--output', result)
    print(f'{action}: {API} {run("systemctl", "is-active", API)}', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'apply', 'check-rollback', 'rollback'))
    parser.add_argument('folder', type=Path)
    args = parser.parse_args()
    folder = args.folder.resolve()
    if Path('/mnt/data-disk') not in folder.parents or run('findmnt', '-rn', '-o', 'UUID', '--mountpoint', '/mnt/data-disk') != '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':
        parser.error('Use a release directory on the mounted data disk')
    if args.action == 'prepare':
        prepare(folder)
    elif args.action == 'check-rollback':
        print('Verified rollback for', len(planned_changes(folder, 'rollback')), 'files')
    else:
        switch(folder, args.action)
