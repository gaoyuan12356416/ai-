"""Scoped template copy/delete overlay with offline tests and accepted-request drain."""
import argparse
from contextlib import closing
import difflib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import time

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
from scripts import deploy_hit_drama_launch_window as ops
from scripts.deploy_drama_launch_range import remember
from scripts.deploy_hit_drama_multi_id import atomic

BASE = 'd358271d'
TEST = 'scripts/test_template_actions.py'
FILES = {
    'x': ['features/x_auto_posts/core.py', 'features/x_auto_posts/service.py', 'features/x_tag_publish/gateway.py'],
    'fb': ['features/fb_auto_posts/core.py', 'features/fb_auto_posts/service.py', 'features/fb_auto_posts/client.py'],
}
STATIC = ['static/' + lane + '-auto-publish-templates.' + ext for lane in ('x-tag', 'fb') for ext in ('html', 'js')]
TAG_TRIGGERS = ['x-tag-auto-scheduler.timer', 'x-tag-auto-runner.timer', 'x-tag-auto-runner.path']
EXTRA_UNITS = TAG_TRIGGERS + ['drama-material-job-worker.service', 'drama-youtube-publish-worker.service']


def show(unit, field='ActiveState'):
    return ops.run('systemctl', 'show', unit, '-p', field, '--value')


def is_static(name):
    return '/static/' in name or name.startswith('/usr/share/nginx/html/')


def overlay(folder, name, data):
    before = subprocess.check_output(['git', '-C', str(SOURCE), 'show', BASE + ':' + name])
    after = (SOURCE / name).read_bytes().replace(b'\r\n', b'\n')
    if data.replace(b'\r\n', b'\n') == before:
        return after
    if name in STATIC and name.endswith('.html'):
        asset = Path(name).stem + '.js'
        pattern = rb'(?<=src="/)' + re.escape(asset.encode()) + rb'\?v=[^"\s]+'
        replacement = re.findall(pattern, after)
        assert len(replacement) == 1 and len(re.findall(pattern, data)) == 1, 'ambiguous_list_script:' + name
        # Other deployments update this cache key independently. Only replace
        # the single known list script URL, preserving the rest of the page.
        return re.sub(pattern, lambda _: replacement[0], data)
    scratch = folder / 'overlay-scratch'
    scratch.write_bytes(data)
    delta = ''.join(difflib.unified_diff(before.decode().splitlines(True), after.decode().splitlines(True),
                    fromfile='a/' + scratch.name, tofile='b/' + scratch.name, n=1))
    result = subprocess.run(['git', 'apply', '--whitespace=nowarn', '-'], cwd=folder,
                            input=delta, text=True, capture_output=True)
    assert not result.returncode, 'live_variant_requires_review:' + name + ':' + result.stderr
    return scratch.read_bytes()


