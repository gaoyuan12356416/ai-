"""Install only the reviewed FB files; preserve publisher data and timer state."""
import argparse
import hashlib
import json
import os
import py_compile
import shutil
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
LIVE = Path('/opt/fb-auto-post/current')
DATA = Path('/mnt/data-disk/fb-auto-post-publisher/fb-auto-post.sqlite3')
BASE = Path('/mnt/data-disk/fb-hit-material-publish')
TIMERS = ['fb-auto-post-' + name + '.timer' for name in ('scheduler', 'plan', 'prepare', 'runner', 'reconcile', 'effects')]
SERVICE = 'fb-auto-post-service.service'
PY_FILES = ['features/fb_auto_posts/' + name + '.py' for name in ('core', 'manual_batch', 'repositories', 'service', 'hit_material')]
STATIC = ['static/' + name for name in ('fb-auto-publish-templates.html', 'fb-auto-publish-templates.js', 'fb-auto-publish.css', 'fb-auto-publish-runs.html')]
BASELINES = {
    'features/fb_auto_posts/core.py': ['31111358729e2ed3a3556ee8c7c2651398ef2b7e6f1190b1eb0eb30d0c88ffe1'],
    'features/fb_auto_posts/manual_batch.py': ['f27941e94628fa28162dff05b4d54d90d843434679a967607045bca18fe88043'],
    'features/fb_auto_posts/repositories.py': ['2626c8095adf0b8166972c58f9538e1508faf045656aa5e90cf31769d574bf87'],
    'features/fb_auto_posts/service.py': ['822417178cae6c339dd792be658efd6c1648beb1ec0084f0fa050dfb2eff9198'],
    'features/fb_auto_posts/hit_material.py': [None],
    'static/fb-auto-publish-templates.html': ['e8f6ec58642270067d9d87376d8e6faf9b1ee95948b410f41d64091959907dc0'],
    'static/fb-auto-publish-templates.js': ['f6f566ba5951d1c9eae96e0e3e38b6634aaff43d9c09badfae3888cf330b303a'],
    'static/fb-auto-publish.css': ['878dc86d48a3631b53cb3d5ba940db5b8a8301380ac33bf2766e0843fc45fd8b', '7a60dfcb06929376eb5a3b8a17f167b16612722c514026f91dbd142230901714'],
    'static/fb-auto-publish-runs.html': ['1cc955b1705f2acd5bed2fc368a51d666d05281133420ac4b40b7ff131e018f2', 'cdbb41c7572f92d64afb8cee76c8abf31bd0780f76676b64a8f7f0218b12d2b1'],
}


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def targets():
    result = [(name, LIVE / name) for name in PY_FILES + STATIC]
    for name in STATIC:
        result += [(name, Path('/root/drama_material_service') / name), (name, Path('/usr/share/nginx/html') / Path(name).name)]
    return result


def db_connect():
    conn = sqlite3.connect('file:' + str(DATA) + '?mode=ro', uri=True, timeout=10)
    conn.execute('PRAGMA query_only=ON')
    return conn


def facts():
    with db_connect() as conn:
        conn.execute('BEGIN')
        result = {}
        tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'fb_auto_%' ORDER BY name")]
        for name in tables:
            require(name.replace('_', '').isalnum(), 'Unexpected table')
            # Existing operational tables are small enough for a consistent full read.
            rows = sorted(json.dumps(row, ensure_ascii=False, default=str) for row in conn.execute('SELECT * FROM ' + name))
            result[name] = {'count': len(rows), 'sha256': hashlib.sha256('\n'.join(rows).encode()).hexdigest()}
        return result


def active_work():
    units = [name.replace('.timer', '.service') for name in TIMERS]
    busy = [name for name in units if run('systemctl', 'show', name, '-p', 'ActiveState', '--value') in ('active', 'activating', 'deactivating')]
    with db_connect() as conn:
        tasks = conn.execute("SELECT id,status FROM fb_auto_task WHERE status IN ('preparing','running')").fetchall()
    return busy, tasks


def active_hit_tasks():
    with db_connect() as conn:
        return conn.execute("""SELECT COUNT(*) FROM fb_auto_task x JOIN fb_auto_run r ON r.id=x.run_id
            WHERE instr(r.config_json,'"hit_material_publish"')>0 AND
            (x.status IN ('planned','preparing','ready','running','submitted','unknown') OR x.unknown_outcome=1)""").fetchone()[0]


def atomic_copy(source, destination, mode=None):
    temporary = destination.with_name(destination.name + '.hit-material-next')
    try:
        shutil.copy2(source, temporary)
        if mode is not None: temporary.chmod(mode)
        os.replace(temporary, destination)
    finally:
        if temporary.exists(): temporary.unlink()


def health():
    for _ in range(25):
        try:
            with urlopen('http://127.0.0.1:18835/health', timeout=3) as response:
                data = json.load(response)
            if data.get('ok') is True: return data
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError('FB service health failed')


