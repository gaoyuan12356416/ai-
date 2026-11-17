"""Overlay only search methods and static assets; retain active runtime variants."""
import argparse
import ast
import contextlib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys

BASE = '51026cab5ca895a71f0f44be6d25bacf8db9902f'
SOURCE = Path(__file__).resolve().parents[2]
LIVE = Path('/opt/fb-auto-post/current')
MAIN = Path('/root/drama_material_service')
HEALTH = '/opt/publish-scheduler-health/current/scripts/publish_scheduler_health.py'
HELPER = Path('/mnt/data-disk/story-copy-src/e1e75352cb/scripts/deploy_publish_queue_isolation.py')
VERSION = '20261117-material-search1'
MODULE = 'features/fb_video_publish/sources.py'
JS = 'static/fb-video-publish.js'


def run(args, **kwargs):
    return subprocess.check_output(list(map(str, args)), **kwargs).decode().strip()


def digest(data):
    return hashlib.sha256(data.replace(b'\r\n', b'\n')).hexdigest()


def method(source, name):
    cls = next(node for node in ast.parse(source).body if isinstance(node, ast.ClassDef) and node.name == 'SourceRepository')
    node = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == name)
    lines = source.splitlines(keepends=True)
    return node, ''.join(lines[node.lineno - 1:node.end_lineno])


def overlay(before, baseline, changed):
    for name in ('_select', 'search_materials'):
        node, old = method(before, name)
        assert old == method(baseline, name)[1], 'concurrent_search_change:' + name
        lines = before.splitlines(keepends=True)
        lines[node.lineno - 1:node.end_lineno] = [method(changed, name)[1]]
        before = ''.join(lines)
    node, _ = method(before, 'search_materials')
    lines = before.splitlines(keepends=True)
    lines.insert(node.lineno - 1, method(changed, '_search_identity')[1] + '\n\n' + method(changed, '_text_search_rows')[1] + '\n\n')
    result = ''.join(lines).replace('PAGE_SIZE = 100\n', 'PAGE_SIZE = 100\nTEXT_SCAN_IDS = 1000\n', 1)
    compile(result, MODULE, 'exec')
    return result.encode()


