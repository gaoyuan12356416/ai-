"""Narrow GitHub-first FB hit-profile release; retain all publication receipts."""
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
import time

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
from scripts import deploy_hit_drama as release
from scripts import deploy_fb_profile_selection as profile_release
from scripts.deploy_pipeline_runtime import verify_source

BASE = '1f0e8a42'
FILES = ['features/fb_auto_posts/' + n + '.py' for n in ('core', 'hit_drama', 'hit_profile', 'client', 'repositories')]
FILES += ['features/hit_drama_target.py']
MAIN = Path('/root/drama_material_service')
MAIN_UNIT = 'drama-material-api.service'
MAIN_CLIENT = 'features/fb_auto_posts/client.py'
X_CURRENT = Path('/opt/x-tag-publisher/current')
SPY = Path('/mnt/data-disk/fb-video-publish/maintenance/inspect-tools/bin/py-spy')
TESTS = profile_release.TESTS + ['tests.test_fb_hit_profile', 'tests.test_fb_hit_receipt', 'scripts.test_hit_drama_target']


def baseline(name):
    value = subprocess.run(['git', '-C', str(SOURCE), 'show', BASE + ':' + name], capture_output=True)
    return value.stdout if value.returncode == 0 else None


def stage_external(folder, info, path, content):
    key = str(path)
    index = len(info['external'])
    old, new = folder / ('external-%s.old' % index), folder / ('external-%s.new' % index)
    shutil.copy2(path, old)
    new.write_bytes(content)
    info['external'][key] = dict(before=release.sha(path), after=hashlib.sha256(content).hexdigest(),
                                backup=str(old), next=str(new))


