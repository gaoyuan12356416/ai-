"""Scoped GitHub overlays for FB/X hit batches; never restore business ledgers."""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time

from scripts.deploy_pipeline_runtime import atomic_copy, preserve_tree_owners, verify_source

DATA = Path('/mnt/data-disk')
LANES = {
    'x': ('/opt/x-tag-publisher/current', ['x-tag-auto.service', 'x-tag-post.service'], [18843, 8821]),
    'fb': ('/opt/fb-auto-post/current', ['fb-auto-post-service.service'], [18835]),
    'coordinator': ('/opt/pipeline-dispatch/current', ['pipeline-coordinator.service'], [18846]),
}
DATABASES = {'x-auto': DATA/'x-tag-publisher/auto/x-auto-post.sqlite3',
             'x-post': DATA/'x-tag-publisher/posts.sqlite3',
             'fb': DATA/'fb-auto-post-publisher/fb-auto-post.sqlite3',
             'dispatch': DATA/'pipeline-dispatch/dispatch.sqlite3'}


def run(args, **kwargs):
    result = subprocess.run([str(a) for a in args], capture_output=True, check=True, **kwargs)
    return result.stdout.decode().strip()


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest() if Path(path).is_file() else ''


def save(folder, manifest):
    path = folder/'manifest.json'
    temporary = folder/'manifest.next'
    temporary.write_text(json.dumps(manifest, indent=2)+'\n')
    temporary.chmod(0o600)
    temporary.replace(path)


def show(unit, field='ActiveState'):
    return run(['systemctl', 'show', unit, '-p', field, '--value'])


def prepare(folder, x_source, fb_source):
    assert DATA.is_mount() and folder.is_relative_to(DATA)
    assert run(['findmnt', '-n', '-o', 'UUID', DATA]) == '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    sources = {'x': x_source.resolve(), 'fb': fb_source.resolve()}
    spec = json.loads((x_source/'deploy/hit-publish-overlay.json').read_text())
    commits = {key: verify_source(source, 'HEAD') for key, source in sources.items()}
    folder.mkdir(parents=True, exist_ok=False, mode=0o755)
    (folder/'backups').mkdir(mode=0o700)
    manifest = dict(commits=commits, lanes={}, static={}, status='preparing')
    for lane, (root_name, units, ports) in LANES.items():
        root = Path(root_name)
        source_key = 'fb' if lane == 'fb' else 'x'
        source = sources[source_key]
        files = ['features/x_tag_publish/dispatch.py'] if lane == 'coordinator' else spec[source_key]['files']
        candidate = folder/'candidates'/lane
        shutil.copytree(root.resolve(), candidate, symlinks=True,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.git'))
        preserve_tree_owners(root.resolve(), candidate)
        item = dict(previous_root=str(root.resolve()), candidate=str(candidate), files={})
        manifest['lanes'][lane] = item
        for relative in files:
            target, live = candidate/relative, root/relative
            original = subprocess.run(['git', 'show', spec[source_key]['base']+':'+relative], cwd=source, capture_output=True)
            assert (live.read_bytes().replace(b'\r\n', b'\n') == original.stdout if original.returncode == 0 else not live.exists()), 'live_file_changed:'+lane+'/'+relative
            assert target.resolve().is_relative_to(candidate.resolve()), 'overlay_symlink_escape'
            atomic_copy(source/relative, target)
            if relative.endswith('.py'):
                compile(target.read_bytes(), relative, 'exec')
            item['files'][relative] = dict(before=digest(live), after=digest(target))
            if relative.startswith('static/'):
                for static_root in ('/root/drama_material_service/static', '/usr/share/nginx/html'):
                    public = Path(static_root)/Path(relative).name
                    if str(public) in manifest['static']:
                        assert manifest['static'][str(public)]['after'] == digest(target)
                        continue
                    backup = folder/'backups'/('static-'+str(len(manifest['static'])))
                    if public.exists():
                        shutil.copy2(public, backup)
                    manifest['static'][str(public)] = dict(source=str(target), backup=str(backup), before=digest(public), after=digest(target))
    for name, path in DATABASES.items():
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)) as src, closing(sqlite3.connect(folder/'backups'/(name+'.sqlite3'))) as dst:
            src.backup(dst)
            assert dst.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
    manifest['status'] = 'prepared'
    save(folder, manifest)
    print(json.dumps(dict(status='prepared', commits=commits, files={k:len(v['files']) for k,v in manifest['lanes'].items()})), flush=True)


