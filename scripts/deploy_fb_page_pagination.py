"""GitHub-first local Page list overlay onto the current composite FB runtime.

Reuses the existing reviewed admission/drain/scheduler switch helpers. Rollback
restores code/static assets only; no ledger, task, receipt or secret restoration.
"""
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time

SOURCE = Path(__file__).resolve().parents[1]
BASE = '0b0e4304d18aacbebf8cddd64c67a63727a67821'
SERVICE = 'features/fb_video_publish/service.py'
JS = 'static/fb-video-publish.js'
HTML = ('fb-pages.html', 'fb-video-publish.html', 'fb-video-publish-records.html')
MAIN = Path('/root/drama_material_service')


def prepare(folder):
    assert release.run('findmnt', '-rn', '-o', 'UUID', '--mountpoint', '/mnt/data-disk') == '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    assert shutil.disk_usage('/mnt/data-disk').free > 3 * 1024**3
    commit = release.run('git', '-C', SOURCE, 'rev-parse', 'HEAD')
    assert not release.run('git', '-C', SOURCE, 'status', '--porcelain'), 'clean_source_required'
    assert release.run('git', '-C', SOURCE, 'branch', '-r', '--contains', commit), 'pushed_source_required'
    previous = Path('/opt/fb-auto-post/current').resolve()
    before = runtime_image(previous)
    folder.mkdir(parents=True, exist_ok=False)
    folder.chmod(0o755)
    candidate = folder / 'candidate'
    shutil.copytree(previous, candidate, symlinks=True, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.git'))
    preserve_tree_owners(previous, candidate)
    info = dict(platform='fb', commit=commit, source=str(SOURCE), previous=str(previous), candidate=str(candidate),
                previous_image=before, files={}, static={}, status='prepared', helpers=helper_hashes())

    def baseline(name, path):
        expected = subprocess.check_output(['git', '-C', str(SOURCE), 'show', BASE+':'+name])
        assert path.read_bytes() == expected, 'concurrent_file_change:'+str(path)

    def external(path, content):
        idx = str(len(info['static']))
        old, new = folder/('file-'+idx+'.old'), folder/('file-'+idx+'.new')
        shutil.copy2(path, old)
        new.write_bytes(content)
        info['static'][str(path)] = dict(before=release.sha(old), after=hashlib.sha256(content).hexdigest(), backup=str(old), next=str(new))

    for name in (SERVICE, JS):
        baseline(name, previous/name)
        release.atomic(candidate/name, (SOURCE/name).read_bytes())
        info['files'][name] = dict(before=release.sha(previous/name), after=release.sha(candidate/name))
    baseline(SERVICE, MAIN/SERVICE)
    external(MAIN/SERVICE, (SOURCE/SERVICE).read_bytes())
    version = release.sha(SOURCE/JS)[:12]
    for root in (candidate/'static', MAIN/'static', Path('/usr/share/nginx/html')):
        if root != candidate/'static':
            baseline(JS, root/'fb-video-publish.js')
            external(root/'fb-video-publish.js', (SOURCE/JS).read_bytes())
        for name in HTML:
            path = root/name
            # Preserve every live HTML difference, modifying only this asset URL.
            text, count = re.subn(r'(/fb-video-publish\.js)(?:\?[^"\'\s>]*)?', r'\1?v='+version, path.read_text())
            assert count == 1, 'script_anchor_changed:'+str(path)
            if root == candidate/'static':
                release.atomic(path, text.encode())
                relative = 'static/'+name
                info['files'][relative] = dict(before=release.sha(previous/relative), after=release.sha(path))
            else:
                external(path, text.encode())
    tests = ('test_fb_shared_manual_pages', 'test_fb_page_local_list', 'test_fb_page_pagination')
    for name in tests:
        release.atomic(candidate/'tests'/(name+'.py'), (SOURCE/'tests'/(name+'.py')).read_bytes())
    proof = subprocess.run([sys.executable, '-B', '-m', 'unittest', *['tests.'+n for n in tests], '-v'], cwd=candidate, capture_output=True, text=True, timeout=120)
    (folder/'tests.txt').write_text(proof.stdout+proof.stderr)
    assert proof.returncode == 0, 'candidate_tests_failed'
    release.run('node', '--check', candidate/JS)
    compile((candidate/SERVICE).read_bytes(), SERVICE, 'exec')
    assert runtime_image(previous) == before, 'concurrent_release_changed_restage'
    info['candidate_image'] = runtime_image(candidate)
    info['main_api_pid'] = fb.show('drama-material-api.service', 'MainPID')
    release.save(folder/'manifest.json', info)
    print(json.dumps(dict(status='prepared', commit=commit, candidate=str(candidate))), flush=True)


def helper_hashes():
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in [
        Path(release.__file__), Path(fb.__file__), Path(owner_module.__file__),
        Path(image_module.__file__), Path(owners_module.__file__)]}


