"""GitHub-first X planner/verification overlay with a scoped, resumable drain.

Only X admission is paused. Running jobs finish normally. No business rows,
leases, reservations or credentials are restored by deploy/rollback.
"""
import argparse
import contextlib
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
import urllib.request

SOURCE = Path(__file__).resolve().parents[1]
ROOT = Path('/mnt/data-disk/x-post-automation')
CURRENT = Path('/opt/x-post-automation/current')
MAIN = Path('/root/drama_material_service')
DB = Path('/var/lib/x-post-automation/accounts.sqlite3')
AUTO_DB = Path('/mnt/data-disk/x-auto-post-publisher/x-auto-post.sqlite3')
BASELINE = {
    'features/x_posts/service.py': '5ce9bf016a7209d694d255dab35179ea8f26e7e7861dcc9e28c25e1224b62d57',
    'features/x_posts/account_blockers.py': 'a7a4f72084346a8af1b25e373a254db244992a8dbfec9d66f1449ec6ed60bd9e',
    'features/x_accounts/oauth_service.py': '66cc3a85d66df8be5bc77188aa1dde7015b7c07b4f8306d2d5a93beb3a15a69d',
    'scripts/x_post_schedule_runner.py': '13c8ccaa969116264fc1ffacb316c7d218f80f6df8aa4e84ce2b6564a91fbb16',
}
TRIGGERS = ['x-post-schedule.timer', 'x-post-schedule-claim.timer', 'x-post-manual.timer',
    'x-post-manual.path', 'x-auto-post-runner.timer', 'x-auto-post-scheduler.timer',
    'x-auto-post-runner.path', 'x-auto-post-metric.timer', 'x-video-pool-scheduler.timer']
CALLERS = [u.replace('.timer', '.service') for u in TRIGGERS if u.endswith('.timer')]
UNITS = ['x-post-automation.service', 'x-auto-post-service.service']
UUID = '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'

def run(*args):
    p = subprocess.run(args, capture_output=True, text=True, timeout=60)
    if p.returncode:
        raise RuntimeError('command failed: ' + ' '.join(args[:3]))
    return p.stdout.strip()

def state(unit): return run('systemctl', 'show', unit, '-p', 'ActiveState', '--value')
def digest(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def tree(path):
    return {p.relative_to(path).as_posix(): digest(p) for p in path.rglob('*')
        if p.is_file() and '__pycache__' not in p.parts and '.git' not in p.parts and p.suffix != '.pyc'}

def save(folder, value):
    temporary = folder / 'manifest.next'
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.chmod(0o600)
    temporary.replace(folder / 'manifest.json')

def copy(source, target):
    old = target.stat()
    temp = target.with_name(target.name + '.x-pool-next')
    shutil.copy2(source, temp)
    os.chown(temp, old.st_uid, old.st_gid)
    temp.chmod(old.st_mode & 0o7777)
    temp.replace(target)

def switch(release):
    temp = CURRENT.with_name('x-pool-next')
    temp.symlink_to(release)
    temp.replace(CURRENT)

def readonly(path):
    c = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=10)
    c.execute('PRAGMA query_only=ON')
    return c

def controller():
    sys.path.insert(0, '/opt/pipeline-dispatch/current')
    from features.pipeline_dispatch.core import DispatchStore
    return DispatchStore('/mnt/data-disk/pipeline-dispatch/dispatch.sqlite3')

def active():
    with contextlib.closing(readonly('/mnt/data-disk/pipeline-dispatch/dispatch.sqlite3')) as c:
        jobs = c.execute("SELECT COUNT(*) FROM pipeline_job WHERE channel='x' AND status='running'").fetchone()[0]
    callers = [u for u in CALLERS if state(u) in {'active', 'activating', 'deactivating'}]
    connections = sum(bool(run('ss', '-Htn', 'state', 'established', 'sport', '=', ':' + str(p))) for p in (8810, 18833))
    return {'running_x_jobs': jobs, 'active_x_callers': callers, 'accepted_x_ports': connections}