def idle_readbacks(pid):
    spy = '/mnt/data-disk/fb-video-publish/maintenance/inspect-tools/bin/py-spy'
    threads = json.loads(run([spy, 'dump', '--pid', pid, '--json'], timeout=10))
    return sum(1 for t in threads if t.get('thread_name') in ('fb-readback-0', 'fb-readback-1')
               and len(t.get('frames', [])) >= 3 and t['frames'][0]['name'] == 'wait'
               and t['frames'][1]['name'] == 'get' and t['frames'][1]['filename'].endswith('/queue.py')
               and t['frames'][2]['name'] == '_reconcile_worker'
               and t['frames'][2]['filename'].endswith('/features/pipeline_dispatch/fb_native.py'))


def drained(unit, lane):
    pid = show(unit, 'MainPID')
    sockets = [line for line in run(['ss', '-Hntp']).splitlines() if 'pid='+pid+',' in line]
    listeners = [line for line in run(['ss', '-Hlntp']).splitlines() if 'pid='+pid+',' in line]
    threads = len(list(Path('/proc', pid, 'task').iterdir()))
    idle = idle_readbacks(pid) if lane == 'fb' else 0
    return dict(pid=pid, sockets=len(sockets), accept_queue=sum(int(line.split()[1]) for line in listeners), threads=threads, idle=idle)


def triggers(lane):
    patterns = ['x-tag-*'] if lane == 'x' else ['fb-auto-post-*', 'fb-video-publish-*'] if lane == 'fb' else []
    if not patterns:
        return []
    rows = run(['systemctl', 'list-units', '--all', '--plain', '--no-legend', *patterns]).splitlines()
    return [r.split()[0] for r in rows if r.split()[0].endswith(('.timer', '.path')) and r.split()[2] == 'active']


def activate(folder, lane, rollback=False):
    manifest = json.loads((folder/'manifest.json').read_text())
    item = manifest['lanes'][lane]
    root_name, units, ports = LANES[lane]
    root = Path(root_name)
    expected = item['candidate'] if rollback else item['previous_root']
    target = item['previous_root'] if rollback else item['candidate']
    assert str(root.resolve()) == expected, 'release_changed:'+lane
    for relative, record in item['files'].items():
        assert digest(root/relative) == record['after' if rollback else 'before'], 'file_changed:'+relative
        assert digest(Path(target)/relative) == record['before' if rollback else 'after'], 'candidate_changed:'+relative
    states, gates, watchdogs = triggers(lane), [], []
    item['trigger_states'] = states
    save(folder, manifest)
    old_pids = {unit:show(unit, 'MainPID') for unit in units}
    try:
        for unit in states:
            run(['systemctl', 'stop', unit])
        # X has verified non-daemon graceful shutdown. Drain only accepted
        # HTTP work for FB/coordinator; remote jobs keep their leases/receipts.
        if lane != 'x':
            for port in ports:
                marker = 'hit-publish-'+lane+'-'+str(os.getpid())
                rule = ['-p','tcp','--dport',str(port),'-m','conntrack','--ctstate','NEW','-m','comment','--comment',marker,'-j','REJECT','--reject-with','tcp-reset']
                run(['systemd-run','--quiet','--unit='+marker,'--on-active=600s','--','/usr/sbin/iptables','-D','INPUT',*rule])
                watchdogs.append(marker+'.timer')
                run(['iptables','-I','INPUT','1',*rule]); gates.append(rule)
            deadline = time.monotonic()+300
            while True:
                proof = drained(units[0], lane)
                if not proof['sockets'] and not proof['accept_queue'] and proof['threads']-proof['idle'] <= (2 if lane == 'coordinator' else 1):
                    time.sleep(.3)
                    if drained(units[0], lane) == proof:
                        break
                assert time.monotonic() < deadline, 'accepted_work_still_running:'+lane
                time.sleep(.5)
            item['drain_proof'] = proof
        for unit in units:
            run(['systemctl', 'stop', unit])
        assert str(root.resolve()) == expected, 'release_changed_during_drain'
        temporary = root.with_name(root.name+'.hit-next')
        assert not temporary.exists() and not temporary.is_symlink()
        temporary.symlink_to(target, target_is_directory=True); temporary.replace(root)
        item['applied'] = not rollback
        save(folder, manifest)
    finally:
        try:
            for unit in reversed(units):
                if show(unit) != 'active':
                    run(['systemctl', 'start', unit])
        finally:
            for rule in gates:
                subprocess.run(['iptables','-D','INPUT',*rule], check=False, capture_output=True)
            for unit in watchdogs:
                subprocess.run(['systemctl','stop',unit], check=False, capture_output=True)
            for unit in states:
                subprocess.run(['systemctl','start',unit], check=False, capture_output=True)
    import urllib.request
    for port in ports:
        for attempt in range(30):
            try:
                with urllib.request.urlopen('http://127.0.0.1:%s/health'%port, timeout=2) as response:
                    assert response.status == 200
                break
            except Exception:
                if attempt == 29: raise
                time.sleep(.5)
    item['pids'] = dict(before=old_pids, after={unit:show(unit,'MainPID') for unit in units})
    save(folder, manifest)
    print(json.dumps(dict(lane=lane, activated=not rollback, pids=item['pids'])), flush=True)