def activate(folder, rollback=False):
    with (folder/'switch.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        info = json.loads((folder/'manifest.json').read_text())
        assert info['helpers'] == helper_hashes(), 'deployment_helpers_changed'
        for label in ('previous', 'candidate'):
            assert runtime_image(Path(info[label])) == info[label+'_image'], 'runtime_changed_restage:'+label
        release.verify_current(info, rollback)
        for path, row in info['static'].items():
            assert release.sha(path) == row['after' if rollback else 'before'], 'external_file_changed:'+path
        before = fb.configuration()
        opened = 'fb' in before['enabled_channels']
        prep_only = 'fb' in before.get('preparation_only_channels', [])
        original_active, original_timers = fb.native_active, fb.TIMERS
        fb.native_active = owner_module.owner_active
        fb.TIMERS = [u for u in original_timers if u not in {
            'fb-auto-post-effects.timer', 'fb-auto-post-metric-hourly.timer', 'fb-auto-post-metric-repair.timer'}]
        active = fb.native_active()
        hold = bool(active and opened and not prep_only)
        audit = dict(started_at=datetime.now(timezone.utc).isoformat(), owner_at_start=active, status='draining')
        receipt = folder/('rollback-admission.json' if rollback else 'apply-admission.json')
        release.save(receipt, audit)
        def interrupted(*_args):
            raise SystemExit('switch interrupted; restoring admission')
        signal.signal(signal.SIGTERM, interrupted)
        try:
            if hold:
                fb.fb_enabled(True, True)
            while active:
                audit['owner_active'] = active
                release.save(receipt, audit)
                print('Waiting for', active, 'affected FB owner operations', flush=True)
                time.sleep(2)
                active = fb.native_active()
            release.activate('fb', folder, rollback, defer_plan_read=False)
        finally:
            if hold:
                fb.fb_enabled(True, False)
            fb.native_active, fb.TIMERS = original_active, original_timers
            after = fb.configuration()
            audit.update(status='finished', ended_at=datetime.now(timezone.utc).isoformat(),
                         final_enabled='fb' in after['enabled_channels'],
                         final_preparation_only='fb' in after.get('preparation_only_channels', []))
            release.save(receipt, audit)
        release.health(18835)
        destination = Path(info['previous' if rollback else 'candidate'])
        pid = fb.show('fb-auto-post-service.service', 'MainPID')
        assert Path('/opt/fb-auto-post/current').resolve() == destination
        assert Path('/proc', pid, 'cwd').resolve() == destination
        health = json.loads((folder/'schedulers-after.json').read_text())
        assert health['ok'], 'scheduler_restore_not_healthy'
        assert fb.show('drama-material-api.service', 'MainPID') == info['main_api_pid'], 'unexpected_main_api_restart'
        print(json.dumps(dict(status='verified', main_api_pid_unchanged=True, schedulers_ok=True)), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'apply', 'rollback'))
    parser.add_argument('--folder', required=True, type=Path)
    parser.add_argument('--helpers', type=Path, default=Path('/mnt/data-disk/fb-shared-manual-pages-20261120/source'))
    args = parser.parse_args()
    # This is the existing, deployed helper set, not a new runtime overlay.
    sys.path.insert(0, str(args.helpers.resolve()))
    from scripts import deploy_hit_drama as release
    from scripts import deploy_fb_skipped_recheck as fb
    from scripts import deploy_fb_profile_priority as owner_module
    from scripts import deploy_mobo_upcoming as image_module
    from scripts import deploy_pipeline_runtime as owners_module
    runtime_image = image_module.runtime_image
    preserve_tree_owners = owners_module.preserve_tree_owners
    release.SOURCE = SOURCE
    folder = args.folder.resolve()
    assert folder.is_relative_to('/mnt/data-disk')
    if args.action == 'prepare':
        prepare(folder)
    else:
        activate(folder, args.action == 'rollback')
