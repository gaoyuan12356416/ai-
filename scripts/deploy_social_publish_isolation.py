#!/usr/bin/env python3
"""Staged, drain-before-cutover deployment. Rollback restores code, never ledgers."""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time
import urllib.request

SOURCE = Path(__file__).resolve().parents[1]
ROOT = Path('/mnt/data-disk/social-publish-isolation-20261102')
CURRENT = Path('/opt/x-post-automation/current')
X = ['features/x_posts/service.py', 'features/x_posts/manual_preflight.py',
     'features/x_video_publish/core.py', 'features/x_accounts/oauth_service.py',
     'features/x_auto_posts/core.py', 'scripts/x_auto_post_runner.py',
     'scripts/x_post_manual_runner.py', 'scripts/x_post_schedule_runner.py', 'scripts/x_post_dispatch.py']
EXTERNAL = {
    '/root/drama_material_service': ['features/x_posts/service.py', 'features/x_posts/manual_preflight.py', 'features/x_video_publish/core.py'],
    '/opt/fb-auto-post/current': ['scripts/fb_auto_post_runner.py'],
    '/opt/tt-auto-post/current': ['features/tt_auto_posts/core.py'],
}
TRIGGERS = ['x-post-schedule.timer', 'x-post-schedule-claim.timer', 'x-post-manual.timer',
            'x-auto-post-runner.timer', 'x-auto-post-scheduler.timer', 'x-auto-post-runner.path',
            'x-auto-post-metric.timer', 'x-api-credit-monitor.timer',
            'tt-auto-post-runner.timer', 'tt-auto-post-runner.path', 'tt-auto-post-scheduler.timer']
RUNNERS = ['x-post-schedule.service', 'x-post-manual.service', 'x-auto-post-runner.service', 'tt-auto-post-runner.service']
SIDES = ['x-post-automation.service', 'x-auto-post-service.service', 'tt-auto-post-service.service']
POOL = 'x-video-pool-scheduler.service'
CONFIG = {
    '/etc/x-auto-post.env': {'X_AUTO_POST_RUNNER_LOCK_PATH': '/run/x-auto-post/runner.lock', 'X_AUTO_POST_WORKER_COUNT': '4', 'X_AUTO_POST_MAX_TASKS_PER_WORKER': '20'},
    '/etc/x-post-schedule.env': {'X_POST_SCHEDULE_LOCK_PATH': '/run/x-post-daily/schedule.lock', 'X_POST_MANUAL_LOCK_PATH': '/run/x-post-daily/manual.lock', 'X_POST_PUBLISH_WORKERS': '2', 'X_POST_SCHEDULE_BATCH_WORKERS': '2'},
}
DBS = ['/var/lib/x-post-automation/accounts.sqlite3', '/mnt/data-disk/x-auto-post-publisher/x-auto-post.sqlite3', '/mnt/data-disk/tt-auto-post-publisher/tt-auto-post.sqlite3']


def run(*args, check=True):
    result = subprocess.run(args, capture_output=True, text=True, timeout=60)
    if check and result.returncode:
        raise RuntimeError('command failed: ' + ' '.join(map(str, args[:3])))
    return result.stdout.strip()


def state(unit, prop='ActiveState'):
    return run('systemctl', 'show', unit, '-p', prop, '--value')


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(folder, data):
    target = folder / 'manifest.json'
    target.write_text(json.dumps(data, indent=2) + '\n')
    target.chmod(0o600)


def atomic_copy(source, target):
    target = Path(target)
    stat = target.stat() if target.exists() else target.parent.stat()
    temporary = target.with_name(target.name + '.isolation-next')
    shutil.copy2(source, temporary)
    os.chown(temporary, stat.st_uid, stat.st_gid)
    temporary.chmod((stat.st_mode & 0o777) if target.exists() else 0o644)
    temporary.replace(target)


def ready(port, attempts=30):
    for _ in range(attempts):
        try:
            with urllib.request.urlopen('http://127.0.0.1:%s/health' % port, timeout=2) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(1)
    raise RuntimeError('health failed: %s' % port)


