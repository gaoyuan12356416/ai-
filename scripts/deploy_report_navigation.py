"""Install the shared report sidebar and its two pages without restarting a service."""
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

ROOTS = (Path('/root/drama_material_service/static'), Path('/usr/share/nginx/html'))
DIST = SOURCE / 'static'
NAV_SRC = re.compile(rb"""(<script\b[^>]*?\bsrc=["'](?:/|\./)?quick-nav\.js)(?:\?[^"']*)?(["'])""", re.I)
PREVIOUS_BLOBS = {
    'quick-nav.js': '0d6284f876a6039344e1c15857a5de5aee848f5c',
    'ui-topbar.js': '0417b5ea8062983424d191944b87e83cc75288d2',
    'socialkit-analytics.js': '38dc67fae868518b9ae18ca8b685934059be5b3f',
    'x-material-effects.js': '466d7c85b6ec90d7af20e7db1973a2c52924a9ff',
    'x-material-effects.css': None,
}
X_HTML = 'x-material-effects.html'
X_HTML_PREVIOUS = 'f646ce2a2a923ee33c4aacfff2c9866bf5346e51'


def content_version(path):
    return hashlib.sha256(path.read_bytes().replace(b'\r\n', b'\n')).hexdigest()[:12].encode()


def pin_scripts(html, filename):
    version = content_version(DIST / 'quick-nav.js')
    updated = NAV_SRC.sub(lambda m: m[1] + b'?v=' + version + m[2], html)
    if filename == 'socialkit-analytics.html':
        for name in ('ui-topbar.js', 'socialkit-analytics.js'):
            pattern = rb"""(<script\b[^>]*?\bsrc=["']/""" + re.escape(name.encode()) + rb""")(?:\?[^"']*)?(["'])"""
            version = content_version(DIST / name)
            updated, count = re.subn(pattern, lambda m: m[1] + b'?v=' + version + m[2], updated)
            if count != 1:
                raise ValueError(f'Missing or ambiguous {name} in {filename}')
    return updated


def blob_id(data):
    return hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()


def plan(root):
    for name, expected in PREVIOUS_BLOBS.items():
        path = root / name
        before = path.read_bytes() if path.exists() else None
        after = (DIST / name).read_bytes()
        if before != after:
            if (blob_id(before) if before is not None else None) != expected:
                raise ValueError(f'Merge concurrent asset change in {path}')
            yield path, before, after

    for path in sorted(root.glob('*.html')):
        before = path.read_bytes()
        if path.name == X_HTML:
            after = (DIST / X_HTML).read_bytes()
            # Other static releases may have updated only the navigation cache token.
            if before != after and blob_id(before) != X_HTML_PREVIOUS:
                original = subprocess.check_output(['git', '-C', SOURCE, 'cat-file', 'blob', X_HTML_PREVIOUS])
                if pin_scripts(before, X_HTML) != pin_scripts(original, X_HTML):
                    raise ValueError(f'Merge concurrent page change in {path}')
        else:
            after = pin_scripts(before, path.name) if NAV_SRC.search(before) else before
        if before != after:
            yield path, before, after


def prepare(folder):
    commit = run('git', '-C', SOURCE, 'rev-parse', 'HEAD')
    if run('git', '-C', SOURCE, 'status', '--porcelain') or not run('git', '-C', SOURCE, 'branch', '-r', '--contains', commit):
        raise ValueError('Use a clean checkout of the pushed commit')
    folder.mkdir(mode=0o700, parents=True, exist_ok=False)
    manifest = {'commit': commit, 'files': {}}
    for root in ROOTS:
        for path, before, after in plan(root):
            if path.is_symlink():
                raise ValueError(f'Inspect static symlink before updating {path}')
            remember(folder, manifest, path, after)
            row = manifest['files'][str(path.resolve())]
            if row['before'] != (hashlib.sha256(before).hexdigest() if before is not None else ''):
                raise ValueError(f'Changed while preparing {path}')
            if row['existed'] and digest(row['backup']) != row['before']:
                raise ValueError(f'Backup mismatch for {path}')
    (folder / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(f"Prepared {len(manifest['files'])} static files for {commit}")


def target_bytes(path, row, action):
    before = Path(row['backup']).read_bytes() if row['existed'] else None
    after = Path(row['next']).read_bytes()
    current = path.read_bytes() if path.exists() else None
    origin, target = (before, after) if action == 'apply' else (after, before)
    if current == target or current == origin:
        return current, target
    # An unrelated change to a consuming page can coexist with a URL-only rollback.
    if path.suffix == '.html' and path.name != X_HTML and current is not None:
        pattern = re.compile(rb"""(<script\b[^>]*?\bsrc=["'](?:/|\./)?)(quick-nav\.js|ui-topbar\.js|socialkit-analytics\.js)(\?[^"']*)?(["'])""", re.I)
        def urls(data):
            return {m[2]: m[3] or b'' for m in pattern.finditer(data)}
        old_urls, new_urls = urls(origin), urls(target)
        changes = {key for key in old_urls if key in new_urls and old_urls[key] != new_urls[key]}
        seen = set()
        def replace(match):
            key = match[2]
            if key not in changes:
                return match[0]
            if (match[3] or b'') not in (old_urls[key], new_urls[key]):
                raise ValueError(f'Newer script version in {path}')
            seen.add(key)
            return match[1] + key + new_urls[key] + match[4]
        result = pattern.sub(replace, current)
        if changes and seen == changes:
            return current, result
    raise ValueError(f'Merge concurrent change in {path}')


def switch(folder, action):
    rows = json.loads((folder / 'manifest.json').read_text())['files']
    entries = list(rows.items())
    if action == 'rollback':
        entries.reverse()
    pending = []
    for name, row in entries:
        path = Path(name)
        if digest(row['backup']) != row['before'] or digest(row['next']) != row['after']:
            raise ValueError(f'Invalid release backup: {path}')
        current, target = target_bytes(path, row, action)
        if current != target:
            pending.append((path, row, current, target))
    for path, row, current, target in pending:
        if (path.read_bytes() if path.exists() else None) != current:
            raise ValueError(f'Concurrent change: {path}')
        if target is None:
            path.unlink()
        else:
            atomic(path, target, row)
        if (path.read_bytes() if path.exists() else None) != target:
            raise ValueError(f'Verification failed: {path}')
    print(f'{action}: verified {len(entries)} static files')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('prepare', 'apply', 'rollback'))
    parser.add_argument('folder', type=Path)
    args = parser.parse_args()
    folder = args.folder.resolve()
    if not folder.is_relative_to(Path('/mnt/data-disk/report-navigation')) or run(
        'findmnt', '-rn', '-o', 'UUID', '--mountpoint', '/mnt/data-disk'
    ) != '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':
        parser.error('Use the report-navigation directory on the mounted data disk')
    if args.action == 'prepare':
        prepare(folder)
    else:
        switch(folder, args.action)
