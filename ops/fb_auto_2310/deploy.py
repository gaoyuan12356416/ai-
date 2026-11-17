"""GitHub-first FB landing migration; retain frozen URLs and publication facts."""
import argparse
import contextlib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time

from patches import patch_source

SOURCE = Path(__file__).resolve().parents[2]
LIVE = Path('/opt/fb-auto-post/current')
MAIN = Path('/root/drama_material_service')
HEALTH = Path('/opt/publish-scheduler-health/current/scripts/publish_scheduler_health.py')
DB = Path('/mnt/data-disk/fb-auto-post-publisher/fb-auto-post.sqlite3')
NGINX = Path('/etc/nginx/snippets/fb-auto-post-s2l.conf')
REPORT = 'features/social_analytics/'
ROOTS = {
    'fb': LIVE, 'main': MAIN,
    'daily': Path('/opt/youtube-publisher-daily-report/current'),
    'warehouse': Path('/opt/social-analytics-warehouse/current'),
    'gap': Path('/opt/socialkit-analytics/current'),
}
COMMON = [REPORT + name for name in ('facebook.py', 'report.py', 'adapters.py')]
FILES = {
    'fb': ['features/fb_auto_posts/links.py', 'features/fb_auto_posts/effect_collector.py'],
    'main': COMMON + [REPORT + 'routes.py', REPORT + 'warehouse_refresh.py'],
    'daily': COMMON + ['features/social_daily_report/collector.py'],
    'warehouse': COMMON + [REPORT + 'routes.py', REPORT + 'warehouse_refresh.py'],
    'gap': COMMON + [REPORT + 'routes.py'],
}


def run(args, **kwargs):
    return subprocess.check_output(list(map(str, args)), **kwargs).decode().strip()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def normalized(data):
    return digest(data.replace(b'\r\n', b'\n'))


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    path.chmod(0o600)


