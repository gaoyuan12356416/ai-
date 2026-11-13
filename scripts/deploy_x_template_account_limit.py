"""Remove the X tag template account cap with a scoped, reversible overlay."""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import urllib.request

SOURCE = Path(__file__).resolve().parents[1]
ROOT = Path('/mnt/data-disk/x-template-account-limit-20261113')
CURRENT = Path('/opt/x-tag-publisher/current')
VALIDATION = 'features/x_auto_posts/validation.py'
EDITOR = 'x-tag-auto-publish-template.js'
PAGE = 'x-tag-auto-publish-template.html'
BASE = {
    VALIDATION: 'b707dd9eb9912b2f4b889672862a4bb96efa87d3c8af40f2bdec565ee1c535d3',
    'static/' + EDITOR: '1e3318a9e40b9dc7e7baf7165356ac9c326eabbbf6a36e7cca2603ad87b05cdc',
    'static/' + PAGE: '4d16117568bb2230296d3ec953f837e19ddfa81cfd1fa67a919141f6e51712b6',
}
GUARD = '    if (state.selectedAccountIds.size > 100) throw new Error("一个模板最多选择 100 个 X 账号。");\n'
AUTO = 'x-tag-auto.service'
TRIGGERS = ('x-tag-auto-scheduler.timer', 'x-tag-auto-runner.timer', 'x-tag-auto-runner.path')
OTHER = ('x-tag-post.service', 'x-tag-gateway.service', 'x-post-automation.service', 'drama-material-api.service')
HEALTH = '/opt/publish-scheduler-health/current/scripts/publish_scheduler_health.py'


def run(*args):
    return subprocess.run(list(map(str, args)), check=True, capture_output=True, text=True).stdout.strip()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def write_record(folder, info):
    temp = folder / 'manifest.next'
    temp.write_text(json.dumps(info, indent=2) + '\n', encoding='utf-8')
    temp.replace(folder / 'manifest.json')


def atomic(path, data):
    stat = path.stat()
    temp = path.with_name(path.name + '.account-count-next')
    temp.write_bytes(data)
    os.chmod(temp, stat.st_mode & 0o777)
    os.chown(temp, stat.st_uid, stat.st_gid)
    temp.replace(path)


def states(units):
    result = {}
    for unit in units:
        result[unit] = dict(line.split('=', 1) for line in run(
            'systemctl', 'show', unit, '-p', 'ActiveState,SubState,MainPID,Result,NextElapseUSecRealtime').splitlines())
    return result


def scheduler(action, folder):
    args = [sys.executable, HEALTH, action, '--lane', 'x', '--output', str(folder / ('schedulers-' + action + '.json'))]
    if action == 'restore':
        args += ['--baseline', str(folder / 'schedulers-snapshot.json'), '--wait-seconds', '25']
    result = subprocess.run(args, capture_output=True, text=True)
    (folder / ('schedulers-' + action + '.txt')).write_text(result.stdout + result.stderr)
    return result.returncode


