"""Apply the channel-selection delta to each current X runtime and public page.

Run from a clean checkout of the pushed commit. Code rollback preserves every
current database, credential, queue, receipt and intentional scheduler pause.
"""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request

SOURCE = Path(__file__).resolve().parents[1]
BASE = 'bce2221c'
ROOT = Path('/mnt/data-disk/x-channel-selection-20261116')
POINTERS = {'ordinary': Path('/opt/x-post-automation/current'),
            'tag': Path('/opt/x-tag-publisher/current')}
BACKEND = ('features/x_auto_posts/service.py', 'features/x_posts/service.py',
           'features/x_accounts/oauth_service.py', 'features/x_auto_posts/x_sidecar.py')
ASSETS = ('static/x-auto-publish-template.js', 'static/x-auto-publish-template.html',
          'static/x-post-material-pool.html', 'static/x-post-drama-pool.html')
OWNERS = ('x-tag-auto.service', 'x-tag-post.service',
          'x-auto-post-service.service', 'x-post-automation.service')
TAG_TRIGGERS = ('x-tag-auto-scheduler.timer', 'x-tag-auto-runner.timer',
                'x-tag-auto-runner.path', 'x-tag-pool.timer', 'x-tag-manual.timer')
TRIGGERS = TAG_TRIGGERS + ('x-post-schedule.timer', 'x-post-schedule-claim.timer',
                           'x-post-manual.timer', 'x-auto-post-scheduler.timer')
HEALTH = '/opt/publish-scheduler-health/current/scripts/publish_scheduler_health.py'
DATABASES = ('/var/lib/x-post-automation/accounts.sqlite3',
             '/mnt/data-disk/x-auto-post-publisher/x-auto-post.sqlite3',
             '/mnt/data-disk/x-tag-publisher/posts.sqlite3',
             '/mnt/data-disk/x-tag-publisher/auto/x-auto-post.sqlite3')


def run(*args, **kwargs):
    return subprocess.run(list(map(str, args)), check=True, capture_output=True,
                          **kwargs).stdout


def sha(data):
    return hashlib.sha256(data).hexdigest()


def save(folder, record):
    temporary = folder / 'record.next'
    temporary.write_text(json.dumps(record, indent=2) + '\n', encoding='utf-8')
    temporary.chmod(0o600)
    temporary.replace(folder / 'record.json')


def fingerprint(root):
    values = []
    for path in sorted(root.rglob('*')):
        if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc':
            values.append((str(path.relative_to(root)), sha(path.read_bytes())))
    return sha(json.dumps(values).encode())


def delta(relative, generated=False, public=False):
    value = run('git', '-C', SOURCE, 'diff', '-U2', BASE, 'HEAD', '--', relative).decode('utf-8')
    if generated:
        value = value.replace('static/x-auto-publish', 'static/x-tag-auto-publish')
        value = value.replace('static/x-post-material-pool', 'static/x-tag-material-pool')
    if public:
        value = value.replace('a/static/', 'a/').replace('b/static/', 'b/')
    return value.encode('utf-8')


def apply_delta(root, relative, generated=False, public=False):
    patch = delta(relative, generated, public)
    if not patch:
        return
    run('git', 'apply', '--unsafe-paths', '--directory=' + str(root),
        input=patch)


def version_editor(root, prefix='x-auto-publish'):
    editor = root / (prefix + '-template.js')
    page = root / (prefix + '-template.html')
    if not editor.exists() or not page.exists():
        return
    content, count = re.subn(r'(/' + prefix + r'-template\.js)(?:\?[^"\']*)?',
                             r'\1?v=' + sha(editor.read_bytes())[:12],
                             page.read_text(encoding='utf-8'))
    assert count == 1, 'editor_reference_changed:' + str(page)
    page.write_text(content, encoding='utf-8')


def atomic_file(path, data):
    stat = path.stat()
    temporary = path.with_name(path.name + '.channel-selection-next')
    temporary.write_bytes(data)
    os.chmod(temporary, stat.st_mode & 0o777)
    os.chown(temporary, stat.st_uid, stat.st_gid)
    temporary.replace(path)


def switch(pointer, target):
    temporary = pointer.with_name(pointer.name + '.channel-selection-next')
    temporary.symlink_to(target)
    temporary.replace(pointer)


def states(units):
    return {u: run('systemctl', 'show', u, '-p', 'ActiveState', '--value').decode().strip()
            for u in units}


def scheduler(folder, action, prefix=''):
    args = [sys.executable, HEALTH, action, '--lane', 'x', '--output',
            str(folder / (prefix + 'schedulers-' + action + '.json'))]
    if action == 'restore':
        args += ['--baseline', str(folder / (prefix + 'schedulers-snapshot.json')), '--wait-seconds', '25']
    result = subprocess.run(args, capture_output=True)
    (folder / (prefix + 'schedulers-' + action + '.txt')).write_bytes(result.stdout + result.stderr)
    return result.returncode


