#!/usr/bin/env python3
"""Scoped GitHub-first template-save deployment; never sends an X Post."""
import argparse
import ast
import contextlib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request

DATA = Path('/mnt/data-disk/x-post-automation')
CURRENT = Path('/opt/x-post-automation/current')
SERVICE = 'x-auto-post-service.service'
TRIGGERS = ['x-auto-post-runner.timer', 'x-auto-post-scheduler.timer', 'x-auto-post-metric.timer', 'x-auto-post-runner.path']
WORKERS = ['x-auto-post-runner.service', 'x-auto-post-scheduler.service', 'x-auto-post-metric.service']
OTHER = ['x-post-schedule.timer', 'x-post-schedule-claim.timer', 'x-post-manual.timer', 'x-post-daily.timer']
EXPECTED = {
    'features/x_auto_posts/service.py': '9cd5f2132ba3eafe2a36ee2835e87bd1ba9e213bb7ee99351ecf1b29daed364a',
    'static/x-auto-publish-template.js': 'a44d419c0ae8e816316cca01599270d8cb9fc3ab0353d3fa90450763373ae6e9',
    'static/x-auto-publish-template.html': '2823e8ea3478b922dab78a758a7c2297bfa14b424691032fa116c1322495ca00',
}

def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)

def output(*args):
    return subprocess.check_output(args, text=True).strip()

def state(unit):
    return output('systemctl', 'show', unit, '-p', 'ActiveState', '--value')

def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def process_env():
    pid = output('systemctl', 'show', SERVICE, '-p', 'MainPID', '--value')
    return dict(x.split('=', 1) for x in Path('/proc/' + pid + '/environ').read_bytes().decode().split('\0') if '=' in x)

def api(path, env):
    request = urllib.request.Request('http://127.0.0.1:18833' + path, headers={'Authorization': 'Bearer ' + env['X_AUTO_POST_INTERNAL_TOKEN']})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)

def ready():
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen('http://127.0.0.1:18833/health', timeout=2) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(.5)
    raise RuntimeError('Auto service readiness failed')

