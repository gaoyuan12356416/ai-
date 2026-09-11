#!/usr/bin/env python3
"""Deploy channel authorization and read caching, preserving unrelated live app code."""
import argparse
import ast
import fcntl
import hashlib
import json
import os
from pathlib import Path
import py_compile
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.request

ROOT = Path('/root/drama_material_service')
PUBLIC = Path('/usr/share/nginx/html')
BASE = Path('/mnt/data-disk/deploy/youtube-auto-publish')
SQL = Path('/etc/youtube-auto-publish/material-source.sql')
SQL_SHA = 'c301d02c6bfad8fa62b86cc2befe83176a38616c4d42c9e385bfb78e0e838e01'
EXPECTED = {'features/youtube_auto_publish/service.py': '84ffe31ac24d8586a2c3eab92838627281e8303d15acf8bd260e17013874ef5b', 'features/youtube_auto_publish/runtime.py': '36dafb87fb292b8cb99fb945ec8ecf7205eb4516553565db90288d861958d87d', 'features/youtube_auto_publish/source.py': '0f99b9b80eec848a11cc5f364cdca8553fa2d09a680e9c98a2dc06aace2859bd', 'static/youtube-publish.html': '007581c435e6811d08ec8135efe83e9524109528bde8d5380ebc23272f12c9db', 'static/youtube-publish.js': '227f1e8655a148aa23cbf678ea84c33450dbd04cf2d4f45c6185cbeecd80925c', 'static/youtube-publish.css': '45dc9a1074f340366e01322e1c564648f36457cac4e4f5c6dcb40d10afb421fc', 'features/youtube_auto_publish/cache.py': None, 'features/youtube_auto_publish/channels.py': None}

API = 'drama-material-api.service'
LEGACY = 'drama-youtube-publish-worker.service'
WORKER = 'youtube-auto-publish-worker.service'
UNITS = [API, LEGACY, WORKER, 'drama-youtube-unified-writer.service']
DB = ROOT / 'data/drama_material_jobs.sqlite3'
DRAIN_GENERATOR = False


def run(*args):
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT).strip()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest() if Path(path).is_file() else None


def install(source, target, mode):
    target = Path(target)
    temp = target.with_name(target.name + '.youtube-channel-cache-new')
    shutil.copyfile(source, temp)
    os.chmod(temp, mode)
    os.replace(temp, target)


def healthy():
    for _ in range(15):
        try:
            with urllib.request.urlopen('http://127.0.0.1:8787/api/auth/status', timeout=2) as response:
                if response.status == 200 and isinstance(json.load(response), dict):
                    return
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError('API health check failed')


def idle_snapshot(require_idle=True):
    with sqlite3.connect('file:' + str(DB) + '?mode=ro', uri=True) as db:
        active = db.execute("SELECT count(*) FROM drama_youtube_publish WHERE status NOT IN ('published','failed','unknown','partial_failed','cancelled') OR comment_status='publishing'").fetchone()[0]
        preparations = db.execute("SELECT count(*) FROM youtube_auto_preparation WHERE state IN ('queued_generation','generating','enqueue_pending') OR lease_until>strftime('%s','now')").fetchone()[0]
        if require_idle and (active or (preparations and not DRAIN_GENERATOR)):
            raise RuntimeError('Publishing or generation is active; leave running work untouched')
        return {'publishing': db.execute('SELECT workflow,status,comment_status,count(*) FROM drama_youtube_publish GROUP BY workflow,status,comment_status').fetchall(),
                'preparations': db.execute('SELECT state,count(*) FROM youtube_auto_preparation GROUP BY state').fetchall(),
                'links': db.execute('SELECT count(*) FROM drama_material_short_link').fetchone()[0]}


def stop():
    run('systemctl', 'stop', *([LEGACY, API] if DRAIN_GENERATOR else [LEGACY, WORKER, API]))


def start():
    run('systemctl', 'start', API)
    healthy()
    run('systemctl', 'start', *([LEGACY] if DRAIN_GENERATOR else [LEGACY, WORKER]))
    for unit in UNITS:
        if run('systemctl', 'is-active', unit) != 'active':
            raise RuntimeError('Inactive service: ' + unit)


def restore(backup, manifest):
    for row in manifest['files']:
        if row['sha256'] is not None and sha(backup / 'files' / row['target'].lstrip('/')) != row['sha256']:
            raise RuntimeError('Backup integrity mismatch')
    for row in manifest['files']:
        target = Path(row['target'])
        if row['sha256'] is None:
            if target.exists():
                target.unlink()
        else:
            install(backup / 'files' / row['target'].lstrip('/'), target, row['mode'])