def prepare():
    assert run('findmnt', '-n', '-o', 'UUID', '--mountpoint', '/mnt/data-disk').decode().strip() == '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    assert not run('git', '-C', SOURCE, 'status', '--porcelain'), 'dirty_source'
    commit = run('git', '-C', SOURCE, 'rev-parse', 'HEAD').decode().strip()
    assert run('git', '-C', SOURCE, 'branch', '-r', '--contains', commit), 'source_not_pushed'
    folder = ROOT / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + commit[:8])
    folder.mkdir(parents=True, mode=0o755)
    (folder / 'backups').mkdir(mode=0o700)
    record = dict(commit=commit, base_commit=BASE, source=str(SOURCE), status='preparing', roots={}, files={})
    save(folder, record)
    for lane, pointer in POINTERS.items():
        live = pointer.resolve(strict=True)
        staged = folder / 'candidates' / lane
        staged.parent.mkdir(exist_ok=True)
        run('cp', '-a', '--reflink=auto', live, staged)
        for relative in BACKEND + ASSETS:
            apply_delta(staged, relative)
        for relative in ASSETS[:3]:
            name = relative.replace('x-auto-publish', 'x-tag-auto-publish').replace('x-post-material-pool', 'x-tag-material-pool')
            if (staged / name).is_file():
                apply_delta(staged, relative, generated=True)
        version_editor(staged / 'static')
        version_editor(staged / 'static', 'x-tag-auto-publish')
        if (staged / 'tag-runtime-manifest.json').is_file():
            manifest = json.loads((staged / 'tag-runtime-manifest.json').read_text(encoding='utf-8'))
            for path in list(manifest.get('files', {})):
                if (staged / path).is_file():
                    manifest['files'][path] = sha((staged / path).read_bytes())
            manifest['channel_selection_overlay_commit'] = commit
            (staged / 'tag-runtime-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
        record['roots'][lane] = dict(pointer=str(pointer), before=str(live), after=str(staged),
                                     before_fingerprint=fingerprint(live))
        # Validate in a separate copy: acceptance fixtures are never installed.
        tests = folder / ('tests-' + lane)
        run('cp', '-a', '--reflink=auto', staged, tests)
        for name in ('test_x_channel_selection.py', 'test_x_auto_post_service.py',
                     'test_x_auto_post_validation.py', 'test_x_auto_post_publisher.py',
                     'test_x_post_schedule_runner.py'):
            shutil.copy2(SOURCE / 'scripts' / name, tests / 'scripts' / name)
        result = subprocess.run([sys.executable, '-m', 'unittest', 'scripts.test_x_channel_selection',
                                 'scripts.test_x_auto_post_service', '-q'], cwd=tests,
                                env=dict(os.environ, PYTHONPATH=str(tests)), capture_output=True)
        (folder / ('tests-' + lane + '.txt')).write_bytes(result.stdout + result.stderr)
        if result.returncode:
            raise RuntimeError('candidate_tests_failed:' + lane)
        run(sys.executable, '-m', 'py_compile', *(staged / p for p in BACKEND))
        matched = re.search(rb'Ran (\d+) tests?', result.stderr)
        print(json.dumps(dict(prepared_lane=lane, tests=(matched.group(1).decode() if matched else 'focused') + ' passed')), flush=True)
    for number, live in enumerate((Path('/usr/share/nginx/html'), Path('/root/drama_material_service/static'))):
        if not any(delta(relative) for relative in ASSETS):
            continue
        staged = folder / ('public-' + str(number))
        staged.mkdir()
        names = [Path(p).name for p in ASSETS]
        names += [n.replace('x-auto-publish', 'x-tag-auto-publish').replace('x-post-material-pool', 'x-tag-material-pool') for n in names[:3]]
        for name in names:
            shutil.copy2(live / name, staged / name)
        for relative in ASSETS:
            apply_delta(staged, relative, public=True)
        for relative in ASSETS[:3]:
            apply_delta(staged, relative, generated=True, public=True)
        version_editor(staged)
        version_editor(staged, 'x-tag-auto-publish')
        for name in names:
            target = live / name
            backup = folder / 'backups' / ('static-' + str(number) + '-' + name)
            shutil.copy2(target, backup)
            record['files'][str(target)] = dict(backup=str(backup), source=str(staged / name),
                                                before=sha(backup.read_bytes()), after=sha((staged / name).read_bytes()))
    for number, database in enumerate(DATABASES):
        with closing(sqlite3.connect(Path(database).as_uri() + '?mode=ro', uri=True)) as original, \
             closing(sqlite3.connect(folder / 'backups' / ('database-' + str(number) + '.sqlite3'))) as backup:
            original.backup(backup)
            assert backup.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
        (folder / 'backups' / ('database-' + str(number) + '.sqlite3')).chmod(0o600)
    record['status'] = 'prepared'
    save(folder, record)
    print(json.dumps(dict(prepared=str(folder), commit=commit)), flush=True)


def ready(baseline):
    for unit, port in (('x-post-automation.service', 8810),
                       ('x-auto-post-service.service', 18833),
                       ('x-tag-post.service', 8821), ('x-tag-auto.service', 18843)):
        if baseline.get(unit) != 'active':
            continue
        deadline = time.monotonic() + 45
        while True:
            try:
                with urllib.request.urlopen('http://127.0.0.1:%d/health' % port, timeout=3) as response:
                    assert response.status == 200
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.5)