def ready(port):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen('http://127.0.0.1:%s/health' % port, timeout=2) as r:
                if r.status == 200: return
        except Exception:
            pass
        time.sleep(.5)
    raise RuntimeError('X service health unavailable: ' + str(port))

def prepare():
    assert run('findmnt', '-no', 'UUID', '/mnt/data-disk') == UUID
    assert shutil.disk_usage(ROOT).free > 2 * 1024**3
    assert not run('git', '-C', str(SOURCE), 'status', '--porcelain')
    commit = run('git', '-C', str(SOURCE), 'rev-parse', 'HEAD')
    assert run('git', '-C', str(SOURCE), 'branch', '-r', '--contains', commit)
    old = CURRENT.resolve()
    for relative, expected in BASELINE.items():
        assert digest(old / relative) == expected, 'live file changed: ' + relative
    tag = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-pool-local-' + commit[:8]
    folder = ROOT / 'backups' / tag
    folder.mkdir(mode=0o700)
    release = ROOT / 'releases' / tag
    before_tree = tree(old)
    shutil.copytree(old, release, symlinks=True, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    for relative in BASELINE:
        copy(SOURCE / relative, release / relative)
        compile((release / relative).read_bytes(), relative, 'exec')
    after_tree = tree(release)
    assert {k for k in before_tree if before_tree[k] != after_tree.get(k)} == set(BASELINE)
    assert tree(old) == before_tree
    external = []
    for relative in ('features/x_posts/service.py', 'features/x_posts/account_blockers.py'):
        target = MAIN / relative
        saved, candidate = folder / ('before-main-' + target.name), folder / ('next-main-' + target.name)
        shutil.copy2(target, saved)
        if target.name == 'service.py':
            # Preserve unrelated main-API overlays; apply only the proven query change.
            original = saved.read_text()
            needle = '                    "ORDER BY p.created_at DESC,p.id DESC LIMIT 1000"'
            assert original.count(needle) == 1
            updated = original.replace(needle,
                '                    "AND NOT EXISTS(SELECT 1 FROM x_post_manual_material_reservation r "\n'
                '                    "WHERE r.material_key=p.material_key AND r.state=\'active\') "\n' + needle)
            candidate.write_text(updated)
        else:
            assert digest(target) == BASELINE[relative]
            shutil.copy2(SOURCE / relative, candidate)
        compile(candidate.read_bytes(), relative, 'exec')
        external.append({'path': str(target), 'before': str(saved), 'after': str(candidate),
            'before_sha': digest(saved), 'after_sha': digest(candidate)})
    manifest = {'phase': 'prepared', 'commit': commit, 'source': str(SOURCE),
        'old_release': str(old), 'new_release': str(release), 'old_tree': before_tree,
        'new_tree': after_tree, 'external': external}
    save(folder, manifest)
    return {'phase': 'prepared', 'folder': str(folder), 'commit': commit}

def drain(folder, m, rollback=False):
    assert m['phase'] in ({'complete'} if rollback else {'prepared'})
    config = controller().config()
    m.update(phase='draining', rollback=rollback, x_enabled='x' in config['enabled_channels'],
        x_prepare_only='x' in config.get('preparation_only_channels', []),
        triggers={u: state(u) for u in TRIGGERS}, units={u: state(u) for u in UNITS})
    save(folder, m)
    if m['x_enabled']: controller().channel_enabled('x', False)
    for unit, previous in m['triggers'].items():
        if previous == 'active': run('systemctl', 'stop', unit)
    return {'phase': 'draining', **active()}

def resume(folder, m):
    if m.get('x_enabled'):
        controller().channel_enabled('x', True, preparation_only=m.get('x_prepare_only', False))
    for unit, previous in m.get('triggers', {}).items():
        if previous == 'active': run('systemctl', 'start', unit)

def activate(folder, m):
    assert m['phase'] == 'draining'
    observed = active()
    if any(observed.values()): return {'phase': 'waiting', **observed}
    rollback = m.get('rollback', False)
    current_key, next_key = ('new', 'old') if rollback else ('old', 'new')
    assert str(CURRENT.resolve()) == m[current_key + '_release']
    assert tree(CURRENT.resolve()) == m[current_key + '_tree'], 'concurrent runtime change'
    for item in m['external']:
        assert digest(item['path']) == item['after_sha' if rollback else 'before_sha'], 'concurrent main change'
    rule = ['OUTPUT', '-d', '127.0.0.1', '-p', 'tcp', '--dport', '8810', '-m', 'conntrack',
        '--ctstate', 'NEW', '-m', 'comment', '--comment', 'x-pool-' + m['commit'][:8], '-j', 'REJECT', '--reject-with', 'tcp-reset']
    gated = stopped = changed = False
    try:
        run('iptables', '-w', '5', '-I', *rule); gated = True
        deadline = time.monotonic() + 20
        while run('ss', '-Htn', 'state', 'established', 'sport', '=', ':8810'):
            if time.monotonic() > deadline: return {'phase': 'waiting_http'}
            time.sleep(.25)
        assert active()['running_x_jobs'] == 0
        suffix = 'rollback' if rollback else 'deploy'
        for label, db in [('accounts', DB), ('auto', AUTO_DB)]:
            with contextlib.closing(readonly(db)) as src, contextlib.closing(sqlite3.connect(str(folder / (label + '-' + suffix + '.sqlite3')))) as dst:
                src.backup(dst)
        tokens = Path('/var/lib/x-post-automation/tokens')
        shutil.copytree(tokens, folder / ('tokens-' + suffix))
        m['token_hashes_' + suffix] = {p.name: digest(p) for p in tokens.glob('*.json')}
        stopped = True
        run('systemctl', 'stop', 'x-post-automation.service')
        switch(Path(m[next_key + '_release'])); changed = True
        for item in m['external']: copy(Path(item['before' if rollback else 'after']), Path(item['path']))
        run('iptables', '-w', '5', '-D', *rule); gated = False
        run('systemctl', 'start', 'x-post-automation.service'); ready(8810)
        if m['units']['x-auto-post-service.service'] == 'active':
            run('systemctl', 'start', 'x-auto-post-service.service'); ready(18833)
        m['phase'] = 'rolled_back' if rollback else 'complete'
        m['token_hashes_unchanged'] = m['token_hashes_' + suffix] == {p.name: digest(p) for p in tokens.glob('*.json')}
        save(folder, m)
    except BaseException:
        if changed:
            # No execution admissions were restored, so reverting code cannot
            # discard a newer publishing receipt or overwrite rotated tokens.
            run('systemctl', 'stop', 'x-post-automation.service')
            switch(Path(m[current_key + '_release']))
            for item in m['external']: copy(Path(item['after' if rollback else 'before']), Path(item['path']))
        m['phase'] = 'failed_restored'
        save(folder, m)
        raise
    finally:
        if gated: run('iptables', '-w', '5', '-D', *rule)
        if stopped:
            run('systemctl', 'start', 'x-post-automation.service'); ready(8810)
            if m['units']['x-auto-post-service.service'] == 'active':
                run('systemctl', 'start', 'x-auto-post-service.service'); ready(18833)
        if m['phase'] != 'draining': resume(folder, m)
    return {'phase': m['phase'], 'folder': str(folder), 'commit': m['commit'], 'token_hashes_unchanged': m['token_hashes_unchanged']}

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--phase', choices=('prepare', 'drain', 'switch', 'abort', 'rollback'), required=True)
    parser.add_argument('--folder', type=Path)
    args = parser.parse_args()
    if args.phase == 'prepare': result = prepare()
    else:
        folder = args.folder.resolve()
        assert folder.parent == ROOT / 'backups' and '-pool-local-' in folder.name
        m = json.loads((folder / 'manifest.json').read_text())
        if args.phase in ('drain', 'rollback'): result = drain(folder, m, args.phase == 'rollback')
        elif args.phase == 'switch': result = activate(folder, m)
        else:
            assert m['phase'] == 'draining'
            resume(folder, m); m['phase'] = 'complete' if m.get('rollback') else 'prepared'; save(folder, m)
            result = {'phase': m['phase'], 'admissions_restored': True}
    print(json.dumps(result), flush=True)