def prepare():
    assert run('findmnt', '-n', '-o', 'UUID', '--mountpoint', '/mnt/data-disk') == '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    commit = run('git', '-C', SOURCE, 'rev-parse', 'HEAD')
    assert not run('git', '-C', SOURCE, 'status', '--porcelain'), 'source_not_clean'
    assert run('git', '-C', SOURCE, 'branch', '-r', '--contains', commit), 'source_not_on_remote'
    live = CURRENT.resolve(strict=True)
    for relative, sha in BASE.items():
        assert digest((live / relative).read_bytes()) == sha, 'live_changed:' + relative
    folder = ROOT / commit[:12]
    folder.mkdir(parents=True, exist_ok=False)
    (folder / 'backups').mkdir(mode=0o700)
    staged = folder / 'staged-runtime'
    shutil.copytree(live, staged, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.git'))
    validation = (SOURCE / VALIDATION).read_bytes()
    (staged / VALIDATION).write_bytes(validation)
    editor = (live / 'static' / EDITOR).read_text(encoding='utf-8')
    assert editor.count(GUARD) == 1
    editor = editor.replace(GUARD, '').encode('utf-8')
    page, count = re.subn(r'(/' + re.escape(EDITOR) + r')(?:\?[^"\']*)?',
                         r'\1?v=' + digest(editor)[:12], (live / 'static' / PAGE).read_text(encoding='utf-8'))
    assert count == 1
    payloads = {VALIDATION: validation, 'static/' + EDITOR: editor, 'static/' + PAGE: page.encode('utf-8')}
    for relative, data in payloads.items():
        (staged / relative).write_bytes(data)
    for name in ('test_x_auto_post_validation.py', 'test_x_auto_post_service.py'):
        shutil.copy2(SOURCE / 'scripts' / name, staged / 'scripts' / name)
    result = subprocess.run([sys.executable, '-m', 'unittest', 'scripts.test_x_auto_post_validation',
                            'scripts.test_x_auto_post_service.XAutoPostServiceTests.test_124_account_save_roundtrip_and_idempotent_task_creation',
                            '-v'], cwd=staged, capture_output=True, text=True)
    (folder / 'tests.txt').write_text(result.stdout + result.stderr)
    if result.returncode:
        print(result.stdout + result.stderr)
        raise RuntimeError('isolated_tests_failed')
    targets = {live / relative: staged / relative for relative in payloads}
    for root in (Path('/usr/share/nginx/html'), Path('/root/drama_material_service/static')):
        for name in (EDITOR, PAGE):
            assert digest((root / name).read_bytes()) == BASE['static/' + name], 'static_changed:' + str(root / name)
            targets[root / name] = staged / 'static' / name
    manifest = json.loads((live / 'tag-runtime-manifest.json').read_text())
    for relative, data in payloads.items():
        manifest['files'][relative] = digest(data)
    manifest['account_count_overlay_commit'] = commit
    (staged / 'tag-runtime-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    targets[live / 'tag-runtime-manifest.json'] = staged / 'tag-runtime-manifest.json'
    info = dict(commit=commit, runtime=str(live), status='prepared', files={})
    for index, (target, source) in enumerate(targets.items()):
        backup = folder / 'backups' / str(index)
        shutil.copy2(target, backup)
        info['files'][str(target)] = dict(backup=str(backup), source=str(source),
                                       before=digest(backup.read_bytes()), after=digest(source.read_bytes()))
    db = Path('/mnt/data-disk/x-tag-publisher/auto/x-auto-post.sqlite3')
    with closing(sqlite3.connect(db.as_uri() + '?mode=ro', uri=True)) as source, closing(sqlite3.connect(folder / 'backups' / 'auto.sqlite3')) as destination:
        source.backup(destination)
        assert destination.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
    write_record(folder, info)
    print(json.dumps(dict(status='prepared', commit=commit, folder=str(folder), tests='11 passed')))


def activate(folder, rollback=False):
    folder = Path(folder).resolve()
    assert folder.parent == ROOT and re.fullmatch('[0-9a-f]{12}', folder.name)
    info = json.loads((folder / 'manifest.json').read_text())
    assert str(CURRENT.resolve()) == info['runtime'], 'runtime_changed'
    before, after = ('after', 'before') if rollback else ('before', 'after')
    for name, item in info['files'].items():
        assert digest(Path(name).read_bytes()) in (item[before], item[after]), 'concurrent_file_change:' + name
        replacement = Path(item['backup' if rollback else 'source'])
        assert digest(replacement.read_bytes()) == item[after], 'replacement_changed:' + name
    info['before_states'] = states((AUTO, *TRIGGERS, *OTHER))
    info['scheduler_snapshot_exit'] = scheduler('snapshot', folder)
    active = [unit for unit in TRIGGERS if info['before_states'][unit]['ActiveState'] == 'active']
    write_record(folder, info)
    try:
        for unit in active:
            run('systemctl', 'stop', unit)
        for name, item in info['files'].items():
            path = Path(name)
            assert digest(path.read_bytes()) in (item[before], item[after]), 'concurrent_file_change:' + name
            atomic(path, Path(item['backup' if rollback else 'source']).read_bytes())
        # Existing non-daemon HTTP handlers finish before systemd replaces Auto.
        # No post worker, queue, lease, token or business database is modified.
        run('systemctl', 'restart', AUTO)
    finally:
        for unit in active:
            run('systemctl', 'start', unit)
    with urllib.request.urlopen('http://127.0.0.1:18843/health', timeout=15) as response:
        assert response.status == 200
        info['health'] = json.load(response)
    info['scheduler_restore_exit'] = scheduler('restore', folder)
    info['after_states'] = states((AUTO, *TRIGGERS, *OTHER))
    for unit in OTHER:
        assert info['after_states'][unit]['MainPID'] == info['before_states'][unit]['MainPID'], 'unrelated_service_restarted:' + unit
    assert info['after_states'][AUTO]['ActiveState'] == 'active'
    for unit in active:
        assert info['after_states'][unit]['ActiveState'] == 'active', 'trigger_not_restored:' + unit
    for name, item in info['files'].items():
        assert digest(Path(name).read_bytes()) == item[after], 'installed_hash_mismatch:' + name
    info['status'] = 'rolled_back' if rollback else 'active'
    write_record(folder, info)
    print(json.dumps(dict(status=info['status'], commit=info['commit'], folder=str(folder),
                         restarted=AUTO, scheduler_restore_exit=info['scheduler_restore_exit'])))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('prepare', 'activate', 'rollback'))
    parser.add_argument('--folder')
    args = parser.parse_args()
    if args.action != 'prepare' and not args.folder:
        parser.error('--folder is required')
    prepare() if args.action == 'prepare' else activate(args.folder, args.action == 'rollback')