def atomic(path, data):
    info = path.stat()
    temporary = path.with_name(path.name + '.fb-2310-next')
    with temporary.open('xb') as handle:
        os.fchmod(handle.fileno(), info.st_mode)
        os.fchown(handle.fileno(), info.st_uid, info.st_gid)
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def helper():
    path = SOURCE / 'scripts/deploy_publish_queue_isolation.py'
    expected = json.loads((SOURCE / 'ops/fb_auto_2310/baseline.json').read_text())['switch/deploy_publish_queue_isolation.py']
    assert normalized(path.read_bytes()) == expected, 'switch_helper_version_changed'
    spec = importlib.util.spec_from_file_location('verified_switch', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def inventory():
    """Read frozen task links only; no state or receipt writes."""
    root = Path('/mnt/data-disk/fb-auto-post-public/s2l/fb')
    counters = {}; missing = []; mismatches = []; published = 0
    state = hashlib.sha256()
    with contextlib.closing(sqlite3.connect(DB.as_uri() + '?mode=ro', uri=True)) as conn:
        rows = conn.execute("SELECT id,status,long_url,short_url,graph_post_id FROM fb_auto_task "
                            "WHERE long_url LIKE 'https://www.dramawavew2a.com/ads/0/2049/view?%' ORDER BY id").fetchall()
        for task_id, status, url, short, graph in rows:
            counters[status] = counters.get(status, 0) + 1
            state.update(json.dumps([task_id, url, short, graph], ensure_ascii=False).encode())
            if status != 'published':
                continue
            published += 1
            path = root / (str(task_id) + '.html')
            if not path.is_file():
                missing.append(task_id)
            elif url.split('?', 1)[0].encode() not in path.read_bytes():
                mismatches.append(task_id)
    return dict(legacy_links=len(rows), published=published, status_counts=counters,
                missing_published_files=missing, mismatched_published_files=mismatches,
                frozen_links_and_receipts_sha256=state.hexdigest())


def prepare(folder):
    assert run(['findmnt', '-n', '-o', 'UUID', '/mnt/data-disk']) == '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    assert folder.is_relative_to(Path('/mnt/data-disk')) and shutil.disk_usage('/mnt/data-disk').free > 1024**3
    assert not run(['git', '-C', SOURCE, 'status', '--porcelain']), 'source_not_clean'
    commit = run(['git', '-C', SOURCE, 'rev-parse', 'HEAD'])
    baseline = json.loads((SOURCE / 'ops/fb_auto_2310/baseline.json').read_text())
    ops = helper()
    folder.mkdir(mode=0o711, parents=True, exist_ok=False)
    original = LIVE.resolve()
    candidate = folder / 'candidate'
    shutil.copytree(original, candidate, symlinks=True, ignore=shutil.ignore_patterns('.git', '__pycache__', '*.pyc'))
    ops.preserve_owners(original, candidate)
    rec = dict(commit=commit, before_root=str(original), candidate=str(candidate), files={},
               roots={name: str(root.resolve()) for name, root in ROOTS.items()},
               main_before_pid=ops.pid('drama-material-api.service'), status='prepared')
    fb = dict(before_root=str(original), candidate=str(candidate), files={}, state='prepared')
    for name, relatives in FILES.items():
        root = ROOTS[name].resolve()
        if (root / '.git').exists():
            rec.setdefault('git_before', {})[name] = dict(
                head=run(['git', '-C', root, 'rev-parse', 'HEAD']),
                status=run(['git', '-C', root, 'status', '--porcelain']))
        for rel in relatives:
            path = root / rel
            before = path.read_bytes()
            key = name + '/' + rel
            if key in baseline:
                assert digest(before) == baseline[key], 'concurrent_source_change:' + key
            after = patch_source(rel, before.decode())
            if name == 'fb':
                atomic(candidate / rel, after)
                fb['files'][rel] = dict(before=normalized(before), after=normalized(after))
            else:
                number = str(len(rec['files']))
                (folder / ('before-' + number)).write_bytes(before)
                (folder / ('after-' + number)).write_bytes(after)
                rec['files'][str(path)] = dict(name=number, before=digest(before), after=digest(after))
    before = NGINX.read_bytes()
    assert digest(before) == baseline['nginx/fb-auto-post-s2l.conf'], 'concurrent_nginx_change'
    (folder / 'nginx-before').write_bytes(before)
    after = (SOURCE / 'ops/fb_auto_2310/fb-auto-post-s2l.conf').read_bytes()
    (folder / 'nginx-after').write_bytes(after)
    rec['nginx'] = dict(before=digest(before), after=digest(after))
    with contextlib.closing(sqlite3.connect(DB.as_uri() + '?mode=ro', uri=True)) as src:
        with contextlib.closing(sqlite3.connect(folder / 'fb-before.sqlite3')) as dst:
            src.backup(dst)
            assert dst.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
    (folder / 'fb-before.sqlite3').chmod(0o600)
    rec['inventory_before'] = inventory()
    rec['main_dependencies'] = {unit: run(['systemctl', 'show', unit, '-p', 'ActiveState', '--value'])
                                for unit in run(['systemctl', 'show', 'drama-material-api.service', '-p', 'RequiredBy', '--value']).split()}
    save(folder / 'manifest.json', dict(lanes=dict(fb=fb)))
    rollback_candidate = folder / 'routing-rollback-candidate'
    shutil.copytree(candidate, rollback_candidate, symlinks=True,
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    ops.preserve_owners(candidate, rollback_candidate)
    rel = 'features/fb_auto_posts/links.py'
    data = (rollback_candidate / rel).read_bytes()
    data = data.replace(b'FB_W2A_BASE_URL = "https://www.dramawavew2a.com/ads/101/2310/view"',
                        b'FB_W2A_BASE_URL = "https://www.dramawavew2a.com/ads/0/2049/view"', 1)
    atomic(rollback_candidate / rel, data)
    rollback_dir = folder / 'routing-rollback'
    rollback_dir.mkdir(mode=0o711)
    save(rollback_dir / 'manifest.json', dict(lanes=dict(fb=dict(
        before_root=str(candidate), candidate=str(rollback_candidate), state='prepared',
        files={rel: dict(before=normalized((candidate / rel).read_bytes()), after=normalized(data))}))))
    save(folder / 'record.json', rec)
    print(json.dumps(dict(prepared=str(folder), commit=commit, inventory=rec['inventory_before'])), flush=True)


def restart_main(rec):
    """Use the installed ExecStop barrier; never terminate an in-flight job."""
    unit = 'drama-material-api.service'
    run(['systemctl', '--job-mode=ignore-dependencies', '--no-block', 'stop', unit])
    until_log = 0
    while run(['systemctl', 'show', unit, '-p', 'MainPID', '--value']) != '0':
        if time.monotonic() >= until_log:
            print(json.dumps(dict(main_stop='waiting_for_existing_service_drain')), flush=True)
            until_log = time.monotonic() + 20
        time.sleep(1)
    run(['systemctl', 'start', unit])
    for dependency, state in rec['main_dependencies'].items():
        if state == 'active' and run(['systemctl', 'show', dependency, '-p', 'ActiveState', '--value']) != 'active':
            run(['systemctl', 'start', dependency])
    rec['main_after_pid'] = run(['systemctl', 'show', unit, '-p', 'MainPID', '--value'])
    assert run(['systemctl', 'is-active', unit]) == 'active'


def activate(folder, rollback=False):
    rec = json.loads((folder / 'record.json').read_text())
    expected, selected = ('after', 'before') if rollback else ('before', 'after')
    for name, root in ROOTS.items():
        if name != 'fb':
            assert str(root.resolve()) == rec['roots'][name], 'concurrent_release_change:' + name
    for filename, item in rec['files'].items():
        assert digest(Path(filename).read_bytes()) == item[expected], 'concurrent_file_change:' + filename
    assert digest(NGINX.read_bytes()) == rec['nginx'][expected], 'concurrent_nginx_change'
    # Report compatibility is installed before new traffic is routed to 2310.
    for filename, item in rec['files'].items():
        atomic(Path(filename), (folder / (selected + '-' + item['name'])).read_bytes())
    restart_main(rec)
    save(folder / 'record.json', rec)
    ops = helper()
    prefix = 'rollback-' if rollback else ''
    snapshot = folder / (prefix + 'schedulers-before.json')
    run([sys.executable, HEALTH, 'snapshot', '--lane', 'fb', '--output', snapshot])
    try:
        ops.switch(folder, 'fb', rollback, preserve_queue_scopes=True)
    finally:
        run([sys.executable, HEALTH, 'restore', '--baseline', snapshot, '--wait-seconds', '25',
             '--output', folder / (prefix + 'schedulers-after.json')])
    atomic(NGINX, (folder / ('nginx-' + selected)).read_bytes())
    try:
        run(['nginx', '-t'], stderr=subprocess.STDOUT)
        run(['systemctl', 'reload', 'nginx'])
    except BaseException:
        atomic(NGINX, (folder / ('nginx-' + expected)).read_bytes())
        run(['nginx', '-t'], stderr=subprocess.STDOUT)
        raise
    rec['status'] = 'rolled_back' if rollback else 'active'
    rec['inventory_after'] = inventory()
    save(folder / 'record.json', rec)
    print(json.dumps(dict(status=rec['status'], source_commit=rec['commit'],
                          main_pid=rec['main_after_pid'], inventory=rec['inventory_after'],
                          ledgers_preserved=True, nginx_reload=True)), flush=True)


def rollback_routing(folder):
    """Revert routing while retaining compatibility with already-created 2310 tasks."""
    rec = json.loads((folder / 'record.json').read_text())
    assert digest(NGINX.read_bytes()) == rec['nginx']['after'], 'later_nginx_change'
    snapshot = folder / 'routing-rollback-schedulers-before.json'
    run([sys.executable, HEALTH, 'snapshot', '--lane', 'fb', '--output', snapshot])
    try:
        helper().switch(folder / 'routing-rollback', 'fb', False, preserve_queue_scopes=True)
    finally:
        run([sys.executable, HEALTH, 'restore', '--baseline', snapshot, '--wait-seconds', '25',
             '--output', folder / 'routing-rollback-schedulers-after.json'])
    atomic(NGINX, (folder / 'nginx-before').read_bytes())
    run(['nginx', '-t'], stderr=subprocess.STDOUT)
    run(['systemctl', 'reload', 'nginx'])
    rec['status'] = 'routing_rolled_back'
    save(folder / 'record.json', rec)
    print(json.dumps(dict(status=rec['status'], report_compatibility_preserved=True,
                          ledgers_preserved=True)), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'activate', 'rollback'])
    parser.add_argument('folder', type=Path)
    args = parser.parse_args()
    if args.action == 'prepare':
        prepare(args.folder.resolve())
    elif args.action == 'activate':
        activate(args.folder.resolve())
    else:
        rollback_routing(args.folder.resolve())
