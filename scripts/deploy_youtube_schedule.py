#!/usr/bin/env python3
"""Deploy optional YouTube native scheduling, preserving unrelated live app code."""
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
EXPECTED = {'features/youtube_auto_publish/service.py': '2c8a2addaf475914597602465e4fc7dd182257b8bffe51e8aa572389bf58c252', 'features/youtube_auto_publish/engine.py': '24ded0c9fcad7afe32afd1d1bcd7cc510a851bb03208af419568af446cc8365e', 'features/drama_synthesis/core.py': '3aa084cf594804cd99691fb872795963ef2df7480ce7a5d7e6a0914fb54e89ca', 'features/youtube_auto_publish/failure_notifications.py': 'ace79407fad61f3b4ed08f8452435295eba21844321c4d086f1c534983cdce49', 'static/youtube-publish.html': 'e3537cd504603a39fadbc887fcd11409d135776c51a176ac5975febd044730c8', 'static/youtube-publish.js': '32d9b76e87035c89d98f74b23bdee722f5e721f8c09d311b3263bf6303e5d1ff', 'static/youtube-publish.css': '2741f039e2bc235e0c7c923e10d260b575482410461575454e525a683d647b76', 'features/youtube_auto_publish/scheduling.py': None}

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
    temp = target.with_name(target.name + '.youtube-scheduled-publish-new')
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
        columns={row[1] for row in db.execute('PRAGMA table_info(drama_youtube_publish)')}
        if require_idle and 'schedule_status' in columns:
            unresolved=db.execute("SELECT count(*) FROM drama_youtube_publish WHERE workflow='reviewed_thumbnail' AND (schedule_status IN ('pending','arming','armed','control_pending','control_running','reconciling','missed') OR schedule_command_status IN ('pending','running','unknown'))").fetchone()[0]
            pre_schedule=db.execute("SELECT count(*) FROM youtube_auto_preparation WHERE state NOT IN ('cancelled') AND json_extract(body,'$.publish_at')<>'' AND NOT EXISTS (SELECT 1 FROM drama_youtube_publish l WHERE l.preparation_id=youtube_auto_preparation.id AND l.workflow='reviewed_thumbnail' AND (l.video_state='published' OR l.schedule_status='cancelled'))").fetchone()[0]
            if unresolved or pre_schedule:raise RuntimeError('Outstanding scheduled intent exists; keep scheduler code and reconcile first')
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
    if args.drain_generator:raise RuntimeError("Schema release requires an idle worker; drain mode is disabled")
    DRAIN_GENERATOR = False
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
    with (BASE / 'scheduled-publish-deploy.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        targets = [ROOT / name for name in EXPECTED] + [ROOT / 'app.py']
        targets += [PUBLIC / Path(name).name for name in EXPECTED if name.startswith('static/')]
        if args.rollback:
            backup = args.rollback.resolve()
            if (BASE / 'backups').resolve() not in backup.parents:
                raise RuntimeError('Invalid backup path')
            manifest = json.loads((backup / 'manifest.json').read_text())
            if manifest.get('kind') != 'youtube-scheduled-publish' or {r['target'] for r in manifest['files']} != {str(t) for t in targets}:
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
        if hashlib.sha256(segment.encode()).hexdigest()!='6f0bb795fd8ec1dabf115aa1c00577ec9aa51196f59a55390fd42982842f3769':
            raise RuntimeError('Live YouTube handler changed')
        candidate=(stage/'app.py').read_text()
        new,_=handler(candidate)
        lines=live.splitlines(keepends=True)
        lines[old.lineno-1:old.end_lineno]=candidate.splitlines(keepends=True)[new.lineno-1:new.end_lineno]
        generated=stage/'.app-scheduled-publish.py'
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
        backup = BASE / 'backups' / ('scheduled-publish-' + time.strftime('%Y%m%d-%H%M%S') + '-' + args.commit[:12])
        backup.mkdir(parents=True, exist_ok=False)
        records = []
        for source, target, _ in inputs:
            saved = backup / 'files' / str(target).lstrip('/')
            if target.exists():
                saved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, saved)
            records.append({'target': str(target), 'mode': target.stat().st_mode & 0o777 if target.exists() else 0o644,
                            'sha256': sha(target), 'installed_sha256': sha(source)})
        manifest = {'kind': 'youtube-scheduled-publish', 'commit': args.commit, 'files': records, 'ledger_before': before}
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
                deadline=time.monotonic()+2
                while not any(line.startswith('State:') and line.split()[1]=='T' for line in Path('/proc/%s/status'%worker_pid).read_text().splitlines()):
                    if time.monotonic()>deadline:raise RuntimeError('Worker did not stop before replacement')
                    time.sleep(.01)
                with sqlite3.connect('file:'+str(DB)+'?mode=ro',uri=True,timeout=1) as probe:
                    generating=probe.execute("SELECT count(*) FROM youtube_auto_preparation WHERE state='generating' AND lease_until>?",(time.time(),)).fetchone()[0]
                children=Path('/proc/%s/task/%s/children'%(worker_pid,worker_pid)).read_text().split()
                image_child=False
                for child in children:
                    try:image_child=image_child or b'codex' in Path('/proc/'+child+'/cmdline').read_bytes().lower()
                    except FileNotFoundError:pass
                if not generating or not image_child:
                    raise RuntimeError('Worker is not inside active generation; resume and use an idle deployment')
                # Never back up a DB while its paused owner holds a write lock.
                for database in (DB,Path('/mnt/data-disk/youtube-auto-publish/failure-notifications.sqlite3')):
                    if database.is_file():
                        with sqlite3.connect(database,timeout=1) as probe:
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
            # Additive migration only; retain all legacy and real platform facts.
            run(sys.executable, '-c', "import sys;sys.path.insert(0,'/root/drama_material_service');from features.drama_synthesis.core import DramaSynthesisStore;DramaSynthesisStore('/root/drama_material_service/data/drama_material_jobs.sqlite3').ensure_storage()")
            start()
            if sha(SQL) != SQL_SHA:
                raise RuntimeError('Material SQL unexpectedly changed')
        except BaseException:
            try:
                if changed:
                    try:
                        stop()
                        idle_snapshot()
                        restore(backup, manifest)
                    finally:
                        start()
                elif stopped:
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
