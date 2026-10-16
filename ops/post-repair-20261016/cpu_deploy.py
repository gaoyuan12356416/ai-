"""Staged GitHub-first repair. Run only with an operator-reviewed plan JSON."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

ROOT = Path('/mnt/data-disk/post-repair-20261016')
TIMERS = ['tt-auto-post-runner.timer', 'tt-auto-post-scheduler.timer', 'tt-auto-post-runner.path',
          'fb-auto-post-prepare.timer', 'fb-auto-post-runner.timer']
FLOOR = '2026-10-15T16:00:00+00:00'


def run(args, **kwargs):
    return subprocess.check_output(args, **kwargs).decode().strip()


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def check_disk():
    assert run(['findmnt', '-rn', '-o', 'UUID', '--mountpoint', '/mnt/data-disk']) == '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    assert shutil.disk_usage('/mnt/data-disk').free > 5 * 1024 ** 3


def read_db(path):
    db = sqlite3.connect('file:' + path + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    return db


def history():
    result = {}
    for name, path, table, stamp, attempts in [
        ('tt', '/mnt/data-disk/tt-auto-post-publisher/tt-auto-post.sqlite3', 'tt_auto_task', 'scheduled_at_utc', 'publish_attempt_count'),
        ('fb', '/mnt/data-disk/fb-auto-post-publisher/fb-auto-post.sqlite3', 'fb_auto_task', 'planned_publish_at_utc', 'attempt_count'),
    ]:
        db = read_db(path)
        result[name] = [dict(row) for row in db.execute('SELECT id,status,' + attempts + ' attempts FROM ' + table + ' WHERE julianday(' + stamp + ')<julianday(?)', (FLOOR,))]
        db.close()
    return result


def verify_baseline(plan):
    for meta in plan['components'].values():
        root = Path(meta['root'])
        for file, digest in meta['sha256'].items():
            assert hashlib.sha256((root / file).read_bytes()).hexdigest() == digest, (root, file)


def stage(plan):
    verify_baseline(plan)
    for name, meta in plan['components'].items():
        cache = meta['cache']
        subprocess.run(['git', '-C', cache, 'fetch', 'origin', meta['branch']], check=True, stdout=subprocess.DEVNULL)
        assert run(['git', '-C', cache, 'rev-parse', meta['commit'] + '^{commit}']) == meta['commit']
        target = ROOT / 'releases' / (name + '-' + meta['commit'])
        assert not target.exists(), str(target)
        shutil.copytree(Path(meta['root']).resolve(), target, symlinks=True, ignore=shutil.ignore_patterns('.git', '__pycache__'))
        for file in meta['files']:
            contents = subprocess.check_output(['git', '-C', cache, 'show', meta['commit'] + ':' + file])
            destination = target / file
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(contents)
        meta['candidate'] = str(target)
        code = [str(target / file) for file in meta['files'] if file.endswith('.py')]
        subprocess.run([sys.executable, '-m', 'py_compile', *code], check=True)
        subprocess.run([sys.executable, '-m', 'unittest', *meta['tests'], '-q'], cwd=target, check=True, timeout=120)
    save(ROOT / 'stage.json', plan)
    print(json.dumps({'status': 'staged_and_tested', 'candidates': {k: v['candidate'] for k, v in plan['components'].items()}}))


def pause_and_backup(plan):
    verify_baseline(plan)
    backup = ROOT / 'backups' / plan['stamp'] / 'cpu'
    backup.mkdir(parents=True, exist_ok=False, mode=0o700)
    state = {unit: subprocess.run(['systemctl', 'is-active', unit], capture_output=True, text=True).stdout.strip() for unit in TIMERS}
    save(backup / 'timer-state.json', state)
    save(backup / 'plan.json', plan)
    subprocess.run(['systemctl', 'stop', *TIMERS], check=True)
    # At this point no new runner may enter. Do not terminate an outbound write.
    db = read_db('/mnt/data-disk/tt-auto-post-publisher/tt-auto-post.sqlite3')
    assert db.execute("SELECT COUNT(*) FROM tt_auto_task WHERE status IN ('selecting','preparing','publishing')").fetchone()[0] == 0, 'TT write/preparation is still active'
    db.close()
    subprocess.run(['systemctl', 'stop', 'tt-auto-post-runner.service', 'fb-auto-post-prepare.service', 'fb-auto-post-runner.service'], check=True, timeout=45)
    for name, source in [('tt', '/mnt/data-disk/tt-auto-post-publisher/tt-auto-post.sqlite3'),
                         ('fb', '/mnt/data-disk/fb-auto-post-publisher/fb-auto-post.sqlite3'),
                         ('delivery', '/mnt/data-disk/post-daily-report/delivery.sqlite3')]:
        db = read_db(source)
        dest = sqlite3.connect(backup / (name + '.sqlite3'))
        db.backup(dest)
        assert dest.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
        dest.close(); db.close()
    save(backup / 'historical-attempts.json', history())
    for name, meta in plan['components'].items():
        save(backup / (name + '-release.json'), {'link': meta['root'], 'previous': str(Path(meta['root']).resolve())})
        for file in meta['sha256']:
            dest = backup / name / file
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(Path(meta['root']) / file, dest)
    print(json.dumps({'status': 'paused_and_backed_up', 'backup': str(backup)}))


def switch(plan):
    backup = ROOT / 'backups' / plan['stamp'] / 'cpu'
    assert (backup / 'historical-attempts.json').exists()
    verify_baseline(plan)
    env = Path('/etc/tt-auto-post-execution-policy-20261016.env')
    dropin = Path('/etc/systemd/system/tt-auto-post-service.service.d/99-zzzz-execution-policy-20261016.conf')
    assert not env.exists() and not dropin.exists()
    env.write_text('TT_AUTO_POST_EXECUTION_NOT_BEFORE_UTC=' + FLOOR + '\n')
    env.chmod(0o600)
    dropin.parent.mkdir(parents=True, exist_ok=True)
    dropin.write_text('[Service]\nEnvironmentFile=' + str(env) + '\n')
    for meta in plan['components'].values():
        link = Path(meta['root'])
        assert link.is_symlink()
        temp = link.with_name(link.name + '.repair-20261016')
        temp.symlink_to(meta['candidate'])
        os.replace(temp, link)
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', 'restart', 'tt-auto-post-service.service', 'fb-auto-post-service.service'], check=True, timeout=60)
    save(backup / 'switch.json', {'status': 'switched', 'floor': FLOOR})
    print(json.dumps({'status': 'switched', 'floor': FLOOR, 'timers': 'remain paused until verification'}))


def verify_history(plan):
    backup = ROOT / 'backups' / plan['stamp'] / 'cpu'
    before = json.loads((backup / 'historical-attempts.json').read_text())
    after = history()
    changes = {}
    for name in before:
        old = {r['id']: r['attempts'] for r in before[name]}
        changes[name] = [r['id'] for r in after[name] if r['id'] in old and r['attempts'] != old[r['id']]]
        assert not changes[name], changes
    save(backup / 'historical-attempts-after.json', after)
    return changes


def resume(plan):
    verify_history(plan)
    backup = ROOT / 'backups' / plan['stamp'] / 'cpu'
    state = json.loads((backup / 'timer-state.json').read_text())
    subprocess.run(['systemctl', 'start', *[u for u, active in state.items() if active == 'active']], check=True)
    print(json.dumps({'status': 'original_triggers_restored', 'historical_attempt_changes': verify_history(plan)}))


def rollback(plan):
    backup = ROOT / 'backups' / plan['stamp'] / 'cpu'
    subprocess.run(['systemctl', 'stop', *TIMERS, 'tt-auto-post-runner.service'], check=True)
    for name, meta in plan['components'].items():
        previous = json.loads((backup / (name + '-release.json')).read_text())['previous']
        link = Path(meta['root']); temp = link.with_name(link.name + '.rollback-20261016')
        temp.symlink_to(previous); os.replace(temp, link)
    subprocess.run(['systemctl', 'restart', 'tt-auto-post-service.service', 'fb-auto-post-service.service'], check=True)
    print('Rolled back code; TT and FB producer triggers remain stopped. SQLite and historical ledgers preserved. TT must not resume on old code without the history guard.')


if __name__ == '__main__':
    check_disk()
    plan = json.loads(Path(sys.argv[2]).read_text())
    {'stage': stage, 'pause': pause_and_backup, 'switch': switch, 'resume': resume, 'rollback': rollback}[sys.argv[1]](plan)
