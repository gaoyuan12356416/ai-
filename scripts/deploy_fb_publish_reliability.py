"""GitHub-first scoped health deployment, with code-only rollback and HTTP drain."""
import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import publish_scheduler_health as support
from scripts.deploy_post_monitor_accounting import switch_coordinator, health as coordinator_health

ROOT = Path('/mnt/data-disk/fb-publish-reliability-20261106')
GIT = Path('/mnt/data-disk/pipeline-dispatch/github')
COORD = Path('/opt/pipeline-dispatch/current')
HEALTH = Path('/opt/publish-scheduler-health/current')
TIMER = 'publish-scheduler-check@fb.timer'
TIMER_FILE = Path('/etc/systemd/system') / TIMER
STATIC = [Path('/root/drama_material_service/static'), Path('/usr/share/nginx/html')]
FB_DB = Path('/mnt/data-disk/fb-auto-post-publisher/fb-auto-post.sqlite3')
SOURCE_FILES = ['scripts/publish_scheduler_health.py', 'scripts/fb_short_link_health.py',
                'scripts/deploy_publish_queue_isolation.py', 'scripts/deploy_final_queue_entries.py',
                'scripts/deploy_fb_publish_reliability.py', 'scripts/deploy_post_monitor_accounting.py',
                'scripts/test_fb_support_systemd.py', 'features/pipeline_dispatch/support_health.py',
                'deploy/publish-scheduler-check@fb.timer', 'ops/fb-publish-reliability/monitor.patch',
                'ops/fb-publish-reliability/files.json']
PROTECTED = ['fb-auto-post-service.service', 'fb-video-publish-worker.service',
             'fb-video-pool-scheduler.service', 'fb-group-pool-scheduler.service',
             'pipeline-node-cpu-main.service', 'drama-material-api.service']


def run(args, timeout=60, **kwargs):
    result = subprocess.run(list(map(str, args)), capture_output=True, text=True, timeout=timeout, **kwargs)
    if result.returncode:
        raise RuntimeError('command_failed:' + str(args[0]) + ':' + str(result.returncode))
    return result.stdout.strip()


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def swap(link, target):
    temporary = link.with_name(link.name + '.fb-support-' + str(os.getpid()))
    temporary.symlink_to(target)
    temporary.replace(link)


def copy(source, target):
    target = Path(target)
    temporary = target.with_name(target.name + '.fb-support-' + str(os.getpid()))
    shutil.copy2(source, temporary)
    if target.exists():
        st = target.stat()
        os.chown(temporary, st.st_uid, st.st_gid)
        temporary.chmod(st.st_mode & 0o777)
    temporary.replace(target)


def pids():
    return {unit: row.get('MainPID') for unit, row in support.show(PROTECTED).items()}


def backup_database(source, target):
    with contextlib.closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=5)) as reader:
        with contextlib.closing(sqlite3.connect(target)) as writer:
            reader.backup(writer, pages=256, sleep=.02)
            assert writer.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
    target.chmod(0o600)


def rollback(manifest_path):
    manifest_path = Path(manifest_path).resolve()
    assert manifest_path.is_relative_to(ROOT)
    folder = manifest_path.parent
    m = json.loads(manifest_path.read_text())
    # Check the whole affected set before restoring any part of it.
    assert str(COORD.resolve()) in (m['old_coordinator'], m['new_coordinator']), 'later_coordinator_preserved'
    assert str(HEALTH.resolve()) in (m['old_health'], m['source'], m.get('original_tools_source')), 'later_health_release_preserved'
    for entry in m['static']:
        assert digest(entry['path']) in (entry['old_sha'], entry['new_sha']), 'later_static_change_preserved'
    if TIMER_FILE.exists():
        assert digest(TIMER_FILE) in (m['timer_new_sha'], m.get('timer_old_sha')), 'later_timer_change_preserved'
    if not m['timer_was_active']:
        run(['systemctl', 'stop', TIMER])  # only the observer timer; no publisher
    if not m['timer_was_enabled']:
        run(['systemctl', 'disable', TIMER])
    with support.deployment_guard(['coordinator'], folder / 'rollback-support'):
        if str(COORD.resolve()) == m['new_coordinator']:
            assert not run(['systemctl', 'show', 'pipeline-coordinator.service', '-p', 'RequiredBy', '--value'])
            switch_coordinator(m['old_coordinator'], folder / 'rollback-http-drain.json')
            coordinator_health()
    for entry in m['static']:
        if digest(entry['path']) == entry['new_sha']:
            copy(folder / entry['backup'], entry['path'])
    if str(HEALTH.resolve()) in (m['source'], m.get('original_tools_source')):
        swap(HEALTH, m['old_health'])
    if m['timer_existed']:
        copy(folder / 'timer-before', TIMER_FILE)
    elif TIMER_FILE.exists():
        TIMER_FILE.unlink()
    run(['systemctl', 'daemon-reload'])
    if m['timer_was_enabled']:
        run(['systemctl', 'enable', TIMER])
    if m['timer_was_active']:
        run(['systemctl', 'start', TIMER])
    support.save(folder / 'rollback-result.json', {'ok': True, 'database_restored': False,
                 'at_utc': dt.datetime.now(dt.timezone.utc).isoformat(), 'protected_pids': pids()})
    print(json.dumps({'rolled_back': str(manifest_path), 'business_facts_retained': True}))