def prepare(folder, tests):
    assert release.run('findmnt', '-rn', '-o', 'UUID', '--mountpoint', '/mnt/data-disk') == '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    assert shutil.disk_usage('/mnt/data-disk').free > 3 * 1024**3, 'insufficient_data_disk_space'
    commit = verify_source(SOURCE, 'HEAD')
    origin = release.run('git', '-C', SOURCE, 'remote', 'get-url', 'origin')
    assert origin.rstrip('/').removesuffix('.git').endswith('gaoyuan12356416/ai-'), 'unexpected_source_repository'
    current, _, ledger = release.settings('fb')
    previous, candidate = current.resolve(), folder / 'candidate'
    assert current.is_symlink() and X_CURRENT.is_symlink(), 'runtime_pointer_required'
    folder.mkdir(parents=True, exist_ok=False)
    folder.chmod(0o755)
    shutil.copytree(previous, candidate, symlinks=True, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.git'))
    for target in [candidate, *candidate.rglob('*')]:
        original = previous / target.relative_to(candidate)
        if original.exists() and not target.is_symlink():
            stat = original.stat()
            os.chown(target, stat.st_uid, stat.st_gid)
    info = dict(platform='fb', commit=commit, baseline=release.run('git', '-C', SOURCE, 'rev-parse', BASE),
                source=str(SOURCE), previous=str(previous), candidate=str(candidate), files={}, static={},
                external={}, x_runtime=str(X_CURRENT.resolve()), status='prepared', main_status='prepared', assets_status='prepared')
    for name in FILES:
        live, target = previous / name, candidate / name
        assert target.resolve().is_relative_to(candidate.resolve()), 'overlay_symlink_escape:' + name
        original = baseline(name)
        if original is None:
            assert not live.exists(), 'new_file_collision:' + name
        else:
            assert live.read_bytes().replace(b'\r\n', b'\n') == original, 'baseline_changed:' + name
        content = (SOURCE / name).read_bytes()
        compile(content, name, 'exec')
        release.atomic(target, content)
        info['files'][name] = dict(before=release.sha(live), after=release.sha(target))
    assert (MAIN / MAIN_CLIENT).read_bytes().replace(b'\r\n', b'\n') == baseline(MAIN_CLIENT), 'main_client_baseline_changed'
    stage_external(folder, info, MAIN / MAIN_CLIENT, (SOURCE / MAIN_CLIENT).read_bytes())
    asset = (SOURCE / 'static/hit-drama-publish.js').read_bytes()
    old_asset = baseline('static/hit-drama-publish.js')
    version = hashlib.sha256(asset).hexdigest()[:12]
    for root, pages in [(candidate / 'static', ['fb-auto-publish-templates.html']),
                        (MAIN / 'static', ['fb-auto-publish-templates.html', 'x-tag-auto-publish-templates.html']),
                        (release.WEB, ['fb-auto-publish-templates.html', 'x-tag-auto-publish-templates.html']),
                        (X_CURRENT.resolve() / 'static', ['x-tag-auto-publish-templates.html'])]:
        js = root / 'hit-drama-publish.js'
        assert js.read_bytes().replace(b'\r\n', b'\n') == old_asset, 'shared_asset_baseline_changed:' + str(root)
        values = [(js, asset)]
        for page in pages:
            html = root / page
            text, count = re.subn(r'(/hit-drama-publish\.js)(?:\?[^"\']*)?', r'\1?v=' + version, html.read_text())
            assert count == 1, 'shared_asset_reference_changed:' + str(html)
            values.append((html, text.encode()))
        for path, content in values:
            if root == candidate / 'static':
                release.atomic(path, content)
            else:
                stage_external(folder, info, path, content)
    for module in tests:
        name = module.replace('.', '/') + '.py'
        assert (SOURCE / name).is_file(), 'test_module_missing:' + module
        release.atomic(candidate / name, (SOURCE / name).read_bytes())
    proof = subprocess.run([sys.executable, '-B', '-m', 'unittest', *tests, '-q'],
                           cwd=candidate, capture_output=True, text=True, timeout=180)
    (folder / 'tests.txt').write_text(proof.stdout + proof.stderr)
    assert proof.returncode == 0, 'candidate_tests_failed'
    info['profile_read_check'] = profile_release.verify(candidate, folder)
    with closing(sqlite3.connect(ledger.as_uri() + '?mode=ro', uri=True)) as src, \
            closing(sqlite3.connect(folder / 'ledger-before.sqlite3')) as dst:
        src.backup(dst)
    (folder / 'ledger-before.sqlite3').chmod(0o600)
    release.save(folder / 'manifest.json', info)
    print(json.dumps(dict(status='prepared', commit=commit, folder=str(folder),
                          files=len(FILES), external_files=len(info['external']))), flush=True)


def show(unit, field='ActiveState'):
    return release.run('systemctl', 'show', unit, '-p', field, '--value')


def main_idle(pid):
    """Check accepted requests and active background work by stack, without locals."""
    assert SPY.is_file(), 'main_stack_probe_unavailable'
    sockets = [line for line in release.run('ss', '-Hntp').splitlines() if ('pid=' + pid + ',') in line]
    listeners = [line for line in release.run('ss', '-Hlntp').splitlines() if ('pid=' + pid + ',') in line]
    children = Path('/proc', pid, 'task', pid, 'children').read_text().strip()
    threads = json.loads(release.run(SPY, 'dump', '--pid', pid, '--json', timeout=10))
    busy = []
    for thread in threads:
        name, frames = thread.get('thread_name', ''), thread.get('frames', [])
        funcs = [frame.get('name', '') for frame in frames]
        first = funcs[0] if funcs else ''
        idle = name == 'MainThread' and first == 'select' and 'serve_forever' in funcs
        idle |= name.startswith('youtube-card-status_') and first == '_worker'
        idle |= name == 'material-status-broadcast-worker' and first == 'wait' and 'material_status_worker_loop' in funcs
        idle |= name == 'material-status-mapping-cache-refresh' and first == 'wait' and 'material_status_mapping_cache_refresh_loop' in funcs
        idle |= name == 'material-replication-broadcast-worker' and first == 'wait' and '_loop' in funcs
        if not idle:
            busy.append(dict(thread=name, function=first))
    proof = dict(sockets=len(sockets), accept_queue=sum(int(line.split()[1]) for line in listeners),
                 child_processes=bool(children), busy_threads=busy, threads=len(threads))
    return not sockets and not proof['accept_queue'] and not children and not busy, proof


def api_ready():
    deadline = time.monotonic() + 30
    while True:
        try:
            for path, expected in [('/api/auth/status', '200'), ('/api/admin/fb-auto-publish/templates', '401')]:
                result = release.run('curl', '--max-time', '5', '-sS', '-o', '/dev/null', '-w', '%{http_code}',
                                     'http://127.0.0.1:8787' + path, timeout=8)
                assert result == expected, 'main_api_route_status:' + path
            return
        except (RuntimeError, AssertionError):
            if time.monotonic() >= deadline:
                raise
            time.sleep(.5)


def check_external(info, rollback=False):
    assert str(X_CURRENT.resolve()) == info['x_runtime'], 'x_runtime_changed'
    for name, record in info['external'].items():
        # A scoped switch may have stopped before installing all staged files.
        # Both known versions are safe; an unrelated later edit is not.
        expected = {record['before'], record['after']}
        assert release.sha(name) in expected, 'external_file_changed:' + name


def switch_main(folder, rollback=False):
    info = json.loads((folder / 'manifest.json').read_text())
    record = info['external'][str(MAIN / MAIN_CLIENT)]
    check_external(info, rollback)
    suffix = 'rollback' if rollback else 'apply'
    health_before = folder / ('main-youtube-before-' + suffix + '.json')
    release.run(sys.executable, release.HEALTH, 'snapshot', '--lane', 'youtube', '--output', health_before)
    dependents = ['drama-material-job-worker.service', 'drama-youtube-publish-worker.service']
    states = {unit: show(unit) for unit in dependents}
    pid = show(MAIN_UNIT, 'MainPID')
    assert pid != '0', 'main_api_not_running'
    marker = 'fb-hit-profile-main-' + str(os.getpid())
    rule = ['-p', 'tcp', '--dport', '8787', '-m', 'conntrack', '--ctstate', 'NEW',
            '-m', 'comment', '--comment', marker, '-j', 'REJECT', '--reject-with', 'tcp-reset']
    watchdog = marker + '-gate'
    release.run('systemd-run', '--quiet', '--unit=' + watchdog, '--on-active=180s', '--',
                '/usr/sbin/iptables', '-D', 'INPUT', *rule)
    gated = False
    try:
        release.run('iptables', '-I', 'INPUT', '1', *rule)
        gated = True
        deadline = time.monotonic() + 60
        while True:
            idle, proof = main_idle(pid)
            if idle:
                break
            assert time.monotonic() < deadline, 'main_requests_active_retry_later'
            time.sleep(.5)
        assert show(MAIN_UNIT, 'MainPID') == pid, 'main_api_pid_changed'
        assert release.sha(MAIN / MAIN_CLIENT) in {record['before'], record['after']}, 'main_client_changed'
        release.atomic(MAIN / MAIN_CLIENT, Path(record['backup' if rollback else 'next']).read_bytes())
        release.run('systemctl', 'restart', MAIN_UNIT, timeout=55)
        assert show(MAIN_UNIT) == 'active', 'main_api_restart_failed'
        release.save(folder / ('main-switch-' + suffix + '.json'),
                     dict(old_pid=pid, new_pid=show(MAIN_UNIT, 'MainPID'), drain=proof, dependencies=states))
    finally:
        if gated:
            release.run('iptables', '-D', 'INPUT', *rule)
        subprocess.run(['systemctl', 'stop', watchdog + '.timer'], capture_output=True, timeout=10)
        for unit, state in states.items():
            if state == 'active' and show(unit) != 'active':
                release.run('systemctl', 'start', unit)
        release.run(sys.executable, release.HEALTH, 'restore', '--baseline', health_before,
                    '--wait-seconds', '25', '--output', folder / ('main-youtube-after-' + suffix + '.json'), timeout=55)
    api_ready()
    info = json.loads((folder / 'manifest.json').read_text())
    info['main_status'] = 'rolled_back' if rollback else 'active'
    release.save(folder / 'manifest.json', info)


def assets(folder, rollback=False):
    info = json.loads((folder / 'manifest.json').read_text())
    # Main client is switched independently, after draining accepted HTTP work.
    static_info = dict(info, external={k: v for k, v in info['external'].items() if k != str(MAIN / MAIN_CLIENT)})
    check_external(static_info, rollback)
    for name, record in static_info['external'].items():
        release.atomic(name, Path(record['backup' if rollback else 'next']).read_bytes())
    info['assets_status'] = 'rolled_back' if rollback else 'active'
    release.save(folder / 'manifest.json', info)


def activate(folder, rollback=False):
    info = json.loads((folder / 'manifest.json').read_text())
    check_external(info, rollback)
    if rollback:
        # This also handles a partially completed apply without overwriting any
        # later source. Existing publication facts are never restored from backup.
        switch_main(folder, True)
        assets(folder, True)
        current = str(release.settings('fb')[0].resolve())
        if current == info['candidate']:
            release.activate('fb', folder, True)
        else:
            assert current == info['previous'], 'later_fb_release_present'
            info = json.loads((folder / 'manifest.json').read_text())
            info['status'] = 'rolled_back'
            release.save(folder / 'manifest.json', info)
    else:
        if info['status'] != 'active':
            release.activate('fb', folder)
        if info['main_status'] != 'active':
            switch_main(folder)
        assets(folder)
    print(json.dumps(dict(status='rolled_back' if rollback else 'active', folder=str(folder))), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'apply', 'rollback'))
    parser.add_argument('--folder', type=Path, required=True)
    parser.add_argument('--test-module', action='append')
    args = parser.parse_args()
    folder = args.folder.resolve()
    assert folder.is_relative_to('/mnt/data-disk'), 'data_disk_folder_required'
    if args.action == 'prepare':
        prepare(folder, args.test_module or TESTS)
    else:
        activate(folder, args.action == 'rollback')


if __name__ == '__main__':
    main()
