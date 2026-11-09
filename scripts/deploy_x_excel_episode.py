"""Scoped GitHub release for Excel free episodes; never restore business data."""
import argparse
from datetime import datetime, timezone
import difflib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parent))
import deploy_x_pool_local_failures as base

BASE_COMMIT = '3f7d9884'
FILES = ['features/x_video_publish/material_identity.py', 'features/x_video_publish/episode_sources.py',
    'features/x_video_publish/sources.py', 'features/x_video_publish/core.py',
    'features/x_video_publish/service.py', 'features/x_posts/service.py',
    'scripts/x_post_manual_runner.py', 'static/x-excel-publish.js', 'static/x-video-publish.html']
NEW_FILES = set(FILES[:2])
TESTS = ['scripts/test_x_excel_episode_sources.py', 'scripts/test_x_excel_episode_pipeline.py',
         'scripts/test_x_excel_episode_frontend.js', 'scripts/test_x_video_publish_core.py']
HEALTH = '/opt/publish-scheduler-health/current/scripts/publish_scheduler_health.py'
DAEMON = 'x-video-pool-scheduler.service'


def atomic_copy(source, target):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    old = target.stat() if target.exists() else target.parent.stat()
    temp = target.with_name(target.name + '.excel-next')
    shutil.copyfile(source, temp)
    os.chown(temp, old.st_uid, old.st_gid)
    temp.chmod((old.st_mode & 0o7777) if target.exists() else 0o644)
    temp.replace(target)


base.copy = atomic_copy


def health(action, folder, lane):
    args = ['python3', HEALTH, action]
    if action == 'snapshot':
        args += ['--lane', lane, '--output', str(folder / (lane + '-before.json'))]
    else:
        args += ['--baseline', str(folder / (lane + '-before.json')), '--wait-seconds', '25',
                 '--output', str(folder / (lane + '-after.json'))]
    return base.run(*args)


def api_work():
    with base.contextlib.closing(base.readonly(base.MAIN / 'data/drama_material_jobs.sqlite3')) as conn:
        # These are API-owned in-memory screenshot threads, including queued
        # retries. Other channels and external drama workers need not be idle.
        return conn.execute("SELECT COUNT(*) FROM drama_screenshot_job WHERE status NOT IN ('done','failed')").fetchone()[0]