def install(source, commit):
    source = Path(source).resolve()
    assert re.fullmatch('[0-9a-f]{40}', commit)
    assert run(['findmnt', '-n', '-o', 'UUID', '/mnt/data-disk']) == '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    assert run(['git', '-C', GIT, 'rev-parse', commit + '^{commit}']) == commit
    assert run(['git', '-C', GIT, 'for-each-ref', '--contains', commit, '--format=%(refname)', 'refs/remotes/origin'])
    for relative in SOURCE_FILES:
        exact = subprocess.check_output(['git', '-C', str(GIT), 'show', commit + ':' + relative])
        assert (source / relative).read_bytes() == exact, 'unreviewed_source:' + relative
    # Existing weak dependency is retained, never converted into Requires=.
    fb = run(['systemctl', 'show', 'fb-auto-post-service.service', '-p', 'Wants', '--value']).split()
    assert 'fb-video-publish-worker.service' in fb, 'worker_weak_dependency_missing'
    assert 'fb-video-publish-worker.service' not in run(['systemctl','show','fb-auto-post-service.service','-p','Requires','--value']).split()
    assert 'fb-auto-post-service.service' in run(['systemctl','show','fb-video-publish-worker.service','-p','After','--value']).split()
    assert not run(['systemctl', 'show', 'pipeline-coordinator.service', '-p', 'RequiredBy', '--value']), 'coordinator_dependents_changed'
    expected = json.loads((source / 'ops/fb-publish-reliability/files.json').read_text())
    tag = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    folder = ROOT / ('deployment-' + tag)
    folder.mkdir(parents=True, mode=0o700)
    old_coordinator = COORD.resolve()
    staged = folder / 'staged'
    for relative, hashes in expected.items():
        live = old_coordinator / relative if relative.startswith('features/') else STATIC[1] / Path(relative).name
        assert digest(live) == hashes['before'], 'live_source_drift:' + relative
        target = staged / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(live, target)
    run(['git', 'apply', '--check', source / 'ops/fb-publish-reliability/monitor.patch'], cwd=staged)
    run(['git', 'apply', source / 'ops/fb-publish-reliability/monitor.patch'], cwd=staged)
    for relative, hashes in expected.items():
        assert digest(staged / relative) == hashes['after'], 'overlay_result_mismatch:' + relative
    candidate = folder / 'coordinator'
    shutil.copytree(old_coordinator, candidate, symlinks=True, ignore=shutil.ignore_patterns('.git','__pycache__','*.pyc'))
    package = candidate / 'features/pipeline_dispatch'
    assert package.resolve().is_relative_to(candidate)
    copy(staged / 'features/pipeline_dispatch/views.py', package / 'views.py')
    copy(source / 'features/pipeline_dispatch/support_health.py', package / 'support_health.py')
    run(['/opt/pipeline-dispatch/coordinator-venv/bin/python', '-B', '-c', 'from features.pipeline_dispatch import views, support_health'], cwd=candidate)
    baseline = pids()
    m = {'commit': commit, 'source': str(source), 'old_health': str(HEALTH.resolve()),
         'old_coordinator': str(old_coordinator), 'new_coordinator': str(candidate),
         'protected_pids_before': baseline, 'static': [], 'timer_existed': TIMER_FILE.exists(),
         'timer_new_sha': digest(source / 'deploy/publish-scheduler-check@fb.timer'),
         'timer_was_active': subprocess.run(['systemctl','is-active','--quiet',TIMER]).returncode == 0,
         'timer_was_enabled': subprocess.run(['systemctl','is-enabled','--quiet',TIMER], capture_output=True).returncode == 0}
    if TIMER_FILE.exists():
        shutil.copy2(TIMER_FILE, folder / 'timer-before')
        m['timer_old_sha'] = digest(TIMER_FILE)
    for index, static_root in enumerate(STATIC):
        for name in ('pipeline-monitor.js', 'pipeline-monitor.html'):
            path = static_root / name
            assert digest(path) == expected['static/' + name]['before'], 'static_copy_drift:' + str(path)
            saved = 'static-' + str(index) + '-' + name
            shutil.copy2(path, folder / saved)
            m['static'].append(dict(path=str(path), backup=saved, old_sha=digest(path), new_sha=digest(staged / 'static' / name)))
    backup_database(FB_DB, folder / 'fb-before.sqlite3')
    backup_database(Path('/mnt/data-disk/pipeline-dispatch/dispatch.sqlite3'), folder / 'dispatch-before.sqlite3')
    manifest = folder / 'manifest.json'
    support.save(manifest, m)
    rollback_file = folder / 'rollback.sh'
    rollback_file.write_text("#!/bin/sh\nset -eu\nexec /usr/bin/python3 '" + str(source / 'scripts/deploy_fb_publish_reliability.py') + "' --rollback '" + str(manifest) + "'\n")
    rollback_file.chmod(0o700)
    proof = run(['/usr/bin/python3', source / 'scripts/test_fb_support_systemd.py'])
    (folder / 'isolated-systemd-proof.json').write_text(proof + '\n')
    try:
        assert COORD.resolve() == old_coordinator and str(HEALTH.resolve()) == m['old_health'], 'concurrent_release'
        assert digest(COORD / 'features/pipeline_dispatch/views.py') == expected['features/pipeline_dispatch/views.py']['before']
        swap(HEALTH, source)
        copy(source / 'deploy/publish-scheduler-check@fb.timer', TIMER_FILE)
        run(['systemctl', 'daemon-reload'])
        run(['systemctl', 'enable', '--now', TIMER])
        # The check is read-only and reports errors without touching publishers.
        run(['systemctl', 'start', 'publish-scheduler-check@fb.service'])
        with support.deployment_guard(['coordinator'], folder / 'coordinator-support'):
            switch_coordinator(str(candidate), folder / 'http-drain.json')
            coordinator_health()
        for entry in m['static']:
            assert digest(entry['path']) == entry['old_sha'], 'concurrent_static_change'
            copy(staged / 'static' / Path(entry['path']).name, entry['path'])
        assert pids() == baseline, 'protected_service_pid_changed'
        result = dict(ok=True, commit=commit, backup=str(folder), rollback=str(rollback_file),
                      protected_pids_unchanged=True, protected_pids_after=pids(),
                      static_verified=all(digest(r['path']) == r['new_sha'] for r in m['static']),
                      weak_worker_dependency_verified=True,
                      at_utc=dt.datetime.now(dt.timezone.utc).isoformat())
        support.save(folder / 'result.json', result)
        print(json.dumps(result))
    except BaseException:
        rollback(manifest)
        raise