def activate(folder, rollback=False):
    record = json.loads((folder / 'record.json').read_text(encoding='utf-8'))
    if not rollback:
        assert record['status'] == 'prepared'
        for item in record['roots'].values():
            assert str(Path(item['pointer']).resolve()) == item['before'], 'runtime_switched_concurrently'
            assert fingerprint(Path(item['before'])) == item['before_fingerprint'], 'runtime_changed_concurrently'
        for name, item in record['files'].items():
            assert sha(Path(name).read_bytes()) == item['before'], 'static_changed_concurrently:' + name
        assert scheduler(folder, 'snapshot') == 0, 'scheduler_baseline_unhealthy'
        record['unit_baseline'] = states(TRIGGERS + OWNERS + ('x-tag-gateway.service',))
        save(folder, record)
    else:
        for item in record['roots'].values():
            assert str(Path(item['pointer']).resolve()) == item['after'], 'later_runtime_release_present'
        for name, item in record['files'].items():
            assert sha(Path(name).read_bytes()) == item['after'], 'later_static_change_present:' + name
        assert scheduler(folder, 'snapshot', 'rollback-') == 0, 'rollback_scheduler_baseline_unhealthy'
        record['rollback_unit_baseline'] = states(TRIGGERS + OWNERS + ('x-tag-gateway.service',))
        save(folder, record)
    baseline = record['rollback_unit_baseline' if rollback else 'unit_baseline']
    active_triggers = [u for u in TRIGGERS if baseline.get(u) == 'active']
    # Stopping a trigger leaves its current oneshot and accepted HTTP requests
    # running. Owner stop uses the deployed graceful drain and infinite timeout.
    if active_triggers:
        run('systemctl', 'stop', *active_triggers)
    print(json.dumps(dict(status='draining_affected_owners')), flush=True)
    switched = False

    def install(reverse):
        for item in record['roots'].values():
            switch(Path(item['pointer']), Path(item['before' if reverse else 'after']))
        for name, item in record['files'].items():
            atomic_file(Path(name), Path(item['backup' if reverse else 'source']).read_bytes())

    def start_owners():
        for unit in reversed(OWNERS):
            if baseline.get(unit) == 'active':
                run('systemctl', 'start', unit)

    try:
        for unit in OWNERS:
            if baseline.get(unit) == 'active':
                run('systemctl', 'stop', unit)
        install(rollback)
        start_owners()
        ready(baseline)
        switched = True
    except BaseException:
        # Restore only code if the scoped switch or new HTTP readiness fails.
        for unit in OWNERS:
            if baseline.get(unit) == 'active':
                run('systemctl', 'stop', unit)
        install(not rollback)
        start_owners()
        ready(baseline)
        raise
    finally:
        for unit in active_triggers:
            run('systemctl', 'start', unit)
        scheduler_result = scheduler(folder, 'restore', 'rollback-' if rollback else '')
        record['scheduler_restore_result'] = scheduler_result
        record['unit_after'] = states(TRIGGERS + OWNERS + ('x-tag-gateway.service',))
        record['status'] = ('rolled_back' if rollback else 'active') if switched else 'switch_failed'
        save(folder, record)
    assert scheduler_result == 0, 'scheduler_restore_unhealthy'
    for name, item in record['files'].items():
        assert sha(Path(name).read_bytes()) == item['before' if rollback else 'after']
    print(json.dumps(dict(status=record['status'], directory=str(folder), commit=record['commit'])), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('prepare', 'apply', 'rollback'))
    parser.add_argument('directory', nargs='?', type=Path)
    parser.add_argument('--base', default=BASE)
    args = parser.parse_args()
    BASE = args.base
    if args.action == 'prepare':
        prepare()
    else:
        assert args.directory is not None
        activate(args.directory, args.action == 'rollback')