def rehearse(stage):
    # Only GET the live DTOs. Both actual save calls use a fresh isolated DB.
    sys.path.insert(0, str(CURRENT.resolve()))
    module_name = 'features.x_auto_posts.save_candidate'
    spec = importlib.util.spec_from_file_location(module_name, stage / 'features/x_auto_posts/service.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    env = process_env()
    template = api('/api/admin/x-auto-publish/templates/4', env)['template']
    class Bridge:
        calls = 0
        def accounts(self):
            self.calls += 1
            return api('/api/admin/x-auto-publish/accounts', env)['items']
    bridge = Bridge()
    with tempfile.TemporaryDirectory(dir=stage, prefix='rehearsal-') as scratch:
        store = module.XAutoPostStore(Path(scratch) / 'isolated.sqlite3')
        service = module.XAutoPostService(store, bridge, None, None)
        payload = {**template['config'], 'name': template['name'], '_actor': {'user_id': 'save-rehearsal'}}
        created = service.create_template(payload)['template']
        start = time.monotonic()
        saved = service.update_template(created['id'], {**payload, 'expected_version': 1})['template']
        seconds = round(time.monotonic() - start, 3)
        assert saved['version'] == 2 and saved['config'] == created['config']
        assert bridge.calls == 2 and not store.list_runs()
    after = api('/api/admin/x-auto-publish/templates/4', env)['template']
    assert after['version'] == template['version'] and after['config'] == template['config']
    result = {'isolated_save_seconds': seconds, 'selected_accounts': len(saved['config']['account_ids']), 'account_list_calls_per_save': 1, 'live_template_version_unchanged': after['version']}
    print(json.dumps(result), flush=True)
    return result

@contextlib.contextmanager
def drained():
    before = {u: state(u) for u in TRIGGERS + OTHER}
    active = [u for u in TRIGGERS if before[u] == 'active']
    rescue = 'x-template-save-restore-' + str(os.getpid())
    if active:
        run('systemd-run', '--quiet', '--unit=' + rescue, '--on-active=5m', '/usr/bin/systemctl', 'start', *active)
    try:
        if active:
            run('systemctl', 'stop', *active)
        deadline = time.monotonic() + 180
        while any(state(u) in {'active', 'activating', 'deactivating'} for u in WORKERS):
            if time.monotonic() >= deadline:
                raise RuntimeError('Active workers have not drained; no code switched')
            time.sleep(2)
        yield before
    finally:
        if active:
            run('systemctl', 'start', *active)
            if all(state(u) == 'active' for u in active):
                run('systemctl', 'stop', rescue + '.timer')
        if any(state(u) != before[u] for u in TRIGGERS + OTHER):
            raise RuntimeError('Timer state changed; inspect saved states')

def restore(backup):
    manifest = json.loads((backup / 'manifest.json').read_text())
    for item in manifest['files']:
        target = Path(item['target'])
        shutil.copy2(backup / item['backup'], target)
        assert digest(target) == item['before']
    run('systemctl', 'restart', SERVICE)
    ready()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--commit')
    parser.add_argument('--rollback', type=Path)
    args = parser.parse_args()
    assert output('findmnt', '-no', 'UUID', '/mnt/data-disk') == '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    assert shutil.disk_usage(DATA).free > 2 * 1024**3
    if args.rollback:
        with drained():
            restore(args.rollback)
        print('Code rollback complete; databases and tokens preserved')
        return
    commit = args.commit or ''
    assert len(commit) == 40 and all(c in '0123456789abcdef' for c in commit)
    repo = DATA / 'template-save-deploy.git'
    if not repo.exists():
        run('git', 'init', '--bare', str(repo), stdout=subprocess.DEVNULL)
    env = dict(os.environ, GIT_SSH_COMMAND='ssh -i /root/.ssh/github_codex_cpu_ed25519 -o IdentitiesOnly=yes')
    run('git', '--git-dir=' + str(repo), 'fetch', '--depth=1', 'git@github.com:gaoyuan12356416/ai-.git', commit, env=env)
    assert output('git', '--git-dir=' + str(repo), 'rev-parse', 'FETCH_HEAD') == commit
    stage = DATA / 'maintenance' / ('20261103-template-save-' + commit[:10])
    stage.mkdir(parents=True, exist_ok=True)
    for name in [*EXPECTED, 'scripts/deploy_x_template_save.py']:
        target = stage / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(subprocess.check_output(['git', '--git-dir=' + str(repo), 'show', commit + ':' + name]))
    ast.parse((stage / 'features/x_auto_posts/service.py').read_text())
    rehearsal = rehearse(stage)
    runtime = CURRENT.resolve()
    targets = []
    for name, expected in EXPECTED.items():
        roots = [runtime]
        if name.startswith('static/'):
            roots += [Path('/root/drama_material_service'), Path('/usr/share/nginx/html')]
        for root in roots:
            target = root / (Path(name).name if str(root) == '/usr/share/nginx/html' else name)
            assert digest(target) == expected, str(target) + ' changed since inspection'
            targets.append((name, target))
    backup = stage / 'backup'
    backup.mkdir(mode=0o700)
    manifest = {'commit': commit, 'runtime': str(runtime), 'rehearsal': rehearsal, 'files': [], 'backup': str(backup)}
    for index, (name, target) in enumerate(targets):
        shutil.copy2(target, backup / str(index))
        manifest['files'].append({'target': str(target), 'source': name, 'backup': str(index), 'before': digest(target), 'after': digest(stage / name)})
    with drained() as before:
        manifest['units_before'] = before
        manifest['pids_before'] = {u: output('systemctl', 'show', u, '-p', 'MainPID', '--value') for u in [SERVICE, 'x-post-automation.service', 'drama-material-api.service']}
        for source, name in [(Path('/mnt/data-disk/x-auto-post-publisher/x-auto-post.sqlite3'), 'auto.sqlite3'), (Path('/var/lib/x-post-automation/accounts.sqlite3'), 'posts.sqlite3')]:
            with contextlib.closing(sqlite3.connect('file:' + str(source) + '?mode=ro', uri=True)) as src, contextlib.closing(sqlite3.connect(str(backup / name))) as dst:
                src.backup(dst)
            os.chmod(backup / name, 0o600)
        (backup / 'manifest.json').write_text(json.dumps(manifest, indent=2))
        assert CURRENT.resolve() == runtime
        try:
            for item in manifest['files']:
                target = Path(item['target'])
                assert digest(target) == item['before']
                temporary = target.with_name(target.name + '.save-new')
                shutil.copy2(target, temporary)
                temporary.write_bytes((stage / item['source']).read_bytes())
                temporary.replace(target)
            run('systemctl', 'restart', SERVICE)
            ready()
            for item in manifest['files']:
                assert digest(item['target']) == item['after']
            for unit in ['x-post-automation.service', 'drama-material-api.service']:
                assert output('systemctl', 'show', unit, '-p', 'MainPID', '--value') == manifest['pids_before'][unit]
            manifest['verification'] = rehearse(runtime)
        except BaseException:
            restore(backup)
            raise
    manifest['status'] = 'deployed'
    manifest['units_after'] = {u: state(u) for u in TRIGGERS + OTHER}
    (backup / 'manifest.json').write_text(json.dumps(manifest, indent=2))
    print(json.dumps({'status': 'deployed', 'commit': commit, 'backup': str(backup), 'verification': manifest['verification']}), flush=True)

if __name__ == '__main__':
    main()