def upgrade_tools(source, commit, manifest_path):
    """Install a reviewed deployment-tool fix without another runtime switch."""
    source, manifest_path = Path(source).resolve(), Path(manifest_path).resolve()
    m = json.loads(manifest_path.read_text())
    assert manifest_path.is_relative_to(ROOT) and HEALTH.resolve() == Path(m['source'])
    assert re.fullmatch('[0-9a-f]{40}', commit)
    assert run(['git','-C',GIT,'for-each-ref','--contains',commit,'--format=%(refname)','refs/remotes/origin'])
    run(['git','-C',GIT,'merge-base','--is-ancestor',m['commit'],commit])
    for relative in SOURCE_FILES + ['scripts/recover_fb_support_switch_upload.py','scripts/test_fb_coordinator_switch.py']:
        assert (source/relative).read_bytes()==subprocess.check_output(['git','-C',str(GIT),'show',commit+':'+relative])
    rollback_file=manifest_path.parent/'rollback.sh'
    shutil.copy2(rollback_file,manifest_path.parent/'rollback-before-tools.sh')
    shutil.copy2(manifest_path,manifest_path.parent/'manifest-before-tools.json')
    before=pids()
    m['original_tools_source']=m['source'];m['source']=str(source);m['tools_commit']=commit
    rollback_file.write_text("#!/bin/sh\nset -eu\nexec /usr/bin/python3 '"+str(source/'scripts/deploy_fb_publish_reliability.py')+"' --rollback '"+str(manifest_path)+"'\n")
    rollback_file.chmod(0o700)
    support.save(manifest_path,m)
    swap(HEALTH,source)
    assert before==pids()
    support.save(manifest_path.parent/'tools-upgrade.json',dict(commit=commit,source=str(source),services_restarted=False,protected_pids_unchanged=True))
    print(json.dumps({'tools_updated':commit,'services_restarted':False,'rollback_updated':str(rollback_file)}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source')
    parser.add_argument('--commit')
    parser.add_argument('--rollback')
    parser.add_argument('--tools-only', help='Existing deployment manifest to update')
    args = parser.parse_args()
    if args.rollback:
        rollback(args.rollback)
    elif args.tools_only:
        upgrade_tools(args.source,args.commit,args.tools_only)
    else:
        install(args.source, args.commit)