def prepare(folder):
    assert ops.run('findmnt', '-rn', '-o', 'UUID', '--mountpoint', '/mnt/data-disk') == '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    assert folder.is_relative_to(Path('/mnt/data-disk')) and shutil.disk_usage('/mnt/data-disk').free > 2 * 1024**3
    assert not ops.run('git', '-C', SOURCE, 'status', '--porcelain')
    commit = ops.run('git', '-C', SOURCE, 'rev-parse', 'HEAD')
    assert ops.run('git', '-C', SOURCE, 'branch', '-r', '--contains', commit)
    folder.mkdir(mode=0o700, parents=True, exist_ok=False)
    info = dict(commit=commit, source=str(SOURCE), files={}, lanes={}, config_before={}, services_before=ops.services())
    ops.save(folder / 'manifest.json', info)
    for lane, names in FILES.items():
        root, _, _, unit, db = ops.LANES[lane]
        candidate = folder / ('candidate-' + lane)
        shutil.copytree(root.resolve(), candidate, symlinks=True,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.git'))
        for name in names:
            target = candidate / name
            assert target.resolve().is_relative_to(candidate)
            data = overlay(folder, name, (root / name).read_bytes())
            target.write_bytes(data)
            compile(data, name, 'exec')
            remember(folder, info, root / name, data)
        shutil.copy2(SOURCE / TEST, candidate / TEST)
        info['lanes'][lane] = dict(root=str(root.resolve()), candidate=str(candidate), unit=unit)
        info['config_before'][lane] = ops.config_hash(db, lane)
        with closing(sqlite3.connect(db.as_uri() + '?mode=ro', uri=True)) as src, \
             closing(sqlite3.connect(folder / ('ledger-before-' + lane + '.sqlite3'))) as dst:
            src.backup(dst)
        (folder / ('ledger-before-' + lane + '.sqlite3')).chmod(0o600)
        result = subprocess.run([sys.executable, '-B', TEST, '--platform', lane], cwd=candidate,
                                capture_output=True, text=True, timeout=90)
        (folder / ('tests-' + lane + '.txt')).write_text(result.stdout + result.stderr)
        if result.returncode:
            print(result.stdout + result.stderr)
            raise RuntimeError('candidate_tests_failed:' + lane)
        print(lane + ' offline tests passed', flush=True)
    # Rehearse the additive X migration twice on a snapshot, preserving all rows.
    rehearsal = folder / 'x-migration.sqlite3'
    shutil.copy2(folder / 'ledger-before-x.sqlite3', rehearsal)
    code = """import sqlite3,sys
from features.x_auto_posts.core import XAutoPostStore
p=sys.argv[1]
def counts():
 with sqlite3.connect(p) as c:
  return {t:c.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ('x_auto_template','x_auto_template_version','x_auto_run','x_auto_task')}
before=counts(); XAutoPostStore(p); XAutoPostStore(p); assert counts()==before
with sqlite3.connect(p) as c:
 assert c.execute('pragma quick_check').fetchone()[0]=='ok'
 assert c.execute(\"select count(*) from x_auto_template where deleted_at_utc<>''\").fetchone()[0]==0
print('migration twice passed; template, version, run and task counts preserved')
"""
    result = subprocess.run([sys.executable, '-B', '-c', code, str(rehearsal)],
                            cwd=folder / 'candidate-x', capture_output=True, text=True, timeout=90)
    (folder / 'migration-test.txt').write_text(result.stdout + result.stderr)
    assert not result.returncode, 'migration_rehearsal_failed:' + result.stderr
    main = Path('/root/drama_material_service')
    for name in ('app.py', 'features/fb_auto_posts/client.py'):
        data = overlay(folder, name, (main / name).read_bytes())
        compile(data, name, 'exec')
        remember(folder, info, main / name, data)
    for root in [main, Path('/usr/share/nginx/html'), ops.LANES['fb'][0], ops.LANES['x'][0]]:
        for name in STATIC:
            path = root / (Path(name).name if root.name == 'html' else name)
            if path.exists():
                remember(folder, info, path, overlay(folder, name, path.read_bytes()))
    ui = folder / 'ui-test'
    (ui / 'scripts').mkdir(parents=True)
    (ui / 'static').mkdir()
    shutil.copy2(SOURCE / 'scripts/test_template_actions_ui.js', ui / 'scripts/test_template_actions_ui.js')
    for name in STATIC:
        if name.endswith('.js'):
            record = info['files']['/usr/share/nginx/html/' + Path(name).name]
            shutil.copy2(record['next'], ui / name)
    result = subprocess.run(['node', 'scripts/test_template_actions_ui.js'], cwd=ui, capture_output=True, text=True, timeout=20)
    (folder / 'tests-ui.txt').write_text(result.stdout + result.stderr)
    assert not result.returncode, 'live_ui_tests_failed:' + result.stderr
    info['status'] = 'prepared'
    ops.save(folder / 'manifest.json', info)
    print(json.dumps(dict(status='prepared', commit=commit, files=len(info['files']), tests='fb+x passed')))


def restart_gateway(folder, action):
    from scripts.deploy_fb_skipped_recheck import drained
    unit = 'x-tag-gateway.service'
    pid = show(unit, 'MainPID')
    marker = 'template-gateway-' + str(os.getpid())
    rule = ['-p', 'tcp', '--dport', '8898', '-m', 'conntrack', '--ctstate', 'NEW',
            '-m', 'comment', '--comment', marker, '-j', 'REJECT', '--reject-with', 'tcp-reset']
    ops.run('systemd-run', '--quiet', '--unit=' + marker, '--on-active=120s', '--',
            '/usr/sbin/iptables', '-D', 'INPUT', *rule)
    gated = False
    try:
        ops.run('iptables', '-I', 'INPUT', '1', *rule)
        gated = True
        deadline = time.monotonic() + 90
        while True:
            proof = drained(pid)
            if not proof['sockets'] and not proof['accept_queue'] and proof['threads'] == 1:
                time.sleep(.3)
                if drained(pid) == proof: break
            if time.monotonic() >= deadline:
                raise RuntimeError('gateway accepted requests preserved; switch pending')
            time.sleep(.5)
        ops.save(folder / (action + '-gateway-drain.json'), dict(pid=pid, proof=proof))
        ops.run('systemctl', 'restart', '--no-block', unit)
        deadline = time.monotonic() + 25
        while show(unit, 'MainPID') in ('0', pid) or show(unit) != 'active':
            if time.monotonic() >= deadline: raise RuntimeError('gateway restart pending')
            time.sleep(.5)
    finally:
        if gated: ops.run('iptables', '-D', 'INPUT', *rule)
        subprocess.run(['systemctl', 'stop', marker + '.timer'], capture_output=True, timeout=10)


def switch(folder, rollback=False):
    info = json.loads((folder / 'manifest.json').read_text())
    assert info['status'] in ('prepared', 'active', 'switch_requested', 'rolled_back')
    for lane, row in info['lanes'].items():
        assert str(ops.LANES[lane][0].resolve()) == row['root'], 'preserve_later_runtime:' + lane
    for name, row in info['files'].items():
        assert ops.digest(name) in (row['before'], row['after']), 'preserve_later_file:' + name
    main_state = show('drama-material-api.service')
    assert main_state == 'active' or (main_state == 'deactivating' and show('drama-material-api.service', 'SubState') == 'stop'), 'main_api_not_serving_or_draining'
    assert 'pipeline_service_drain.py stop' in ops.run('systemctl', 'show', 'drama-material-api.service', '-p', 'ExecStop', '--value')
    action = 'rollback' if rollback else 'apply'
    if rollback:
        for lane, (_, _, _, _, db) in ops.LANES.items():
            with closing(sqlite3.connect(db.as_uri() + '?mode=ro', uri=True)) as conn:
                table, where = ('x_auto_template', "deleted_at_utc<>''") if lane == 'x' else ('fb_auto_template', "status='deleted'")
                assert not conn.execute('SELECT count(*) FROM ' + table + ' WHERE ' + where).fetchone()[0], 'deleted_templates_require_forward_compatible_rollback'
    info['services_at_switch'] = ops.services()
    info['main_pid_before'] = show('drama-material-api.service', 'MainPID')
    info['extra_before'] = {u: show(u) for u in EXTRA_UNITS}
    for lane in FILES:
        ops.run(sys.executable, ops.HEALTH, 'snapshot', '--lane', lane,
                '--output', folder / (action + '-schedulers-before-' + lane + '.json'))
    for name, row in info['files'].items():
        if is_static(name): continue  # Publish buttons only after all APIs are active.
        source = Path(row['backup' if rollback else 'next'])
        assert ops.digest(source) == row['before' if rollback else 'after']
        atomic(Path(name), source.read_bytes(), row)
    info.update(status='switch_requested', requested_action=action)
    ops.save(folder / 'manifest.json', info)
    ops.restart_fb(folder, action)
    ops.run('systemctl', 'restart', '--no-block', ops.LANES['x'][3])
    restart_gateway(folder, action)
    # The installed main API stop/start hooks drain accepted handlers and restore
    # their own admission state. Never shorten their timeout or force a signal.
    sharing_drain = (show('drama-material-api.service') == 'deactivating' and
                     show('drama-material-api.service', 'MainPID') == info['main_pid_before'])
    if not sharing_drain:
        ops.run('systemctl', 'restart', '--no-block', 'drama-material-api.service')
    info['shared_existing_main_drain'] = sharing_drain
    ops.save(folder / 'manifest.json', info)
    print(json.dumps(dict(status='switch_requested', action=action, commit=info['commit'])))


def verify(folder):
    info = json.loads((folder / 'manifest.json').read_text())
    rollback = info['requested_action'] == 'rollback'
    action = 'rollback' if rollback else 'apply'
    for unit in ('fb-auto-post-service.service', 'x-tag-auto.service', 'x-tag-gateway.service', 'drama-material-api.service'):
        assert show(unit) == 'active', 'restart_still_pending:' + unit
        assert 'MainPID=' + show(unit, 'MainPID') + '\n' not in info['services_at_switch'][unit] + '\n', 'old_process_still_running:' + unit
    for lane in FILES:
        ops.run('curl', '--max-time', '5', '-fsS', 'http://127.0.0.1:' + ('18835' if lane == 'fb' else '18843') + '/health')
        ops.run(sys.executable, ops.HEALTH, 'restore', '--baseline', folder / (action + '-schedulers-before-' + lane + '.json'),
                '--wait-seconds', '25', '--output', folder / (action + '-schedulers-after-' + lane + '.json'))
        assert ops.config_hash(ops.LANES[lane][4], lane) == info['config_before'][lane], 'template_configuration_changed:' + lane
    for unit, status in info['extra_before'].items():
        if status == 'active' and show(unit) != 'active': ops.run('systemctl', 'start', unit)
        assert show(unit) == status, 'dependent_state_changed:' + unit
    for unit in TAG_TRIGGERS:
        if unit.endswith('.timer') and info['extra_before'][unit] == 'active':
            assert show(unit, 'NextElapseUSecRealtime') not in ('', 'n/a') or show(unit, 'NextElapseUSecMonotonic') not in ('', '0', 'infinity'), 'no_future_timer:' + unit
    oneshots = {u: ops.run('systemctl', 'show', u, '-p', 'Result,ExecMainStatus,ActiveEnterTimestamp,InactiveEnterTimestamp')
                for u in ('x-tag-auto-scheduler.service', 'x-tag-auto-runner.service')}
    for unit, status in oneshots.items():
        assert 'Result=success' in status and 'ExecMainStatus=0' in status, 'tag_scheduler_failed:' + unit
    after = ops.services()
    for unit in ('x-tag-post.service', 'pipeline-cos-native-fb.service', 'pipeline-cos-native-x-tag.service'):
        assert after[unit] == info['services_at_switch'][unit], 'publisher_process_changed:' + unit
    for name, row in info['files'].items():
        if is_static(name):
            assert ops.digest(name) in (row['before'], row['after']), 'preserve_later_static:' + name
            atomic(Path(name), Path(row['backup' if rollback else 'next']).read_bytes(), row)
        assert ops.digest(name) == row['before' if rollback else 'after'], 'installed_hash_mismatch:' + name
    info['status'] = 'rolled_back' if rollback else 'active'
    ops.save(folder / 'manifest.json', info)
    result = dict(status=info['status'], commit=info['commit'], files=len(info['files']), templates_unchanged=True,
                  services_after=after, tag_oneshots=oneshots, real_test_posts=0, historical_replays=0)
    ops.save(folder / ('verification-' + action + '.json'), result)
    print(json.dumps(result))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'apply', 'rollback', 'verify'])
    parser.add_argument('--folder', type=Path, required=True)
    args = parser.parse_args()
    folder = args.folder.resolve()
    if args.action == 'prepare': prepare(folder)
    elif args.action == 'verify': verify(folder)
    else: switch(folder, args.action == 'rollback')