def protected_facts():
    with contextlib.closing(sqlite3.connect('file:' + DBS[0] + '?mode=ro', uri=True)) as conn:
        rows = conn.execute("SELECT id,queue_id,status,attempt_count,x_post_id,x_post_url,unknown_outcome,published_at FROM x_post_publish_log WHERE status='published' OR unknown_outcome=1 OR status IN ('post_creating','repost_creating') ORDER BY id").fetchall()
        return {'count': len(rows), 'sha256': hashlib.sha256(json.dumps(rows).encode()).hexdigest()}


def restore_triggers(manifest):
    if manifest['pool_state'] == 'active':
        run('systemctl', 'start', POOL)
    for unit, previous in manifest['triggers'].items():
        if previous == 'active':
            run('systemctl', 'start', unit)


def prepare():
    assert not run('git', '-C', str(SOURCE), 'status', '--porcelain')
    commit = run('git', '-C', str(SOURCE), 'rev-parse', 'HEAD')
    assert run('git', '-C', str(SOURCE), 'branch', '-r', '--contains', commit)
    expected = json.loads((SOURCE / 'doc/20261102.social-publish-isolation/runtime-baseline.json').read_text())
    for path, sha in expected.items():
        assert digest(path) == sha, 'runtime drift: ' + path
    folder = ROOT / ('backup-' + time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()))
    folder.mkdir(parents=True, mode=0o700)
    release = ROOT / ('x-' + commit)
    assert not release.exists()
    shutil.copytree(CURRENT.resolve(), release, symlinks=True, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    release.chmod(CURRENT.resolve().stat().st_mode & 0o777)
    for relative in X:
        atomic_copy(SOURCE / relative, release / relative)
        compile((release / relative).read_text(), relative, 'exec')
    manifest = {'commit': commit, 'previous_release': str(CURRENT.resolve()), 'new_release': str(release),
                'triggers': {u: state(u) for u in TRIGGERS}, 'pool_state': state(POOL),
                'sides': {u: state(u) for u in SIDES}, 'files': [], 'paused': [], 'status': 'preparing'}
    for root, relatives in EXTERNAL.items():
        for relative in relatives:
            target = Path(root) / relative
            record = {'target': str(target), 'source': relative, 'existed': target.exists(), 'backup': 'file-%s' % len(manifest['files'])}
            if target.exists():
                shutil.copy2(target, folder / record['backup'])
            manifest['files'].append(record)
    for target in CONFIG:
        record = {'target': target, 'backup': 'file-%s' % len(manifest['files']), 'existed': True, 'config': True}
        shutil.copy2(target, folder / record['backup'])
        manifest['files'].append(record)
    save(folder, manifest)
    run('systemctl', 'stop', *[u for u in TRIGGERS if manifest['triggers'][u] == 'active'])
    if manifest['pool_state'] == 'active':
        run('systemctl', 'stop', POOL)
    # Pause callers only; the independent sidecars finish their current API
    # requests and commit receipts. No uploader or sidecar is killed here.
    for unit in RUNNERS:
        if state(unit) in {'active', 'activating'}:
            run('systemctl', 'kill', '--kill-who=main', '-s', 'SIGSTOP', unit)
            manifest['paused'].append(unit)
    manifest['status'] = 'draining'
    save(folder, manifest)
    print(json.dumps({'status': 'draining', 'backup': str(folder)}))


def apply(folder, rollback=False):
    manifest = json.loads((folder / 'manifest.json').read_text())
    if rollback:
        assert str(CURRENT.resolve()) == manifest['new_release'], 'another release is active'
        assert all(state(u) not in {'active', 'activating'} for u in RUNNERS), 'stop triggers and drain runners first'
    else:
        assert manifest['status'] == 'draining'
    # X has one permanent YouTube-share worker in addition to its HTTP main.
    idle_tasks = {'x-post-automation.service': '2', 'x-auto-post-service.service': '1', 'tt-auto-post-service.service': '1'}
    busy = {u: state(u, 'TasksCurrent') for u in SIDES if state(u) == 'active' and state(u, 'TasksCurrent') != idle_tasks[u]}
    if busy:
        print(json.dumps({'status': 'draining', 'sidecar_tasks': busy}))
        return
    shares = Path('/mnt/data-disk/x-post-automation/youtube-shares/ledger.sqlite3')
    if shares.exists():
        with contextlib.closing(sqlite3.connect(shares.as_uri() + '?mode=ro', uri=True)) as conn:
            if conn.execute("SELECT COUNT(*) FROM youtube_share_item WHERE status IN ('queued','publishing')").fetchone()[0]:
                print(json.dumps({'status': 'draining', 'youtube_share_work': True}))
                return
    for unit in manifest['paused']:
        # SIGTERM plus SIGCONT makes the paused caller exit. The sidecar has
        # already returned its receipt, so this cannot interrupt an X write.
        run('systemctl', 'kill', '--kill-who=main', '-s', 'SIGTERM', unit, check=False)
        run('systemctl', 'kill', '--kill-who=main', '-s', 'SIGCONT', unit, check=False)
        run('systemctl', 'stop', unit)
    before = protected_facts()
    for unit in SIDES:
        run('systemctl', 'stop', unit)
    if not rollback:
        for index, path in enumerate(DBS):
            if (folder / ('ledger-%s.sqlite3' % index)).exists():
                continue
            with contextlib.closing(sqlite3.connect('file:' + path + '?mode=ro', uri=True)) as conn, contextlib.closing(sqlite3.connect(folder / ('ledger-%s.sqlite3' % index))) as target:
                conn.backup(target)
                assert target.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
        manifest['protected_before'] = before
        save(folder, manifest)
    for record in manifest['files']:
        target = Path(record['target'])
        if rollback:
            if record['existed']:
                atomic_copy(folder / record['backup'], target)
            elif target.exists():
                target.unlink()
        elif record.get('config'):
            changes = CONFIG[str(target)]
            lines = target.read_text().splitlines()
            lines = [line for line in lines if line.split('=', 1)[0] not in changes]
            temporary = folder / 'next.env'
            temporary.write_text('\n'.join(lines + [k + '=' + v for k, v in changes.items()]) + '\n')
            atomic_copy(temporary, target)
            temporary.unlink()
        else:
            atomic_copy(SOURCE / record['source'], target)
    link = CURRENT.with_name('social-isolation-next')
    link.symlink_to(manifest['previous_release'] if rollback else manifest['new_release'])
    link.replace(CURRENT)
    for unit in SIDES:
        if manifest['sides'][unit] == 'active':
            run('systemctl', 'start', unit)
    ready(8810); ready(18833)
    run('systemctl', 'restart', 'drama-material-api.service')
    assert protected_facts() == before, 'protected publication facts changed during cutover'
    manifest['protected_after'] = protected_facts()
    manifest['status'] = 'cutover_complete'
    save(folder, manifest)
    restore_triggers(manifest)
    # TT readiness includes active timers/path, which are intentionally paused
    # until all source/receipt checks have passed.
    ready(18831, attempts=90)  # automation probe caches the paused state for 60s
    manifest['status'] = 'rolled_back' if rollback else 'complete'
    save(folder, manifest)
    print(json.dumps({'status': manifest['status'], 'backup': str(folder), 'commit': manifest['commit'], 'protected': before}))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=['prepare', 'apply', 'rollback'])
    parser.add_argument('--backup', type=Path)
    args = parser.parse_args()
    assert run('findmnt', '-no', 'UUID', '/mnt/data-disk') == '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    assert shutil.disk_usage('/mnt/data-disk').free > 3 * 1024**3
    ROOT.mkdir(parents=True, exist_ok=True)
    ROOT.chmod(0o755)
    os.umask(0o077)
    if args.phase == 'prepare':
        prepare()
    else:
        assert args.backup and args.backup.resolve().is_relative_to(ROOT)
        apply(args.backup, rollback=args.phase == 'rollback')


if __name__ == '__main__':
    main()