def prepare():
    assert base.run('findmnt', '-no', 'UUID', '/mnt/data-disk') == base.UUID
    assert shutil.disk_usage(base.ROOT).free > 2 * 1024**3
    assert not base.run('git', '-C', str(base.SOURCE), 'status', '--porcelain')
    commit = base.run('git', '-C', str(base.SOURCE), 'rev-parse', 'HEAD')
    assert base.run('git', '-C', str(base.SOURCE), 'branch', '-r', '--contains', commit)
    old = base.CURRENT.resolve()
    for rel in FILES:
        if rel in NEW_FILES:
            assert not (old / rel).exists(), 'concurrent new module: ' + rel
        else:
            original = subprocess.check_output(['git', '-C', str(base.SOURCE), 'show', BASE_COMMIT + ':' + rel])
            assert (old / rel).read_bytes().replace(b'\r\n', b'\n') == original.replace(b'\r\n', b'\n'), 'live drift: ' + rel
    tag = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-excel-episode-' + commit[:8]
    folder, release = base.ROOT / 'backups' / tag, base.ROOT / 'releases' / tag
    folder.mkdir(mode=0o700)
    before = base.tree(old)
    shutil.copytree(old, release, symlinks=True, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    for rel in FILES + TESTS:
        atomic_copy(base.SOURCE / rel, release / rel)
        if rel.endswith('.py'): compile((release / rel).read_bytes(), rel, 'exec')
    assert base.tree(old) == before
    # Main API has independent overlays. Apply only this feature's exact diff.
    stage = folder / 'main-candidate'
    (stage / 'features/x_posts').mkdir(parents=True)
    shutil.copy2(base.MAIN / 'features/x_posts/service.py', stage / 'features/x_posts/service.py')
    rel = 'features/x_posts/service.py'
    original = subprocess.check_output(['git', '-C', str(base.SOURCE), 'show', BASE_COMMIT + ':' + rel]).decode()
    updated = (base.SOURCE / rel).read_text()
    # The existing main API intentionally lacks this X-runner-only call
    # argument. Omit it from both diff contexts; do not add it to main.
    runtime_only = '                allow_deferred_media=os.environ.get("PIPELINE_X_DISPATCH_ENABLED") == "1",\n'
    assert original.count(runtime_only) == updated.count(runtime_only) == 1
    patch = ''.join(difflib.unified_diff(original.replace(runtime_only, '').splitlines(keepends=True),
        updated.replace(runtime_only, '').splitlines(keepends=True), fromfile='a/' + rel, tofile='b/' + rel)).encode()
    subprocess.run(['git', 'apply', '--unsafe-paths', '--directory=' + str(stage)],
                   input=patch, cwd=folder, check=True, capture_output=True)
    external = []
    for rel in FILES:
        if rel.startswith('scripts/'):
            continue
        targets = [base.MAIN / rel]
        if rel.startswith('static/'):
            targets.append(Path('/usr/share/nginx/html') / Path(rel).name)
        for target in targets:
            exists = target.exists()
            if rel in NEW_FILES:
                assert not exists, 'concurrent main module: ' + str(target)
            elif rel != 'features/x_posts/service.py':
                assert base.digest(target) == base.digest(old / rel), 'external drift: ' + str(target)
            saved, candidate = folder / ('before-' + str(len(external))), folder / ('next-' + str(len(external)))
            if exists: shutil.copy2(target, saved)
            else: saved.write_bytes(b'')
            shutil.copy2(stage / rel if rel == 'features/x_posts/service.py' else base.SOURCE / rel, candidate)
            if rel.endswith('.py'): compile(candidate.read_bytes(), rel, 'exec')
            external.append({'path': str(target), 'before': str(saved), 'after': str(candidate),
                'before_sha': base.digest(saved), 'after_sha': base.digest(candidate), 'existed': exists})
    main_external = [item for item in external if item['path'].startswith(str(base.MAIN) + '/features/')]
    # The free-episode CDN is already trusted by the active X publisher. Add
    # that same host to the separate interactive metadata reader, preserving
    # all current entries and keeping credentials out of release artifacts.
    def process_env(unit):
        pid = base.run('systemctl', 'show', unit, '-p', 'MainPID', '--value')
        return dict(part.decode().split('=', 1) for part in Path('/proc/' + pid + '/environ').read_bytes().split(b'\0') if b'=' in part)
    api_env, x_env = process_env('drama-material-api.service'), process_env('x-post-automation.service')
    assert 'img.tianmai.cn' in x_env.get('X_POST_MEDIA_ALLOWED_HOSTS', '').split(',')
    hosts = list(dict.fromkeys(filter(None, api_env.get('X_POST_SOURCE_DURATION_ALLOWED_HOSTS', '').split(',') + ['img.tianmai.cn'])))
    assert all(host and all(char.isalnum() or char in '.-*' for char in host) for host in hosts)
    env_path = Path('/etc/x-excel-episode-source.env')
    schedule_lines = [line.split('=', 1)[1].strip().strip('"').strip("'")
        for line in Path('/etc/x-post-schedule.env').read_text().splitlines()
        if line.startswith('X_POST_SCHEDULE_MEDIA_ALLOWED_HOSTS=')]
    assert len(schedule_lines) == 1
    manual_hosts = list(dict.fromkeys(host.strip() for host in schedule_lines[0].split(',') if host.strip()))
    assert 'img.tianmai.cn' in manual_hosts
    assert all(all(char.isalnum() or char in '.-*' for char in host) for host in manual_hosts)
    manual_env_path = Path('/etc/x-excel-episode-manual-source.env')
    config_files = [(env_path, 'X_POST_SOURCE_DURATION_ALLOWED_HOSTS=' + ','.join(hosts) + '\n'),
        (Path('/etc/systemd/system/drama-material-api.service.d/zzzz-x-excel-episode-source.conf'),
         '[Service]\nEnvironmentFile=' + str(env_path) + '\n'),
        (manual_env_path, 'X_POST_SOURCE_DURATION_ALLOWED_HOSTS=' + ','.join(manual_hosts) + '\n'),
        (Path('/etc/systemd/system/x-post-manual.service.d/zzzz-x-excel-episode-source.conf'),
         '[Service]\nEnvironmentFile=' + str(manual_env_path) + '\n')]
    for index, (target, content) in enumerate(config_files):
        assert not target.exists(), 'concurrent configuration: ' + str(target)
        # API and manual units use the same drop-in basename in different
        # directories. Keep their staged files distinct as well.
        suffix = str(index) + '-' + target.name
        saved, candidate = folder / ('before-config-' + suffix), folder / ('next-config-' + suffix)
        saved.write_bytes(b''); candidate.write_text(content)
        main_external.append({'path': str(target), 'before': str(saved), 'after': str(candidate),
            'before_sha': base.digest(saved), 'after_sha': base.digest(candidate), 'existed': False})
    manifest = {'phase': 'prepared', 'commit': commit, 'source': str(base.SOURCE),
        'old_release': str(old), 'new_release': str(release), 'old_tree': before,
        'new_tree': base.tree(release), 'external': [item for item in external if item not in main_external],
        'main_external': main_external}
    base.save(folder, manifest)
    return {'phase': 'prepared', 'folder': str(folder), 'release': str(release), 'commit': commit}


def drain(folder, manifest, rollback=False):
    assert manifest['phase'] == ('complete' if rollback else 'prepared')
    if api_work():
        return {'phase': 'waiting_api_work', 'api_screenshot_tasks': api_work()}
    if rollback:
        with base.contextlib.closing(base.readonly(base.DB)) as conn:
            assert not conn.execute('SELECT 1 FROM x_video_publish_task WHERE length(material_id)=32 LIMIT 1').fetchone(), \
                'Free-episode history exists; retain identity-compatible runtime, roll back only Excel UI instead.'
    health('snapshot', folder, 'x')
    health('snapshot', folder, 'youtube')
    manifest['personal_scheduler_was_active'] = base.state(DAEMON) == 'active'
    manifest['main_pid_before'] = base.run('systemctl', 'show', 'drama-material-api.service', '-p', 'MainPID', '--value')
    manifest['main_preactivated'] = False
    result = base.drain(folder, manifest, rollback)
    if manifest['personal_scheduler_was_active']:
        # Handler sets stop and finishes its current bounded HTTP call. Exit 0
        # stays stopped under this daemon's Restart=on-failure unit contract.
        assert base.run('systemctl', 'show', DAEMON, '-p', 'Restart', '--value') == 'on-failure'
        base.run('systemctl', 'kill', '--kill-who=main', '--signal=SIGTERM', DAEMON)
    return result


def api_ready():
    deadline = time.monotonic() + 30
    while True:
        try:
            with base.urllib.request.urlopen('http://127.0.0.1:8787/api/ui/topbar', timeout=2) as response:
                assert response.status == 200
            return
        except (OSError, urllib.error.URLError):
            if time.monotonic() > deadline: raise
            time.sleep(.5)


def install_main(folder, manifest, *, restore=False):
    rollback = bool(manifest.get('rollback'))
    target_before = rollback != restore
    current_key, next_key = ('after', 'before') if target_before else ('before', 'after')
    if api_work(): return False
    rule = ['OUTPUT', '-d', '127.0.0.1', '-p', 'tcp', '--dport', '8787', '-m', 'conntrack',
        '--ctstate', 'NEW', '-m', 'comment', '--comment', 'x-excel-' + manifest['commit'][:8],
        '-j', 'REJECT', '--reject-with', 'tcp-reset']
    gated = changed = False
    try:
        base.run('iptables', '-w', '5', '-I', *rule); gated = True
        deadline = time.monotonic() + 20
        while base.run('ss', '-Htn', 'state', 'established', 'sport', '=', ':8787'):
            if time.monotonic() > deadline: return False
            time.sleep(.25)
        if api_work(): return False
        for item in manifest['main_external']:
            path = Path(item['path'])
            if not item['existed'] and current_key == 'before':
                assert not path.exists(), 'concurrent new main file: ' + str(path)
            else:
                assert base.digest(path) == item[current_key + '_sha'], 'concurrent main file: ' + str(path)
        changed = True
        for item in manifest['main_external']:
            if target_before and not item['existed']: Path(item['path']).unlink(missing_ok=True)
            else: atomic_copy(Path(item[next_key]), Path(item['path']))
        base.run('systemctl', 'daemon-reload')
        base.run('systemctl', 'restart', 'drama-material-api.service')
        base.run('iptables', '-w', '5', '-D', *rule); gated = False
        api_ready()
        manifest['main_preactivated'] = not restore
        manifest['main_pid_after'] = base.run('systemctl', 'show', 'drama-material-api.service', '-p', 'MainPID', '--value')
        base.save(folder, manifest)
        return True
    except BaseException:
        if changed:
            for item in manifest['main_external']:
                if current_key == 'before' and not item['existed']: Path(item['path']).unlink(missing_ok=True)
                else: atomic_copy(Path(item[current_key]), Path(item['path']))
            base.run('systemctl', 'daemon-reload')
            base.run('systemctl', 'restart', 'drama-material-api.service')
        raise
    finally:
        if gated: base.run('iptables', '-w', '5', '-D', *rule)


def activate(folder, manifest):
    observed = base.active()
    if any(observed.values()): return {'phase': 'waiting', **observed}
    # Activate the backward-compatible source broker first. Thus an X owner
    # Wants dependency can safely start its pool daemon after the X cutover.
    if not manifest.get('main_preactivated') and not install_main(folder, manifest):
        return {'phase': 'waiting_api_work', 'api_screenshot_tasks': api_work()}
    original_resume = base.resume
    base.resume = lambda *_: None
    owners_ready = False
    try:
        result = base.activate(folder, manifest)
        if result['phase'] not in {'complete', 'rolled_back'}: return result
        api_ready()
        owners_ready = True
        original_resume(folder, manifest)
        for lane in ('x', 'youtube'): health('restore', folder, lane)
        manifest['schedulers_verified'] = True
        base.save(folder, manifest)
        return result
    except BaseException:
        if manifest['phase'] == 'failed_restored' and manifest.get('main_preactivated'):
            if not install_main(folder, manifest, restore=True):
                manifest['phase'] = 'restore_pending_api_work'
                base.save(folder, manifest)
            else:
                api_ready()
                base.ready(8810)
                owners_ready = True
        raise
    finally:
        base.resume = original_resume
        if owners_ready:
            original_resume(folder, manifest)
            if manifest.get('personal_scheduler_was_active'):
                base.run('systemctl', 'start', DAEMON)
        elif manifest['phase'] in {'complete', 'rolled_back', 'failed_restored'}:
            manifest['phase'] = 'activation_needs_attention'
            base.save(folder, manifest)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--phase', choices=('prepare', 'drain', 'switch', 'status', 'abort', 'rollback'), required=True)
    parser.add_argument('--folder', type=Path)
    args = parser.parse_args()
    if args.phase == 'prepare':
        result = prepare()
    else:
        folder = args.folder.resolve()
        assert folder.parent == base.ROOT / 'backups' and '-excel-episode-' in folder.name
        manifest = json.loads((folder / 'manifest.json').read_text())
        if args.phase in ('drain', 'rollback'):
            result = drain(folder, manifest, args.phase == 'rollback')
        elif args.phase == 'switch':
            result = activate(folder, manifest)
        elif args.phase == 'status':
            result = {'phase': manifest['phase'], 'api_screenshot_tasks': api_work(), **base.active()}
        else:
            assert manifest['phase'] == 'draining'
            base.resume(folder, manifest)
            if manifest.get('personal_scheduler_was_active'): base.run('systemctl', 'start', DAEMON)
            manifest['phase'] = 'complete' if manifest.get('rollback') else 'prepared'
            base.save(folder, manifest)
            result = {'phase': manifest['phase'], 'admissions_restored': True}
    print(json.dumps(result), flush=True)