def public_files(folder, rollback=False):
    manifest = json.loads((folder/'manifest.json').read_text())
    for name, record in manifest['static'].items():
        target = Path(name)
        assert digest(target) == record['after' if rollback else 'before'], 'static_changed:'+name
        source = Path(record['backup'] if rollback else record['source'])
        if source.exists(): atomic_copy(source, target)
        elif rollback and not record['before']: target.unlink(missing_ok=True)
    manifest['static_applied'] = not rollback
    save(folder, manifest)
    print(json.dumps(dict(static_activated=not rollback)), flush=True)


def rollback(folder, ui_only=False):
    manifest = json.loads((folder/'manifest.json').read_text())
    if manifest.get('static_applied'): public_files(folder, True)
    if ui_only: return
    # Older execution code cannot honor the new frozen future/material policy.
    # Keep its compatibility until these exact new batches are terminal.
    queries = {
        'x-auto': "SELECT COUNT(*) FROM x_auto_task t JOIN x_auto_run r ON r.id=t.run_id WHERE r.blacklist_snapshot_json LIKE '%profile_one_to_one_release_minus_30m_v1%' AND t.status NOT IN ('published','failed','no_candidate','canceled','cancelled','skipped')",
        'fb': "SELECT COUNT(*) FROM fb_auto_task t JOIN fb_auto_run r ON r.id=t.run_id WHERE r.config_json LIKE '%profile_one_to_one_release_minus_30m_v1%' AND t.status NOT IN ('published','failed','skipped','cancelled')"}
    for name, query in queries.items():
        with closing(sqlite3.connect(DATABASES[name].as_uri()+'?mode=ro',uri=True)) as conn:
            assert conn.execute(query).fetchone()[0] == 0, 'new_hit_tasks_active_UI_restored_keep_execution:'+name
    for lane in ('x', 'fb', 'coordinator'):
        if manifest['lanes'][lane].get('applied'): activate(folder,lane,True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare','activate','static','rollback'])
    parser.add_argument('--folder', type=Path, required=True)
    parser.add_argument('--x-source', type=Path)
    parser.add_argument('--fb-source', type=Path)
    parser.add_argument('--lane', choices=LANES)
    parser.add_argument('--ui-only', action='store_true')
    args=parser.parse_args(); folder=args.folder.resolve()
    if args.action == 'prepare': prepare(folder,args.x_source,args.fb_source)
    elif args.action == 'activate': activate(folder,args.lane)
    elif args.action == 'static': public_files(folder)
    else: rollback(folder,args.ui_only)


if __name__ == '__main__': main()
