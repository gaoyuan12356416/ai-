"""Stage and switch only the two auto-template owners for Mobo upcoming titles."""
import argparse
import ast
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
from scripts import deploy_fb_skipped_recheck as fb
from scripts import deploy_hit_drama as release
from scripts.deploy_fb_profile_priority import owner_active
from scripts.deploy_pipeline_runtime import inside, preserve_tree_owners, verify_source

BASE = 'b1361e96b3358ecd0792f42002ed4e04b8ba6a52'
DATA = Path('/mnt/data-disk')
LANES = {
    'x': ('x-tag-auto.service', 18843, ('features/x_tag_publish/', 'features/x_auto_posts/')),
    'fb': ('fb-auto-post-service.service', 18835, ('features/fb_auto_posts/',)),
}
X_TRIGGERS = ('x-tag-auto-runner.timer', 'x-tag-auto-runner.path', 'x-tag-auto-scheduler.timer')
FIXTURES = {'x': 'scripts/test_x_auto_post_selector.py', 'fb': 'scripts/test_fb_auto_validation.py'}
SHARED = 'features/moboreels_upcoming.py'


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def git(*args, check=True):
    return subprocess.run(['git', '-C', str(SOURCE), *map(str, args)], capture_output=True, check=check)


def manifest_write(folder, info):
    target = folder / 'manifest.json'
    temp = folder / 'manifest.next'
    temp.write_text(json.dumps(info, ensure_ascii=False, indent=2) + '\n')
    temp.chmod(0o600)
    temp.replace(target)


def runtime_image(root):
    """Content identity for the running code and static assets shared by this pointer."""
    digest = hashlib.sha256()
    for prefix in ('features', 'scripts', 'static', '.'):
        start = root / prefix
        if not start.is_dir():
            continue
        for path in sorted(start.iterdir() if prefix == '.' else start.rglob('*')):
            if prefix == '.' and path.suffix != '.py' and path.name != 'tag-runtime-manifest.json':
                continue
            if '__pycache__' in path.parts or path.suffix == '.pyc':
                continue
            if path.is_symlink():
                content = 'link:' + os.readlink(path)
            elif path.is_file():
                content = release.sha(path)
            else:
                continue
            digest.update(os.fsencode(str(path.relative_to(root))) + b'\0')
            digest.update(os.fsencode(content) + b'\n')
    return digest.hexdigest()


def verify_images(info):
    for side in ('previous', 'candidate'):
        if runtime_image(Path(info[side])) != info[side + '_image']:
            raise RuntimeError(side + '_release_changed_restage_from_current')


def overlay(candidate, previous, folder, commit, name):
    """Merge the published patch onto the exact running file; ambiguous changes stay offline."""
    live = inside(previous, name)
    out = inside(candidate, name)
    base = git('show', BASE + ':' + name, check=False)
    desired = git('show', commit + ':' + name).stdout
    old = live.read_bytes() if live.is_file() else None
    if old is not None:
        saved = folder / 'code-before' / name
        saved.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(live, saved)
    if base.returncode:
        if old is not None and old != desired:
            raise RuntimeError('new_file_collision:' + name)
        merged = desired
    elif old is None:
        raise RuntimeError('live_module_missing:' + name)
    elif old in (base.stdout, desired):
        merged = desired
    else:
        base_file = folder / 'merge.base'
        next_file = folder / 'merge.next'
        base_file.write_bytes(base.stdout)
        next_file.write_bytes(desired)
        result = subprocess.run(['git', 'merge-file', '-p', str(live), str(base_file), str(next_file)],
                                capture_output=True, timeout=30)
        if result.returncode:
            conflict = folder / ('conflict-' + name.replace('/', '_'))
            conflict.write_bytes(result.stdout)
            conflict.chmod(0o600)
            raise RuntimeError('live_source_hunk_conflict:' + name + ':' + str(conflict))
        merged = result.stdout
    compile(merged, name, 'exec')
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(merged)
    return {'before': release.sha(live), 'after': release.sha(out)}


