#!/usr/bin/env python3
"""Scoped composite deployment and optional no-post pool revalidation."""
import argparse
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time
import urllib.request

BASE = Path('/mnt/data-disk/x-post-automation')
CURRENT = Path('/opt/x-post-automation/current')
MAIN = Path('/root/drama_material_service')
PUBLIC = Path('/usr/share/nginx/html/x-post-material-pool.html')
CODE = ['app.py', 'features/x_posts/selector.py', 'features/x_posts/service.py',
        'static/x-post-material-pool.html']
TESTS = ['scripts/test_x_post_material_pool_selector.py',
         'scripts/test_x_post_multi_schedule_store.py']
EXPECTED = {
    'app.py': '38e3336bf9584977ebe9d52614897b2bb9b909e0a76f5b763ef22ae1304b7222',
    'features/x_posts/selector.py': '8a0353a4ed116fb36dc3d7e9511e3b461c8de8b4b21467f543f3309577b60adf',
    'features/x_posts/service.py': 'acf7926784d6576a13cbd5986edb2c50b15175ef7e435cbb0081ed9c8da0fc05',
}
MAIN_HTML = '3da5586970738ba5106ef75de9a3e860b02c53675d540e2c6ed8b465bddecd35'
RUNTIME_HTML = '2f4d5007c28963687af8129aaa899ff2c6025dc31c9e88fd44671514c4648acd'
TIMERS = ['x-post-schedule.timer', 'x-post-schedule-claim.timer',
          'x-post-manual.timer', 'x-auto-post-runner.timer',
          'x-auto-post-scheduler.timer', 'x-auto-post-metric.timer',
          'x-account-operating-stats.timer', 'x-auto-post-runner.path']
RUNNERS = ['x-post-schedule.service', 'x-post-schedule-claim.service',
           'x-post-manual.service', 'x-auto-post-runner.service',
           'x-auto-post-scheduler.service', 'x-auto-post-metric.service',
           'x-account-operating-stats.service']
DBS = {'post': Path('/var/lib/x-post-automation/accounts.sqlite3'),
       'auto': Path('/mnt/data-disk/x-auto-post-publisher/x-auto-post.sqlite3')}
SERVICES = ['x-post-automation.service', 'x-auto-post-service.service',
            'drama-material-api.service']
RESCUE = 'x-pool-language-restore-20261012'


def run(args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, text=True, **kwargs)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def state(unit):
    return subprocess.run(['systemctl', 'is-active', unit], capture_output=True,
                          text=True).stdout.strip()


