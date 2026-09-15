"""Exact-source retirement rollout. Runtime data and history are never restored over live state."""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import time

ROOT = Path('/mnt/data-disk/retired-ai-modules-20260915')
LIVE = Path('/root/drama_material_service')
PUBLIC = Path('/usr/share/nginx/html')
UNITS = ['ad-control-v3-runner.timer', 'ad-control-v3-runner.service',
         'ad-material-frontend-test.service', 'drama-material-api-test.service']
EXPECTED = {
    'app.py': '5056aaafd1067584c793bacc1b8fcff43c09c13b9e5557566c397ba70ced36a7',
    'static/quick-nav.js': 'cd49720093c1d9e9accbeb39d18d38ab8c28ef350523968261bebb2298252bfe',
}
VERSION = '20260915retired'


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def validate_disk():
    assert run('findmnt', '-n', '-o', 'UUID', '/mnt/data-disk') == '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    assert shutil.disk_usage('/mnt/data-disk').free > 1024 ** 3


def atomic_write(path, data, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.retire-tmp')
    temp.write_bytes(data)
    temp.chmod(mode)
    temp.replace(path)


def prepare(release):
    validate_disk()
    assert ROOT in release.resolve().parents
    assert not run('git', '-C', str(release), 'status', '--porcelain')
    commit = run('git', '-C', str(release), 'rev-parse', 'HEAD')
    for rel, expected in EXPECTED.items():
        assert digest(LIVE / rel) == expected, 'live source changed: ' + rel
    assert digest(PUBLIC / 'quick-nav.js') == EXPECTED['static/quick-nav.js']
    backup = ROOT / ('backup-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    backup.mkdir(mode=0o700, parents=True)
    changes = []

    def add(path, data):
        before = path.read_bytes() if path.exists() else None
        if before == data:
            return
        name = str(len(changes))
        if before is not None:
            (backup / (name + '.before')).write_bytes(before)
        (backup / (name + '.after')).write_bytes(data)
        changes.append({'path': str(path), 'name': name, 'before': digest(path),
                        'after': hashlib.sha256(data).hexdigest(),
                        'mode': (path.stat().st_mode & 0o777) if path.exists() else 0o644})

    for rel in ['app.py', 'features/retired_modules.py', 'static/quick-nav.js',
                'scripts/ad_control_rule_runner.py', 'scripts/ad_control_v3_runner.py']:
        data = (release / rel).read_bytes()
        add(LIVE / rel, data)
        if rel.startswith('static/'):
            add(PUBLIC / Path(rel).name, data)
    spec = importlib.util.spec_from_file_location('retired', release / 'features/retired_modules.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for path in [PUBLIC / 'navigation.json', LIVE / 'static/navigation.json']:
        nav = json.loads(path.read_text(encoding='utf-8-sig'))
        add(path, (json.dumps(module.filter_navigation(nav), ensure_ascii=False, indent=2) + '\n').encode())
    # Update only script URLs in each current live page; do not replace unrelated page content.
    for root in [PUBLIC, LIVE / 'static']:
        for path in root.glob('*.html'):
            old = path.read_text(encoding='utf-8')
            new = re.sub(r'quick-nav\.js(?:\?[^"\s<>]*)?', 'quick-nav.js?v=' + VERSION, old)
            if new != old:
                add(path, new.encode())
    add(Path('/etc/nginx/default.d/retired-ai-modules.conf'), (release / 'deploy/retired-ai-modules.conf').read_bytes())
    env_path = Path('/mnt/data-disk/ai-ad-control-v3/config/runtime.env')
    env = env_path.read_text()
    for key in ['RUNNER_ENABLED', 'RUNNER_OBSERVE_RELEASED', 'RUNNER_LIVE_RELEASED',
                'LIVE_PAUSE_ENABLED', 'LIVE_COPY_ENABLED', 'COPY_PERSISTENCE_ENABLED', 'COPY_ACTIVATE_ENABLED']:
        key = 'AD_CONTROL_V3_' + key
        env, count = re.subn(r'^' + key + r'=.*$', key + '=0', env, flags=re.M)
        if count == 0:
            env += '\n' + key + '=0\n'
    add(env_path, env.encode())
    units = {}
    for unit in UNITS:
        props = dict(line.split('=', 1) for line in run('systemctl', 'show', unit, '-p', 'ActiveState', '-p', 'UnitFileState', '-p', 'MemoryCurrent').splitlines())
        path = Path('/etc/systemd/system') / unit
        assert path.is_file() and not path.is_symlink(), 'unexpected unit file: ' + str(path)
        (backup / unit).write_bytes(path.read_bytes())
        props['hash'] = digest(path)
        units[unit] = props
    cron = subprocess.run(['crontab', '-l'], capture_output=True, text=True, check=True).stdout
    (backup / 'crontab.before').write_text(cron)
    removed = [line for line in cron.splitlines() if 'scripts/ad_control_rule_runner.py' in line and not line.lstrip().startswith('#')]
    assert len(removed) == 1, 'unexpected legacy scheduler entries'
    for label, db_path in [('main', LIVE / 'data/drama_material_jobs.sqlite3'),
                           ('test', Path('/root/drama_material_service_test/data/drama_material_jobs.sqlite3'))]:
        with sqlite3.connect(db_path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5) as src:
            assert not src.execute("SELECT 1 FROM ad_material_task WHERE status IN ('generating_demand','generating_material') LIMIT 1").fetchone(), 'generation active'
            with sqlite3.connect(str(backup / (label + '.sqlite3'))) as dst:
                src.backup(dst)
                assert dst.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    plan = {'release': str(release), 'commit': commit, 'changes': changes, 'units': units, 'removed_cron': removed}
    (backup / 'plan.json').write_text(json.dumps(plan, indent=2))
    print(json.dumps({'backup': str(backup), 'commit': commit, 'files': len(changes), 'unit_memory_bytes': sum(int(x.get('MemoryCurrent', '0')) for x in units.values() if x.get('MemoryCurrent', '').isdigit())}))


def wait_runners():
    names = {b'ad_control_rule_runner.py', b'ad_control_v3_runner.py'}
    for attempt in range(30):
        running = []
        for path in Path('/proc').iterdir():
            if not path.name.isdigit():
                continue
            try:
                args = (path / 'cmdline').read_bytes().split(b'\0')
            except OSError:
                continue
            if any(arg.rsplit(b'/', 1)[-1] in names for arg in args):
                running.append(path.name)
        if not running:
            return
        time.sleep(1)
    raise RuntimeError('existing scheduler still running; preserve its outcome: ' + str(running))


def switch(backup, rollback=False):
    validate_disk()
    assert ROOT in backup.resolve().parents
    plan = json.loads((backup / 'plan.json').read_text())
    if rollback:
        assert not (backup / 'reclaim.json').exists(), 'restore archived test directory before resuming retired services'
    for change in plan['changes']:
        expected = change['after'] if rollback else change['before']
        assert digest(Path(change['path'])) == expected, 'source drift: ' + change['path']
        desired = 'before' if rollback else 'after'
        if change[desired] is not None:
            assert digest(backup / (change['name'] + '.' + desired)) == change[desired], 'backup changed'
    if not rollback:
        cron = subprocess.run(['crontab', '-l'], capture_output=True, text=True, check=True).stdout
        assert all(line in cron.splitlines() for line in plan['removed_cron']), 'cron changed'
        subprocess.run(['crontab', '-'], input='\n'.join(line for line in cron.splitlines() if line not in plan['removed_cron']) + '\n', text=True, check=True)
        subprocess.run(['systemctl', 'stop', 'ad-control-v3-runner.timer'], check=True)
        wait_runners()
        subprocess.run(['systemctl', 'disable', '--now'] + UNITS, check=True)
        for unit in UNITS:
            path = Path('/etc/systemd/system') / unit
            assert digest(path) == plan['units'][unit]['hash'], 'unit drift'
            path.unlink()
            path.symlink_to('/dev/null')
    for change in plan['changes']:
        target = Path(change['path'])
        desired = 'before' if rollback else 'after'
        if change[desired] is None:
            target.unlink(missing_ok=True)
        else:
            atomic_write(target, (backup / (change['name'] + '.' + desired)).read_bytes(), change['mode'])
    if rollback:
        for unit in UNITS:
            path = Path('/etc/systemd/system') / unit
            assert path.is_symlink() and str(path.readlink()) == '/dev/null'
            path.unlink()
            atomic_write(path, (backup / unit).read_bytes())
    subprocess.run(['nginx', '-t'], check=True)
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', 'reload', 'nginx'], check=True)
    subprocess.run(['python3', '-m', 'py_compile', str(LIVE / 'app.py')], check=True)
    subprocess.run(['bash', str(Path(plan['release']) / 'scripts/safe_restart_drama_api.sh')], check=True, stdout=subprocess.DEVNULL)
    if rollback:
        cron = subprocess.run(['crontab', '-l'], capture_output=True, text=True, check=True).stdout
        for line in plan['removed_cron']:
            if line not in cron.splitlines():
                cron = cron.rstrip('\n') + '\n' + line + '\n'
        subprocess.run(['crontab', '-'], input=cron, text=True, check=True)
        for unit, old in plan['units'].items():
            if old['UnitFileState'] == 'enabled':
                subprocess.run(['systemctl', 'enable', unit], check=True)
            if old['ActiveState'] == 'active':
                subprocess.run(['systemctl', 'start', unit], check=True)
    (backup / ('rollback.done' if rollback else 'apply.done')).write_text(datetime.now(timezone.utc).isoformat())
    print(json.dumps({'action': 'rollback' if rollback else 'applied', 'backup': str(backup), 'commit': plan['commit']}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['prepare', 'apply', 'rollback'])
    parser.add_argument('path', type=Path)
    args = parser.parse_args()
    prepare(args.path) if args.mode == 'prepare' else switch(args.path, args.mode == 'rollback')
