#!/usr/bin/env python3
"""Exact GitHub checkout overlay; preserve the deployed composite and all ledgers."""
import argparse
import contextlib
import fcntl
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
MANIFEST = json.loads((SOURCE / 'deploy/x-random-template-manifest.json').read_text())
CATALOG = 'b5df776a88bdfa961e60f60d6076c6915f37c5fd5ed8ade973dc4be7205eea6c'
TIMERS = ['x-post-manual.timer', 'x-post-schedule.timer', 'x-post-schedule-claim.timer',
          'x-auto-post-runner.timer', 'x-auto-post-scheduler.timer', 'x-auto-post-metric.timer']
SERVICES = ['x-post-automation.service', 'x-auto-post-service.service', 'drama-material-api.service']
DB = Path('/var/lib/x-post-automation/accounts.sqlite3')


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def active(unit):
    return subprocess.run(['systemctl', 'is-active', '--quiet', unit]).returncode == 0


def digest(path):
    return hashlib.sha256(Path(path).read_bytes().replace(b'\r\n', b'\n')).hexdigest()


def copy(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def switch(link, target):
    temporary = link.with_name(link.name + '.x-random-next')
    assert not temporary.exists() and not temporary.is_symlink()
    temporary.symlink_to(target)
    os.replace(temporary, link)


def counts(db):
    with contextlib.closing(sqlite3.connect('file:' + str(db) + '?mode=ro', uri=True)) as conn:
        conn.execute('PRAGMA query_only=ON')
        return {table: conn.execute('SELECT count(*) FROM ' + table).fetchone()[0]
                for table in ('x_post_queue', 'x_post_publish_log', 'x_post_schedule_run', 'x_post_manual_run')}


def snapshot(target):
    with contextlib.closing(sqlite3.connect('file:' + str(DB) + '?mode=ro', uri=True)) as source:
        with contextlib.closing(sqlite3.connect(target)) as dest:
            source.backup(dest)
    os.chmod(target, 0o600)


def backup_file(backup, path):
    if path.exists():
        copy(path, backup / 'files' / str(path).lstrip('/'))


def receipt(backup, value):
    (backup / 'deployment.json').write_text(json.dumps(value, indent=2) + '\n')
    lines = []
    for path in sorted(backup.rglob('*')):
        if path.is_file() and path.name != 'SHA256SUMS':
            lines.append(hashlib.sha256(path.read_bytes()).hexdigest() + '  ' + str(path.relative_to(backup)))
    (backup / 'SHA256SUMS').write_text('\n'.join(lines) + '\n')


def deploy(host):
    commit = run('git', '-C', str(SOURCE), 'rev-parse', 'HEAD')
    assert not run('git', '-C', str(SOURCE), 'status', '--porcelain'), 'checkout is dirty'
    root = Path('/data/x-post-media-repair' if host == 'gpu' else '/mnt/data-disk/x-post-automation')
    link = Path('/data/x-post-media-repair/current' if host == 'gpu' else '/opt/x-post-automation/current')
    old = link.resolve(strict=True)
    stamp = time.strftime('%Y%m%dT%H%M%S', time.gmtime()) + '-x-random-' + commit[:12]
    backup = root / 'backups' / stamp
    backup.mkdir(parents=True, mode=0o700)
    release = root / 'releases' / (commit + '-x-random')
    assert not release.exists()
    if host == 'gpu':
        assert digest(old / 'features/x_posts/media_repair.py') == MANIFEST['gpu_before'], 'GPU source drift'
        files = MANIFEST['gpu']
        units = ['x-post-media-repair.service']
        dropin = Path('/etc/systemd/system/x-post-media-repair.service.d/60-x-random-template.conf')
    else:
        for name in MANIFEST['cpu_existing']:
            assert digest(old / name) == MANIFEST['before'][name], 'X source drift: ' + name
        for name in MANIFEST['main']:
            if name in MANIFEST['before']:
                assert digest(Path('/root/drama_material_service') / name) == MANIFEST['before'][name], 'main source drift: ' + name
        files = MANIFEST['cpu_existing'] + MANIFEST['cpu_new']
        units = SERVICES
        dropin = Path('/etc/systemd/system/x-post-automation.service.d/60-x-random-template.conf')
    assert all(active(unit) for unit in units), 'service is not healthy before deploy'
    assert not dropin.exists(), 'deployment drop-in already exists'
    shutil.copytree(old, release, symlinks=True, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    for name in files:
        copy(SOURCE / name, release / name)
    for name in files:
        if name.endswith('.py'):
            subprocess.check_call([sys.executable, '-m', 'py_compile', str(release / name)])
    state = {'host': host, 'commit': commit, 'previous_release': str(old), 'release': str(release),
             'link': str(link), 'dropin': str(dropin), 'timers': {t: active(t) for t in TIMERS} if host == 'cpu' else {},
             'services': units, 'backed_up_files': [], 'new_main_files': []}
    if host == 'cpu':
        for name in MANIFEST['main']:
            target = Path('/root/drama_material_service') / name
            if target.exists():
                backup_file(backup, target); state['backed_up_files'].append(str(target))
            else:
                state['new_main_files'].append(str(target))
        for name in MANIFEST['static']:
            for parent in ('/root/drama_material_service/static', '/usr/share/nginx/html'):
                target = Path(parent) / name
                backup_file(backup, target); state['backed_up_files'].append(str(target))
        # Rehearse only against an online backup, preserving its old-column rows.
        snapshot(backup / 'before.sqlite3')
        copy(backup / 'before.sqlite3', backup / 'rehearsal.sqlite3')
        sys.path.insert(0, str(release))
        from features.x_posts.service import ensure_storage
        before = counts(backup / 'rehearsal.sqlite3')
        ensure_storage(backup / 'rehearsal.sqlite3'); ensure_storage(backup / 'rehearsal.sqlite3')
        assert counts(backup / 'rehearsal.sqlite3') == before
        with contextlib.closing(sqlite3.connect(backup / 'rehearsal.sqlite3')) as conn:
            assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
            for table in ('x_post_queue','x_post_schedule_config','x_post_schedule_run','x_post_manual_run','x_post_schedule_random_plan'):
                assert conn.execute('SELECT count(*) FROM ' + table + ' WHERE random_template_enabled<>0').fetchone()[0] == 0
        state['migration_rehearsal'] = {'integrity': 'ok', 'counts_unchanged': before, 'all_flags_false': True}
        for timer, was_active in state['timers'].items():
            if was_active: subprocess.check_call(['systemctl', 'stop', timer])
    receipt(backup, state)
    lock = None
    switched = False
    try:
        if host == 'cpu':
            lock = open('/run/x-post-daily/runner.lock', 'r+')
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # No in-flight publishing or planning oneshot may be interrupted.
            assert not any(active(t.replace('.timer','.service')) for t in TIMERS), 'X oneshot is active; retry after drain'
            state['before_counts'] = counts(DB)
        else:
            pid = run('systemctl','show',units[0],'--property=MainPID','--value')
            assert len(list(Path('/proc/' + pid + '/task').iterdir())) == 1, 'GPU request active; retry after drain'
        subprocess.check_call(['systemctl', 'stop', *units])
        if host == 'cpu':
            snapshot(backup / 'cutover.sqlite3')
            ensure_storage(DB)
            assert counts(DB) == state['before_counts'], 'ledger count changed during migration'
            for name in MANIFEST['main']:
                copy(SOURCE / name, Path('/root/drama_material_service') / name)
            for name in MANIFEST['static']:
                for parent in ('/root/drama_material_service/static', '/usr/share/nginx/html'):
                    copy(SOURCE / 'static' / name, Path(parent) / name)
        dropin.parent.mkdir(parents=True, exist_ok=True)
        content = '[Service]\nEnvironment=X_POST_RANDOM_TEMPLATE_MANIFEST_SHA256=' + CATALOG + '\n'
        if host == 'gpu':
            content += ('Environment=X_POST_RANDOM_TEMPLATE_ENABLED=1\n'
                        'Environment=X_POST_RANDOM_TEMPLATE_ASSET_ROOT=/data/random-overlay-gpu/assets/catalog-b5df776a88bd\n'
                        'Environment=RANDOM_GPU_ASSET_CACHE_ROOT=/data/random-overlay-gpu/asset-cache-v1\n')
        dropin.write_text(content)
        switch(link, release); switched = True
        subprocess.check_call(['systemctl','daemon-reload'])
        subprocess.check_call(['systemctl','start',*units])
        assert all(active(unit) for unit in units), 'post-deploy service failed'
        state['completed'] = True
        if host == 'cpu': state['after_counts'] = counts(DB)
        receipt(backup, state)
        print(json.dumps({'backup':str(backup), **state}, indent=2), flush=True)
    except Exception:
        if switched:
            # New user-enabled tasks require an explicit hold, never silent code fallback.
            print('Deployment failed after cutover; use guarded rollback with backup ' + str(backup), flush=True)
        else:
            subprocess.run(['systemctl','start',*units])
        raise
    finally:
        if lock: lock.close()
        for timer, was_active in state['timers'].items():
            if was_active: subprocess.check_call(['systemctl','start',timer])


def rollback(backup):
    state = json.loads((backup / 'deployment.json').read_text())
    if state['host'] == 'cpu':
        with contextlib.closing(sqlite3.connect('file:' + str(DB) + '?mode=ro', uri=True)) as conn:
            queries = ["SELECT count(*) FROM x_post_queue WHERE random_template_enabled=1 AND status NOT IN ('published','cancelled')",
                       "SELECT count(*) FROM x_post_manual_run WHERE random_template_enabled=1 AND status IN ('pending','preparing','queued','running')",
                       "SELECT count(*) FROM x_post_schedule_config WHERE random_template_enabled=1",
                       "SELECT count(*) FROM x_post_schedule_run WHERE random_template_enabled=1 AND status IN ('claimed','queued','running')"]
            queries.append("SELECT count(*) FROM x_post_schedule_random_plan WHERE random_template_enabled=1 AND run_date>=date('now','+8 hours')")
            enabled=any(conn.execute(q).fetchone()[0] for q in queries)
        auto=Path('/mnt/data-disk/x-auto-post-publisher/x-auto-post.sqlite3')
        assert auto.is_file(), 'Auto database unavailable; rollback cannot establish safe scope'
        with contextlib.closing(sqlite3.connect('file:' + str(auto) + '?mode=ro', uri=True)) as conn:
            enabled=enabled or bool(conn.execute("SELECT count(*) FROM x_auto_template_version WHERE json_extract(config_json,'$.random_template_enabled')=1").fetchone()[0])
            if enabled:
                for timer in state['timers']: subprocess.run(['systemctl','stop',timer])
                subprocess.run(['systemctl','stop','x-auto-post-service.service','x-post-automation.service'])
                raise SystemExit('Rollback blocked: random-enabled work/config exists. X publishers are held; reconcile and pause exact work first. Database retained.')
    subprocess.check_call(['systemctl','stop',*state['services']])
    for target in state['backed_up_files']:
        copy(backup / 'files' / target.lstrip('/'), Path(target))
    # Additive modules and database columns remain, but old callers cannot run enabled work.
    Path(state['dropin']).unlink(missing_ok=True)
    switch(Path(state['link']), Path(state['previous_release']))
    subprocess.check_call(['systemctl','daemon-reload'])
    subprocess.check_call(['systemctl','start',*state['services']])
    print('Code rolled back; databases, receipts and publishing ledgers retained.')


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--host', choices=('cpu','gpu'))
    parser.add_argument('--rollback', type=Path)
    args=parser.parse_args()
    if args.rollback: rollback(args.rollback)
    elif args.host: deploy(args.host)
    else: parser.error('--host or --rollback is required')