def mount_check():
    require(run('findmnt', '-no', 'UUID', '/mnt/data-disk') == '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8', 'Data mount mismatch')
    require(shutil.disk_usage('/mnt/data-disk').free > 2 * 1024**3, 'Insufficient data disk capacity')


def restore(manifest):
    subprocess.check_call(['systemctl', 'stop', SERVICE])
    errors = []
    for item in manifest['files']:
        target = Path(item['target'])
        try:
            if item['backup']:
                atomic_copy(Path(item['backup']), target, item['mode'])
            elif target.exists():
                require(file_hash(target) == item['after'], 'New file changed; refusing removal')
                target.unlink()
        except Exception as exc:
            errors.append(str(exc))
    # A partial copy must not leave the service stopped because the new module
    # was never installed, or because restoring one file failed.
    try:
        subprocess.check_call(['systemctl', 'start', SERVICE])
        health()
    except Exception as exc:
        errors.append(str(exc))
    require(not errors, 'Code restore needs inspection: ' + '; '.join(errors))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--rollback', type=Path)
    args = parser.parse_args()
    mount_check()
    if args.rollback:
        manifest = json.loads(args.rollback.read_text())
        require(str(args.rollback.resolve()).startswith(str(BASE) + '/backups/'), 'Invalid rollback manifest path')
        for item in manifest['files']:
            require(file_hash(Path(item['target'])) == item['after'], 'Deployed file has changed; inspect before rollback')
        enabled = [timer for timer in TIMERS if run('systemctl', 'show', timer, '-p', 'ActiveState', '--value') == 'active']
        subprocess.check_call(['systemctl', 'stop', *TIMERS])
        try:
            require(active_work() == ([], []), 'Publisher work still active; retry rollback after drain')
            require(active_hit_tasks() == 0, 'Active hit-material tasks require the new execution guards; retain code and reconcile first')
            restore(manifest)
        finally:
            if enabled: subprocess.check_call(['systemctl', 'start', *enabled])
        print(json.dumps({'rolled_back': True, 'database_restored': False}))
        return
    commit = run('git', '-C', str(ROOT), 'rev-parse', 'HEAD')
    require(not run('git', '-C', str(ROOT), 'status', '--porcelain', '--untracked-files=no'), 'Source checkout dirty')
    for name, target in targets():
        require(file_hash(target) in BASELINES[name], 'Live baseline drift: ' + str(target))
    for name in PY_FILES:
        py_compile.compile(str(ROOT / name), doraise=True)
    print(json.dumps({'preflight_ok': True, 'commit': commit, 'targets': len(targets()), 'active_work': active_work()}), flush=True)
    if not args.apply: return
    enabled = [timer for timer in TIMERS if run('systemctl', 'show', timer, '-p', 'ActiveState', '--value') == 'active']
    subprocess.check_call(['systemctl', 'stop', *TIMERS])
    changed = False
    try:
        # Never interrupt an active render/upload. The operator can retry later.
        require(active_work() == ([], []), 'Publisher work still active; retry deployment after drain')
        backup = BASE / 'backups' / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + commit[:8])
        backup.mkdir(parents=True, exist_ok=False)
        before = facts()
        with db_connect() as source, sqlite3.connect(str(backup / 'publisher.sqlite3')) as dest:
            source.backup(dest)
        manifest = {'commit': commit, 'backup': str(backup), 'timers': enabled, 'before': before, 'files': []}
        for index, (name, target) in enumerate(targets()):
            saved = backup / ('file-' + str(index)) if target.exists() else None
            mode = target.stat().st_mode & 0o777 if target.exists() else 0o444
            if saved: shutil.copy2(target, saved)
            manifest['files'].append({'source': name, 'target': str(target), 'backup': str(saved) if saved else None, 'mode': mode, 'before': file_hash(target), 'after': file_hash(ROOT / name)})
        manifest_path = backup / 'manifest.json'
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
        subprocess.check_call(['systemctl', 'stop', SERVICE])
        changed = True
        for item in manifest['files']:
            atomic_copy(ROOT / item['source'], Path(item['target']), item['mode'])
        subprocess.check_call(['systemctl', 'start', SERVICE])
        status = health()
        require(facts() == before, 'Publisher facts changed during code-only deployment')
        for item in manifest['files']:
            require(file_hash(Path(item['target'])) == item['after'], 'Post-deploy file hash mismatch')
        manifest['health'] = status
        manifest['facts_unchanged'] = True
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
        print(json.dumps({'deployed': True, 'commit': commit, 'manifest': str(manifest_path), 'health': status, 'facts_unchanged': True}), flush=True)
    except Exception:
        if changed:
            require(active_hit_tasks() == 0, 'A hit-material run was accepted; preserving the new code and publisher facts for inspection')
            restore(manifest)
        raise
    finally:
        if enabled: subprocess.check_call(['systemctl', 'start', *enabled])


if __name__ == '__main__':
    main()
