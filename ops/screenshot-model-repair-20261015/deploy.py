"""Scoped, GitHub-sourced screenshot rollout. Never changes job rows."""
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

ROOT = Path(__file__).resolve().parents[2]
STATE = Path('/mnt/data-disk/codex/tasks/screenshot-model-repair-20261015')
LIVE = Path('/root/drama_material_service/codex_screenshot_service.py')
DB = '/root/drama_material_service/data/drama_material_jobs.sqlite3'
OLD_SHA = '9d410bc9e33dccd765a5b0ead95c8277c3b1c3937305a40b88a9a4313118aa93'
UNITS = ['codex-screenshot-batch.service', 'codex-screenshot-batch-burst.service',
         'codex-screenshot-migrated-primary.service', 'codex-screenshot-migrated-burst.service']
PORTS = [8795, 8798, 18795, 18798]
OVERRIDE = '95-screenshot-model-repair.conf'


def run(*args):
    return subprocess.check_output(list(args)).decode().strip()


def read_db():
    conn = sqlite3.connect('file:' + DB + '?mode=ro', uri=True, timeout=10)
    conn.execute('PRAGMA query_only=ON')
    conn.row_factory = sqlite3.Row
    return conn


def assert_drained():
    with read_db() as conn:
        assert conn.execute("SELECT count(*) FROM drama_screenshot_job WHERE status NOT IN ('done','failed')").fetchone()[0] == 0, 'active screenshot jobs: drain before rollout'
    # No active HTTP generation request may be interrupted by the restart.
    connections = run('ss', '-Htn', 'state', 'established')
    assert not any('127.0.0.1:%d ' % p in connections for p in PORTS), 'active sidecar connection'


def main():
    assert run('findmnt', '-no', 'UUID', '/mnt/data-disk') == '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    assert os.access('/mnt/data-disk', os.W_OK)
    assert shutil.disk_usage('/mnt/data-disk').free > 5 * 1024**3
    assert not run('git', '-C', str(ROOT), 'status', '--porcelain')
    commit = run('git', '-C', str(ROOT), 'rev-parse', 'HEAD')
    assert_drained()
    assert hashlib.sha256(LIVE.read_bytes()).hexdigest() == OLD_SHA, 'live source drift'
    STATE.mkdir(parents=True, exist_ok=True)
    backup = STATE / 'before-deploy'
    backup.mkdir()  # An existing backup is never overwritten.
    os.chmod(str(STATE), 0o700)
    shutil.copy2(str(LIVE), str(backup / LIVE.name))
    with read_db() as source:
        with sqlite3.connect(str(backup / 'jobs.sqlite3')) as target:
            source.backup(target)
        rows = [dict(r) for r in source.execute(
            "SELECT * FROM drama_screenshot_job WHERE status='failed' AND created_at>=? AND created_at<? ORDER BY created_at,job_id",
            ('2026-10-14 16:00:00', '2026-10-15 16:00:00'))]
        (STATE / 'frozen-failures.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
    ui = backup / 'ui'
    ui.mkdir()
    shutil.copytree('/root/drama_material_service/static', str(ui / 'service-static'))
    public = ui / 'public-static'; public.mkdir()
    for path in Path('/usr/share/nginx/html').iterdir():
        if path.is_file() and path.suffix in ('.html', '.js', '.css', '.json'):
            shutil.copy2(str(path), str(public / path.name))
    for unit in UNITS:
        (backup / (unit + '.txt')).write_text(run('systemctl', 'cat', unit), encoding='utf-8')
        override = Path('/etc/systemd/system') / (unit + '.d') / OVERRIDE
        assert not override.exists(), 'dedicated rollout override already exists'
    release = Path('/mnt/data-disk/codex-workers/us-migrated/releases') / commit / 'runtime'
    release.mkdir(parents=True)
    candidate = release / LIVE.name
    shutil.copy2(str(ROOT / LIVE.name), str(candidate))
    run('/usr/bin/python3', '-m', 'py_compile', str(candidate))
    run('/usr/bin/python3', str(ROOT / 'scripts/test_screenshot_model_compatibility.py'))
    metadata = {'commit': commit, 'release': str(candidate), 'backup': str(backup),
                'old_sha256': OLD_SHA, 'new_sha256': hashlib.sha256(candidate.read_bytes()).hexdigest(),
                'frozen_count': len(rows), 'ports': PORTS, 'units': UNITS,
                'api_pid_before': run('systemctl', 'show', 'drama-material-api', '-p', 'MainPID', '--value')}
    (STATE / 'deployment.json').write_text(json.dumps(metadata, indent=2), encoding='utf-8')
    assert_drained()
    for unit in UNITS:
        override = Path('/etc/systemd/system') / (unit + '.d') / OVERRIDE
        override.parent.mkdir(exist_ok=True)
        command = '/usr/bin/python3 ' + str(candidate)
        if 'migrated' in unit:
            slot = 'screenshot-burst' if 'burst' in unit else 'screenshot-primary'
            wrapper = '/mnt/data-disk/codex-workers/us-migrated/releases/dc4346f421e4def62bc0f97070df2fd9a2134d35/run.sh'
            command = '/bin/bash %s %s %s' % (wrapper, slot, candidate)
        override.write_text('[Service]\nEnvironment=CODEX_SCREENSHOT_CODEX_MODEL=gpt-6.1-sol\nExecStart=\nExecStart=' + command + '\n', encoding='utf-8')
    shutil.copy2(str(candidate), str(LIVE))
    run('systemctl', 'daemon-reload')
    run('systemctl', 'restart', *UNITS)
    for port in PORTS:
        for attempt in range(15):
            try:
                health = json.load(urllib.request.urlopen('http://127.0.0.1:%d/healthz' % port, timeout=3))
                assert health['codex_model'] == 'gpt-6.1-sol'
                assert health['codex_reasoning'] == 'low'
                print('HEALTH', port, health['codex_model'], health['codex_reasoning'])
                break
            except Exception:
                if attempt == 14: raise
                time.sleep(1)
    assert metadata['api_pid_before'] == run('systemctl', 'show', 'drama-material-api', '-p', 'MainPID', '--value')
    print(json.dumps(metadata, indent=2))


if __name__ == '__main__':
    main()