def main():
    global DRAIN_GENERATOR
    parser = argparse.ArgumentParser()
    parser.add_argument('--commit')
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--drain-generator', action='store_true', help='Keep current generation running, then gracefully load installed worker code')
    parser.add_argument('--rollback', type=Path)
    args = parser.parse_args()
    DRAIN_GENERATOR = args.drain_generator
    worker_pid = int(run('systemctl','show',WORKER,'-p','MainPID','--value'))
    if DRAIN_GENERATOR:
        if args.rollback:raise RuntimeError('Drain mode is only for forward rolling deployment')
        if run('systemctl','show',WORKER,'-p','Restart','--value')!='always' or worker_pid<=0 or run('systemctl','show',WORKER,'-p','WatchdogUSec','--value')!='0':
            raise RuntimeError('Automatic worker restart contract missing')
        for prop in ('Requires','BindsTo','PartOf'):
            if API in run('systemctl','show',WORKER,'-p',prop,'--value').split():
                raise RuntimeError('Worker depends on API lifecycle; cannot preserve generation')

    if run('findmnt', '-n', '-o', 'UUID', '/mnt/data-disk') != '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':
        raise RuntimeError('Data mount mismatch')
    with (BASE / 'channel-cache-deploy.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        targets = [ROOT / name for name in EXPECTED] + [ROOT / 'app.py']
        targets += [PUBLIC / Path(name).name for name in EXPECTED if name.startswith('static/')]
        if args.rollback:
            backup = args.rollback.resolve()
            if (BASE / 'backups').resolve() not in backup.parents:
                raise RuntimeError('Invalid backup path')
            manifest = json.loads((backup / 'manifest.json').read_text())
            if manifest.get('kind') != 'youtube-channel-cache' or {r['target'] for r in manifest['files']} != {str(t) for t in targets}:
                raise RuntimeError('Invalid backup manifest')
            for row in manifest['files']:
                if sha(row['target']) != row['installed_sha256']:
                    raise RuntimeError('Newer live change exists; do not overwrite it')
            idle_snapshot()
            try:
                stop()
                idle_snapshot()
                restore(backup, manifest)
            finally:
                start()
            print(json.dumps({'rollback': str(backup), 'database': 'retained', 'channel_audit': 'retained', 'material_sql': 'retained'}))
            return
        stage = Path(__file__).resolve().parents[1]
        if not args.commit or len(args.commit) != 40 or (stage / '.github-verified-commit').read_text().strip() != args.commit:
            raise RuntimeError('Exact GitHub-fetched commit required')
        # Only replace the reviewed handler, never the production composite app.
        def handler(content):
            tree=ast.parse(content)
            for cls in tree.body:
                if isinstance(cls,ast.ClassDef) and cls.name=='DramaMaterialHandler':
                    for node in cls.body:
                        if isinstance(node,ast.FunctionDef) and node.name=='_dispatch_youtube_auto_publish':
                            return node,ast.get_source_segment(content,node)
            raise RuntimeError('YouTube handler not found')
        live=(ROOT/'app.py').read_text()
        old,segment=handler(live)
        if hashlib.sha256(segment.encode()).hexdigest()!='5ae59e34c939a8f2f284648f4b6287dafab26c1c87a2cbca42b43f328a4af2da':
            raise RuntimeError('Live YouTube handler changed')
        candidate=(stage/'app.py').read_text()
        new,_=handler(candidate)
        lines=live.splitlines(keepends=True)
        lines[old.lineno-1:old.end_lineno]=candidate.splitlines(keepends=True)[new.lineno-1:new.end_lineno]
        generated=stage/'.app-channel-cache.py'
        generated.write_text(''.join(lines))
        inputs = [(stage / name, ROOT / name, expected) for name, expected in EXPECTED.items()]
        inputs += [(stage / name, PUBLIC / Path(name).name, expected) for name, expected in EXPECTED.items() if name.startswith('static/')]
        inputs.append((generated,ROOT/'app.py',sha(ROOT/'app.py')))
        def check_baseline():
            if DRAIN_GENERATOR and int(run('systemctl','show',WORKER,'-p','MainPID','--value'))!=worker_pid:
                raise RuntimeError('Worker changed during rolling deployment')
            if sha(ROOT/'scripts/youtube_auto_publish_worker.py') != '409f7196bbc6193c0c712dbda32d59bcf2bafe37ee72c40a77d0f8234a0632d4':
                raise RuntimeError('Existing failure observer worker changed')
            if sha(SQL) != SQL_SHA:
                raise RuntimeError('Material SQL changed; inspect before deployment')
            for source, target, expected in inputs:
                if not source.is_file() or sha(target) != expected:
                    raise RuntimeError('Baseline changed: ' + str(target))
        check_baseline()
        for source, _, _ in inputs:
            if source.suffix == '.py':
                py_compile.compile(str(source), doraise=True)
        run(sys.executable, str(stage / 'scripts/verify_live_feature_guard.py'), '--root', str(stage))
        before = idle_snapshot()
        healthy()
        if args.check:
            print(json.dumps({'check': 'passed', 'commit': args.commit, 'ledger': before, 'files': len(inputs)}))
            return
        if shutil.disk_usage(BASE).free < DB.stat().st_size * 2 + 64 * 1024 * 1024:
            raise RuntimeError('Insufficient backup disk space')
        backup = BASE / 'backups' / ('channel-cache-' + time.strftime('%Y%m%d-%H%M%S') + '-' + args.commit[:12])
        backup.mkdir(parents=True, exist_ok=False)
        records = []
        for source, target, _ in inputs:
            saved = backup / 'files' / str(target).lstrip('/')
            if target.exists():
                saved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, saved)
            records.append({'target': str(target), 'mode': target.stat().st_mode & 0o777 if target.exists() else 0o644,
                            'sha256': sha(target), 'installed_sha256': sha(source)})
        manifest = {'kind': 'youtube-channel-cache', 'commit': args.commit, 'files': records, 'ledger_before': before}
        (backup / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
        stopped = False
        changed = False
        paused = False
        try:
            stopped = True
            stop()
            if DRAIN_GENERATOR:
                check_baseline()
                os.kill(worker_pid,signal.SIGSTOP)
                paused = True
                # Never back up a DB while its paused owner holds a write lock.
                with sqlite3.connect(DB,timeout=1) as probe:
                    probe.execute('BEGIN IMMEDIATE')
                    probe.rollback()
            check_baseline()
            before = idle_snapshot()
            with sqlite3.connect(DB) as src, sqlite3.connect(backup / 'jobs.sqlite3') as dst:
                src.backup(dst)
            os.chmod(backup / 'jobs.sqlite3', 0o600)
            outbox=Path('/mnt/data-disk/youtube-auto-publish/failure-notifications.sqlite3')
            if outbox.exists():
                with sqlite3.connect(outbox) as src, sqlite3.connect(backup/'failure-notifications.sqlite3') as dst:
                    src.backup(dst)
                os.chmod(backup/'failure-notifications.sqlite3',0o600)
            changed = True
            for (source, target, _), row in zip(inputs, records):
                install(source, target, row['mode'])
            for source, target, _ in inputs:
                if sha(source) != sha(target):
                    raise RuntimeError('Installed byte mismatch')
            run(sys.executable, str(stage / 'scripts/verify_live_feature_guard.py'), '--root', str(ROOT), '--public-root', str(PUBLIC))
            start()
            if sha(SQL) != SQL_SHA:
                raise RuntimeError('Material SQL unexpectedly changed')
        except BaseException:
            try:
                if changed:
                    stop()
                    restore(backup, manifest)
                if stopped:
                    start()
            finally:
                if paused:
                    # Resume the original code after rollback; never leave it frozen.
                    os.kill(worker_pid,signal.SIGCONT)
                    paused = False
            raise
        finally:
            if paused:
                # The main process alone was frozen during replacement. Child image
                # generation continued. Queue graceful STOP before allowing new claims.
                try:
                    os.kill(worker_pid,signal.SIGTERM)
                finally:
                    os.kill(worker_pid,signal.SIGCONT)
        result = {'draining_worker_pid': worker_pid if DRAIN_GENERATOR else None, 'commit': args.commit, 'backup': str(backup), 'services': {u: run('systemctl', 'show', u, '-p', 'ActiveState', '--value') for u in UNITS},
                  'ledger_before': before, 'ledger_after': idle_snapshot(require_idle=False), 'material_sql_sha256': sha(SQL),
                  'sha256': {str(target): sha(target) for _, target, _ in inputs}}
        (backup / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result))


if __name__ == '__main__':
    main()