def test_dependencies(path):
    """Copy only explicitly imported test helpers absent from the composite production tree."""
    result = set()
    for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
        if isinstance(node, ast.ImportFrom):
            base = node.module or ''
            names = [base] + [base + '.' + alias.name for alias in node.names]
        elif isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        else:
            continue
        for name in names:
            if name.startswith(('scripts.test_', 'tests.test_')):
                relative = '/'.join(name.split('.')[:2]) + '.py'
                if (SOURCE / relative).is_file():
                    result.add(relative)
    return result


def run_tests(candidate, folder, lane, changes):
    suite = 'tests/test_' + lane + '_mobo_upcoming.py'
    common = [p for p in changes if p.startswith(('tests/test_moboreels_upcoming', 'tests/test_mobo_upcoming'))]
    cases = [suite, *common]
    files = set()
    todo = [*cases, FIXTURES[lane]]
    if lane == 'x':
        todo.append('scripts/test_moboreels_hit.py')
    while todo:
        name = todo.pop()
        if name in files:
            continue
        src = SOURCE / name
        if not src.is_file():
            raise RuntimeError('missing_test:' + name)
        files.add(name)
        todo.extend(test_dependencies(src) - files)
        target = inside(candidate, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        # The same fixtures as the authored regression tests, in the actual service module tree.
        shutil.copy2(src, target)
    # `tests` is not a package in certain historical composites.
    log = folder / 'tests.txt'
    commands = [(name, ['discover', '-s', 'tests', '-p', Path(name).name, '-v']) for name in cases]
    if lane == 'x':
        commands.append(('explicit-mobo-hit', ['-v', 'scripts.test_moboreels_hit.XTests']))
    for name, args in commands:
        cmd = [sys.executable, '-B', '-m', 'unittest', *args]
        result = subprocess.run(cmd, cwd=candidate, capture_output=True, text=True, timeout=180)
        with log.open('a') as output:
            output.write(result.stdout + result.stderr)
        if result.returncode or 'Ran 0 tests' in result.stderr:
            raise RuntimeError('candidate_tests_failed:' + name + ':' + str(log))


def native_state(lane, db):
    with closing(sqlite3.connect(db.as_uri() + '?mode=ro', uri=True, timeout=10)) as conn:
        conn.row_factory = sqlite3.Row
        if lane == 'x':
            templates = 'select t.id,t.enabled,t.current_version,v.config_sha256 from x_auto_template t join x_auto_template_version v on v.template_id=t.id and v.version=t.current_version'
            ongoing = """select id,status,material_id,execution_queue_id,publish_id from x_auto_task
                         where status in ('selecting','preparing','publishing','reconciling','ready')"""
        else:
            templates = 'select t.id,t.status,t.current_version,v.config_sha256 from fb_auto_template t join fb_auto_template_version v on v.template_id=t.id and v.version=t.current_version'
            ongoing = """select id,status,material_id,gpu_job_id,graph_post_id from fb_auto_task
                         where status in ('preparing','running','submitted','ready')"""
        tables = [r[0] for r in conn.execute(
            "select name from sqlite_master where type='table' and name like ? order by name",
            (lane + '_auto_mobo_%',))]
        return {'observed_at': now(), 'upcoming_tables': tables,
                'templates': [dict(r) for r in conn.execute(templates)],
                'inflight': [dict(r) for r in conn.execute(ongoing)]}


def prepare(lane, folder):
    commit = verify_source(SOURCE, 'HEAD')
    root, _, db = release.settings(lane)
    previous = root.resolve(strict=True)
    unit = LANES[lane][0]
    pid = fb.show(unit, 'MainPID')
    if pid == '0' or Path('/proc', pid, 'cwd').resolve() != previous:
        raise RuntimeError('live_service_root_does_not_match_release_pointer')
    if lane == 'x':
        env = Path('/proc', pid, 'environ').read_bytes().split(b'\0')
        if ('X_TAG_SELECTION_BASE=' + str(root)).encode() not in env:
            raise RuntimeError('inspect_pinned_x_selector_base')
    changes = git('diff', '--name-only', BASE, commit).stdout.decode().splitlines()
    names = [n for n in changes if n == SHARED or n.startswith(LANES[lane][2])]
    if SHARED not in names or len(names) < 2:
        raise RuntimeError('missing_upcoming_selector_changes')
    folder.mkdir(parents=True, exist_ok=False)
    folder.chmod(0o755)
    previous_image = runtime_image(previous)
    candidate = folder / 'candidate'
    shutil.copytree(previous, candidate, symlinks=True,
                    ignore=shutil.ignore_patterns('.git', '__pycache__', '*.pyc'))
    preserve_tree_owners(previous, candidate)
    info = dict(platform=lane, commit=commit, source=str(SOURCE), previous=str(previous),
                candidate=str(candidate), previous_image=previous_image, files={}, static={}, status='prepared')
    for name in names:
        info['files'][name] = overlay(candidate, previous, folder, commit, name)
    run_tests(candidate, folder, lane, changes)
    info['candidate_image'] = runtime_image(candidate)
    verify_images(info)
    before = native_state(lane, db)
    fb.save(folder / 'native-before.json', before)
    with closing(sqlite3.connect(db.as_uri() + '?mode=ro', uri=True, timeout=20)) as src:
        backup = folder / 'ledger-before.sqlite3'
        backup.touch(mode=0o600, exist_ok=False)
        with closing(sqlite3.connect(backup)) as dst:
            src.backup(dst, pages=512, sleep=.05)
            if dst.execute('pragma quick_check').fetchone()[0] != 'ok':
                raise RuntimeError('native_ledger_backup_invalid')
    manifest_write(folder, info)
    return info


def pending_for_rollback(lane, db):
    with closing(sqlite3.connect(db.as_uri() + '?mode=ro', uri=True, timeout=10)) as conn:
        if lane == 'fb':
            table = conn.execute("select 1 from sqlite_master where type='table' and name='fb_auto_mobo_wait'").fetchone()
            return conn.execute('select count(*) from fb_auto_mobo_wait').fetchone()[0] if table else 0
        return conn.execute("""select count(*) from x_auto_run r join x_auto_task t on t.run_id=r.id
            where instr(r.blacklist_snapshot_json,'"mobo_upcoming"')>0
            and t.status in ('pending','selecting','retry_wait')
            and coalesce(t.material_id,'')='' and coalesce(t.execution_queue_id,0)=0""").fetchone()[0]


def scheduler(lane, action, folder, label):
    baseline = folder / ('schedulers-' + label + '-before.json')
    result = folder / ('schedulers-' + label + '-after.json')
    if action == 'snapshot':
        release.run(sys.executable, release.HEALTH, 'snapshot', '--lane', lane, '--output', baseline)
    else:
        release.run(sys.executable, release.HEALTH, 'restore', '--baseline', baseline,
                    '--wait-seconds', '25', '--output', result)


def switch_x(folder, info, rollback):
    unit, port, _ = LANES['x']
    label = 'rollback' if rollback else 'apply'
    scheduler('x', 'snapshot', folder, label)
    triggers = {unit_name: fb.show(unit_name) for unit_name in X_TRIGGERS}
    fb.save(folder / ('tag-triggers-' + label + '.json'), triggers)
    if fb.show(unit, 'TimeoutStopUSec') != 'infinity' or fb.show(unit, 'SendSIGKILL') != 'no':
        raise RuntimeError('x_selector_graceful_shutdown_policy_changed')
    root, _, db = release.settings('x')
    destination = Path(info['previous'] if rollback else info['candidate'])
    original = root.resolve()
    old_pid = fb.show(unit, 'MainPID')
    stopped = False
    changed = False
    try:
        for trigger, state in triggers.items():
            if state == 'active':
                release.run('systemctl', 'stop', trigger)
        # Finish requests already accepted by the two native runner clients.
        # The service itself uses a join-on-close ThreadingHTTPServer and SIGTERM
        # handler. The unit's unlimited graceful stop protects manual requests too.
        for worker in ('x-tag-auto-runner.service', 'x-tag-auto-scheduler.service'):
            while fb.show(worker, 'MainPID') != '0':
                time.sleep(1)
        release.verify_current(info, rollback)
        stopped = True
        release.run('systemctl', 'stop', '--no-block', unit)
        while fb.show(unit, 'MainPID') != '0' or fb.show(unit) == 'deactivating':
            time.sleep(.25)
        release.verify_current(info, rollback)
        verify_images(info)
        if rollback and pending_for_rollback('x', db):
            raise RuntimeError('unfinished_mobo_selections_require_current_reader')
        tmp = root.with_name('current.mobo-' + str(os.getpid()))
        tmp.symlink_to(destination, target_is_directory=True)
        tmp.replace(root)
        changed = True
        release.run('systemctl', 'start', unit)
        release.health(port)
        if Path('/proc', fb.show(unit, 'MainPID'), 'cwd').resolve() != destination:
            raise RuntimeError('x_selector_did_not_load_requested_release')
        fb.save(folder / ('owner-' + label + '.json'),
                {'old_pid': old_pid, 'new_pid': fb.show(unit, 'MainPID'), 'runtime': str(destination)})
    except Exception:
        if changed and root.resolve() == destination and fb.show(unit, 'MainPID') == '0':
            tmp = root.with_name('current.mobo-recover-' + str(os.getpid()))
            tmp.symlink_to(original, target_is_directory=True)
            tmp.replace(root)
        if stopped and fb.show(unit, 'MainPID') == '0':
            release.run('systemctl', 'start', unit)
        raise
    finally:
        for trigger, state in triggers.items():
            if state == 'active':
                release.run('systemctl', 'start', trigger)
        scheduler('x', 'restore', folder, label)
    info['status'] = 'rolled_back' if rollback else 'active'
    manifest_write(folder, info)


def switch(lane, folder, rollback=False):
    info = json.loads((folder / 'manifest.json').read_text())
    if info['platform'] != lane:
        raise RuntimeError('deployment_lane_mismatch')
    root, _, db = release.settings(lane)
    destination = Path(info['previous'] if rollback else info['candidate'])
    desired = 'rolled_back' if rollback else 'active'
    if info['status'] == desired and root.resolve() == destination:
        release.health(LANES[lane][1])
        return info
    release.verify_current(info, rollback)
    verify_images(info)
    for name, values in info['files'].items():
        if release.sha(destination / name) != values['before' if rollback else 'after']:
            raise RuntimeError('destination_file_drift:' + name)
    if rollback and pending_for_rollback(lane, db):
        raise RuntimeError('unfinished_mobo_selections_require_current_reader')
    if lane == 'x':
        switch_x(folder, info, rollback)
    else:
        original = fb.native_active

        def guard():
            active = owner_active()
            if rollback and pending_for_rollback(lane, db):
                raise RuntimeError('unfinished_mobo_selections_require_current_reader')
            if not active:
                verify_images(info)
            return active

        fb.native_active = guard
        try:
            release.activate('fb', folder, rollback, defer_plan_read=False)
        finally:
            fb.native_active = original
    release.health(LANES[lane][1])
    current_pid = fb.show(LANES[lane][0], 'MainPID')
    if root.resolve() != destination or Path('/proc', current_pid, 'cwd').resolve() != destination:
        raise RuntimeError('owner_root_after_switch_mismatch')
    state = native_state(lane, db)
    fb.save(folder / ('native-' + desired + '.json'), state)
    expected = {'x_auto_mobo_upcoming'} if lane == 'x' else {'fb_auto_mobo_plan', 'fb_auto_mobo_wait'}
    if not rollback and not expected.issubset(state['upcoming_tables']):
        raise RuntimeError('owner_did_not_initialize_upcoming_tables')
    return json.loads((folder / 'manifest.json').read_text())


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'apply', 'rollback'))
    parser.add_argument('--lane', choices=('x', 'fb'), required=True)
    parser.add_argument('--folder', type=Path, required=True)
    opts = parser.parse_args()
    folder = opts.folder.resolve()
    if DATA not in folder.parents or release.run('findmnt', '-rn', '-o', 'UUID', '--mountpoint', DATA) != '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':
        raise RuntimeError('expected_data_disk_required')
    result = prepare(opts.lane, folder) if opts.action == 'prepare' else switch(opts.lane, folder, opts.action == 'rollback')
    print(json.dumps({'platform': opts.lane, 'commit': result['commit'], 'status': result['status'],
                      'folder': str(folder)}), flush=True)