def helper():
    expected = subprocess.check_output(['git', '-C', str(HELPER.parents[1]), 'show', 'e1e75352cbb43b71952ef4cc9808dbd253701c23:scripts/deploy_publish_queue_isolation.py'])
    assert digest(HELPER.read_bytes()) == digest(expected), 'switch_helper_changed'
    spec = importlib.util.spec_from_file_location('verified_fb_switch', HELPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def save(folder, record):
    path = folder / 'record.json'
    path.write_text(json.dumps(record, indent=2) + '\n')
    path.chmod(0o600)


def atomic(path, data):
    stat = path.stat()
    temp = path.with_name(path.name + '.material-search-next')
    assert not temp.exists()
    temp.write_bytes(data)
    os.chmod(temp, stat.st_mode)
    os.chown(temp, stat.st_uid, stat.st_gid)
    os.replace(temp, path)


def prepare(folder):
    commit = run(['git', '-C', SOURCE, 'rev-parse', 'HEAD'])
    assert not run(['git', '-C', SOURCE, 'status', '--porcelain']), 'source_not_clean'
    assert run(['findmnt', '-n', '-o', 'UUID', '/mnt/data-disk']) == '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    assert folder.is_relative_to(Path('/mnt/data-disk')) and shutil.disk_usage('/mnt/data-disk').free > 1024**3
    ops = helper()
    folder.mkdir(parents=True, mode=0o755, exist_ok=False)
    candidate = folder / 'candidate'
    original = LIVE.resolve()
    shutil.copytree(original, candidate, symlinks=True, ignore=shutil.ignore_patterns('.git', '__pycache__', '*.pyc'))
    ops.preserve_owners(original, candidate)
    baseline = subprocess.check_output(['git', '-C', SOURCE, 'show', BASE + ':' + MODULE]).decode()
    changed = (SOURCE / MODULE).read_text()
    rec = {'commit': commit, 'candidate': str(candidate), 'before_root': str(original), 'files': {}, 'main_pid': ops.pid('drama-material-api.service')}
    item = {'before_root': str(original), 'candidate': str(candidate), 'files': {}, 'state': 'prepared'}
    for root in (original, MAIN, Path('/usr/share/nginx/html')):
        for relative in ((MODULE,) if root == MAIN else ()) + ((JS,) if root in (MAIN, original) else ('fb-video-publish.js',)):
            path = root / relative
            data = overlay(path.read_text(), baseline, changed) if relative == MODULE else (SOURCE / JS).read_bytes()
            if root == original:
                target = candidate / relative
                atomic(target, data)
                item['files'][relative] = {'before': digest(path.read_bytes()), 'after': digest(data)}
            else:
                stage_file(folder, rec, path, data)
        if root == original:
            path = root / MODULE
            data = overlay(path.read_text(), baseline, changed)
            atomic(candidate / MODULE, data)
            item['files'][MODULE] = {'before': digest(path.read_bytes()), 'after': digest(data)}
        static = root / 'static' if root != Path('/usr/share/nginx/html') else root
        for name in ('fb-pages.html', 'fb-video-publish.html', 'fb-video-publish-records.html'):
            path = static / name
            if not path.exists():
                continue
            text, count = re.subn(r'(/fb-video-publish\.js\?v=)[^"\s]+', r'\g<1>' + VERSION, path.read_text())
            assert count == 1, 'script_tag_missing:' + str(path)
            data = text.encode()
            if root == original:
                rel = str(path.relative_to(original))
                atomic(candidate / rel, data)
                item['files'][rel] = {'before': digest(path.read_bytes()), 'after': digest(data)}
            else:
                stage_file(folder, rec, path, data)
    db = Path('/mnt/data-disk/fb-auto-post-publisher/fb-auto-post.sqlite3')
    with contextlib.closing(sqlite3.connect(db.as_uri() + '?mode=ro', uri=True)) as src:
        with contextlib.closing(sqlite3.connect(folder / 'fb-before.sqlite3')) as dst:
            src.backup(dst)
            assert dst.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
    (folder / 'fb-before.sqlite3').chmod(0o600)
    test = candidate / 'tests/test_fb_manual_material_search.py'
    test.parent.mkdir(exist_ok=True)
    shutil.copy2(SOURCE / 'tests/test_fb_manual_material_search.py', test)
    (folder / 'manifest.json').write_text(json.dumps({'lanes': {'fb': item}}, indent=2))
    (folder / 'manifest.json').chmod(0o600)
    save(folder, rec)
    print(json.dumps({'prepared': str(folder), 'commit': commit}))


def stage_file(folder, rec, path, data):
    name = str(len(rec['files']))
    (folder / ('before-' + name)).write_bytes(path.read_bytes())
    (folder / ('after-' + name)).write_bytes(data)
    rec['files'][str(path)] = {'name': name, 'before': digest(path.read_bytes()), 'after': digest(data)}


def activate(folder, rollback=False):
    ops = helper()
    rec = json.loads((folder / 'record.json').read_text())
    for path, record in rec['files'].items():
        assert digest(Path(path).read_bytes()) == record['after' if rollback else 'before'], 'later_file_change:' + path
    prefix = 'rollback-' if rollback else ''
    snapshot = folder / (prefix + 'schedulers-before.json')
    run([sys.executable, HEALTH, 'snapshot', '--lane', 'fb', '--output', snapshot])
    try:
        ops.switch(folder, 'fb', rollback, preserve_queue_scopes=True)
        for path, record in rec['files'].items():
            data = (folder / (('before-' if rollback else 'after-') + record['name'])).read_bytes()
            atomic(Path(path), data)
    finally:
        run([sys.executable, HEALTH, 'restore', '--baseline', snapshot, '--wait-seconds', '25', '--output', folder / (prefix + 'schedulers-after.json')])
    rec['status'] = 'rolled_back' if rollback else 'active'
    rec['main_pid_unchanged'] = rec['main_pid'] == ops.pid('drama-material-api.service')
    save(folder, rec)
    print(json.dumps({'status': rec['status'], 'main_pid_unchanged': rec['main_pid_unchanged'], 'ledgers_preserved': True}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'activate', 'rollback'])
    parser.add_argument('folder', type=Path)
    args = parser.parse_args()
    if args.action == 'prepare':
        prepare(args.folder.resolve())
    else:
        activate(args.folder.resolve(), args.action == 'rollback')