def save(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temporary.chmod(0o600)
    os.replace(temporary, path)


def progress(backup, stage, **values):
    value = dict(stage=stage, at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(), **values)
    save(backup / 'progress.json', value)
    print(json.dumps(value, ensure_ascii=False), flush=True)


def ro(path):
    connection = sqlite3.connect('file:' + str(path) + '?mode=ro', uri=True)
    connection.execute('PRAGMA query_only=ON')
    return connection


def ledger_fingerprint():
    result = {}
    for label, path in DBS.items():
        with ro(path) as connection:
            for (table,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
                if label == 'post' and table not in {
                    'x_post_queue', 'x_post_publish_log', 'x_post_repost_ledger',
                    'x_post_schedule_run', 'x_post_daily_run', 'x_post_manual_run',
                }:
                    continue
                if label == 'auto' and table not in {'x_auto_run', 'x_auto_task'}:
                    continue
                digest = hashlib.sha256()
                count = 0
                for row in connection.execute('SELECT * FROM "' + table + '" ORDER BY id'):
                    digest.update(json.dumps(row, ensure_ascii=True, separators=(',', ':')).encode())
                    count += 1
                result[label + '.' + table] = {'count': count, 'sha256': digest.hexdigest()}
    return result


def token_inventory():
    return {p.name: {'sha256': sha(p), 'uid': p.stat().st_uid, 'gid': p.stat().st_gid,
                     'mode': oct(p.stat().st_mode & 0o777)}
            for p in Path('/var/lib/x-post-automation/tokens').glob('*.json')}


def atomic_copy(source, target):
    temp = target.with_name(target.name + '.x-pool-language-tmp')
    shutil.copy2(source, temp)
    os.replace(temp, target)


def switch(target):
    temp = CURRENT.with_name('current.x-pool-language-tmp')
    temp.symlink_to(target, target_is_directory=True)
    os.replace(temp, CURRENT)


def restore(backup):
    manifest = json.loads((backup / 'manifest.json').read_text())
    for unit, old in manifest['timers'].items():
        if old == 'active':
            run(['systemctl', 'start', unit])
    after = {unit: state(unit) for unit in manifest['timers']}
    result = {'before': manifest['timers'], 'after': after,
              'restored': after == manifest['timers']}
    save(backup / 'restored.json', result)
    subprocess.run(['systemctl', 'stop', RESCUE + '.timer'], capture_output=True)
    return result


def ready():
    urls = ['http://127.0.0.1:8810/health', 'http://127.0.0.1:18833/health',
            'http://127.0.0.1:8787/api/ui/topbar']
    for url in urls:
        for attempt in range(30):
            try:
                with urllib.request.urlopen(url, timeout=3) as response:
                    if response.status == 200:
                        break
            except Exception:
                pass
            time.sleep(1)
        else:
            raise RuntimeError('service health failed: ' + url)
    return {'http_200': urls}


def install(args):
    source = Path(args.source).resolve(strict=True)
    backup = Path(args.backup).resolve(strict=True)
    if not backup.is_relative_to(BASE / 'maintenance'):
        raise RuntimeError('backup must be on the X maintenance data disk')
    if (backup / 'manifest.json').exists():
        raise RuntimeError('maintenance already started; inspect receipts instead of replaying')
    old_release = CURRENT.resolve(strict=True)
    for name, expected in EXPECTED.items():
        assert sha(old_release / name) == sha(MAIN / name) == expected, name
    assert sha(MAIN / CODE[-1]) == sha(PUBLIC) == MAIN_HTML
    assert sha(old_release / CODE[-1]) == RUNTIME_HTML
    stage = BASE / 'releases' / (args.commit + '-pool-drama-language')
    assert not stage.exists()
    manifest = {'commit': args.commit, 'previous_release': str(old_release),
                'release': str(stage), 'timers': {unit: state(unit) for unit in TIMERS},
                'material_ids': args.material_id, 'files': CODE}
    save(backup / 'manifest.json', manifest)
    progress(backup, 'draining_publishers')
    run(['systemd-run', '--unit=' + RESCUE, '--on-active=60m', '/usr/bin/python3',
         str(Path(__file__).resolve()), '--restore', str(backup)])
    deployed = False
    try:
        for unit, old in manifest['timers'].items():
            if old == 'active':
                run(['systemctl', 'stop', unit])
        deadline = time.monotonic() + 900
        while any(state(unit) in {'active', 'activating', 'deactivating'} for unit in RUNNERS):
            if time.monotonic() > deadline:
                raise RuntimeError('publisher drain timeout; no code switched')
            time.sleep(5)
        with open('/run/x-post-daily/runner.lock', 'a+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            for port in (8810, 18833):
                active = run(['ss', '-Htn', 'state', 'established', 'sport = :%d' % port]).stdout.strip()
                if active:
                    raise RuntimeError('active sidecar request; no code switched')
            for label, path in DBS.items():
                with ro(path) as original, sqlite3.connect(backup / (label + '-before.sqlite3')) as copy:
                    original.backup(copy)
                (backup / (label + '-before.sqlite3')).chmod(0o600)
            before = ledger_fingerprint()
            tokens = token_inventory()
            save(backup / 'ledger-before.json', before)
            save(backup / 'tokens-before.json', tokens)
            for name in CODE:
                saved = backup / 'main' / name
                saved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(MAIN / name, saved)
            shutil.copy2(PUBLIC, backup / 'public-pool.html')
            progress(backup, 'staging_verified_composite')
            shutil.copytree(old_release, stage, symlinks=True,
                            ignore=shutil.ignore_patterns('.git', '__pycache__', '*.pyc'))
            for name in CODE + TESTS:
                atomic_copy(source / name, stage / name)
            tests = run(['/usr/bin/python3', '-m', 'unittest',
                         'scripts.test_x_post_material_pool_selector',
                         'scripts.test_x_post_material_pool',
                         'scripts.test_x_post_multi_schedule_store',
                         'scripts.test_x_post_manual_runner',
                         'scripts.test_x_post_schedule_runner',
                         'scripts.test_x_post_auto_template_bridge'], cwd=stage, timeout=180)
            (backup / 'staged-tests.txt').write_text(tests.stdout + tests.stderr)
            run(['/usr/bin/python3', '-m', 'py_compile', *[str(stage / n) for n in CODE if n.endswith('.py')]])
            switch(stage)
            deployed = True
            for name in CODE:
                atomic_copy(source / name, MAIN / name)
            atomic_copy(source / CODE[-1], PUBLIC)
            for unit in SERVICES:
                run(['systemctl', 'restart', unit], timeout=90)
            health = ready()
            hashes = {name: sha(stage / name) for name in CODE}
            assert all(sha(MAIN / name) == hashes[name] for name in CODE)
            assert sha(PUBLIC) == hashes[CODE[-1]]
            save(backup / 'deployed.json', dict(commit=args.commit, release=str(stage), hashes=hashes, health=health))
        progress(backup, 'revalidating_materials', total=len(args.material_id), completed=0)
        results = []
        revalidation = backup / 'revalidation'
        revalidation.mkdir(mode=0o700)
        for material_id in args.material_id:
            assert material_id.isdecimal() and int(material_id) > 0
            report = revalidation / (material_id + '.json')
            proc = subprocess.run(['/usr/bin/python3', str(stage / 'scripts/x_post_media_repair_backfill.py'),
                                   '--material-id', material_id, '--report-path', str(report)],
                                  capture_output=True, text=True, timeout=1200, cwd=stage)
            if not report.exists():
                raise RuntimeError('revalidation receipt missing for ' + material_id)
            item = json.loads(report.read_text())
            results.extend(item.get('results', []))
            progress(backup, 'revalidating_materials', total=len(args.material_id),
                     completed=len(results), latest=item.get('status'),
                     ready=sum(r.get('status', '').endswith('_ready') for r in results))
        after = ledger_fingerprint()
        save(backup / 'verification.json', {
            'commit': args.commit, 'results': results,
            'ledger_unchanged': before == after, 'ledger_after': after,
            'token_inventory_unchanged': tokens == token_inventory(),
            'health': ready(), 'backup': str(backup), 'release': str(stage),
        })
        assert before == after, 'publication facts changed during maintenance'
        progress(backup, 'completed', total=len(results),
                 ready=sum(r.get('status', '').endswith('_ready') for r in results))
    except Exception as exc:
        save(backup / 'failure.json', {'error_type': type(exc).__name__, 'message': str(exc)[:600],
                                     'code_switched': deployed})
        # A revalidation problem never restores publication data or undoes valid checks.
        if deployed and not (backup / 'deployed.json').exists():
            switch(old_release)
            for name in CODE:
                atomic_copy(backup / 'main' / name, MAIN / name)
            atomic_copy(backup / 'public-pool.html', PUBLIC)
            for unit in SERVICES:
                run(['systemctl', 'restart', unit], timeout=90)
            ready()
        raise
    finally:
        result = restore(backup)
        progress(backup, 'timers_restored', restored=result['restored'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source')
    parser.add_argument('--backup')
    parser.add_argument('--commit')
    parser.add_argument('--material-id', action='append', default=[])
    parser.add_argument('--restore')
    args = parser.parse_args()
    if args.restore:
        print(json.dumps(restore(Path(args.restore))))
    else:
        assert args.source and args.backup and args.commit and len(args.commit) == 40
        install(args)
