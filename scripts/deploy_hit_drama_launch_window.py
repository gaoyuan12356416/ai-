"""Scoped code overlay for FB/X explicit-drama age limits, with safe rollback."""
import argparse
from contextlib import closing
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import sys

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
from scripts import deploy_hit_drama_multi_id as static_release

LANES = {
    'fb': (Path('/opt/fb-auto-post/current'), 'features/fb_auto_posts/repositories.py',
           'deffa57ec692ac0e79e9701bc219e63c02985f7079fe667b0fbc1094a792d1e4',
           'fb-auto-post-service.service', Path('/mnt/data-disk/fb-auto-post-publisher/fb-auto-post.sqlite3')),
    'x': (Path('/opt/x-tag-publisher/current'), 'features/x_tag_publish/selection.py',
          '4e1341a19503c07c303a8067172d625e93c2e6d5776c789670f5df47d91ce872',
          'x-tag-auto.service', Path('/mnt/data-disk/x-tag-publisher/auto/x-auto-post.sqlite3')),
}
HEALTH = '/opt/publish-scheduler-health/current/scripts/publish_scheduler_health.py'
TEST = 'scripts/test_hit_drama_launch_window.py'


def run(*args, timeout=60):
    return subprocess.check_output(list(map(str, args)), text=True, timeout=timeout).strip()


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def config_hash(db, lane):
    with closing(sqlite3.connect(db.as_uri() + '?mode=ro', uri=True)) as conn:
        table = 'fb_auto_template' if lane == 'fb' else 'x_auto_template_version'
        columns = 'id,config_json,current_version' if lane == 'fb' else 'template_id,version,config_json'
        rows = conn.execute('SELECT ' + columns + ' FROM ' + table + ' ORDER BY 1,2').fetchall()
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode()).hexdigest()


def services():
    units = [row[3] for row in LANES.values()] + ['drama-material-api.service', 'x-tag-post.service',
        'x-tag-gateway.service', 'pipeline-cos-native-fb.service', 'pipeline-cos-native-x-tag.service']
    return {unit: run('systemctl', 'show', unit, '--property=MainPID,ActiveState,SubState') for unit in units}


def prepare(folder):
    assert run('findmnt', '-rn', '-o', 'UUID', '--mountpoint', '/mnt/data-disk') == '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    assert folder.resolve().is_relative_to('/mnt/data-disk')
    assert not run('git', '-C', SOURCE, 'status', '--porcelain')
    commit = run('git', '-C', SOURCE, 'rev-parse', 'HEAD')
    assert run('git', '-C', SOURCE, 'branch', '-r', '--contains', commit)
    folder.mkdir(parents=True, exist_ok=False)
    folder.chmod(0o700)
    info = dict(commit=commit, files={}, lanes={}, config_before={}, services_before=services())
    for lane, (root, name, before, unit, db) in LANES.items():
        assert digest(root / name) == before, 'live_selector_changed:' + lane
        candidate = folder / ('candidate-' + lane)
        shutil.copytree(root.resolve(), candidate, symlinks=True,
            ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.git'))
        shutil.copy2(SOURCE / name, candidate / name)
        shutil.copy2(SOURCE / TEST, candidate / TEST)
        result = subprocess.run([sys.executable, '-B', TEST, '--platform', lane], cwd=candidate,
                                capture_output=True, text=True, timeout=90)
        (folder / ('tests-' + lane + '.txt')).write_text(result.stdout + result.stderr)
        if result.returncode:
            print(result.stdout + result.stderr)
            raise RuntimeError('candidate_tests_failed:' + lane)
        info['lanes'][lane] = dict(root=str(root.resolve()), unit=unit, candidate=str(candidate))
        info['config_before'][lane] = config_hash(db, lane)
        with closing(sqlite3.connect(db.as_uri() + '?mode=ro', uri=True)) as src, \
                closing(sqlite3.connect(folder / ('ledger-before-' + lane + '.sqlite3'))) as dst:
            src.backup(dst)
        (folder / ('ledger-before-' + lane + '.sqlite3')).chmod(0o600)
    static_release.BASELINE = 'acc231761234e28e70e1c465e57def26344d8e1ddc7c9185c650d6f8f62316a7'
    changes = static_release.plan()
    for root, name, *_ in LANES.values():
        changes[(root / name).resolve()] = (SOURCE / name).read_bytes()
    for index, (path, data) in enumerate(changes.items()):
        backup, next_file = folder / (str(index) + '.before'), folder / (str(index) + '.next')
        shutil.copy2(path, backup)
        next_file.write_bytes(data)
        metadata = path.stat()
        info['files'][str(path)] = dict(before=digest(path), after=digest(next_file),
            backup=str(backup), next=str(next_file), mode=stat.S_IMODE(metadata.st_mode),
            uid=metadata.st_uid, gid=metadata.st_gid)
    save(folder / 'manifest.json', info)
    print(json.dumps(dict(status='prepared', commit=commit, files=len(changes), tests='fb+x passed')))


def switch(folder, rollback=False):
    info = json.loads((folder / 'manifest.json').read_text())
    for lane, values in info['lanes'].items():
        assert str(LANES[lane][0].resolve()) == values['root'], 'later_runtime_present:' + lane
    for name, entry in info['files'].items():
        assert digest(name) in {entry['before'], entry['after']}, 'preserve_later_edit:' + name
        selected = entry['backup' if rollback else 'next']
        assert digest(selected) == entry['before' if rollback else 'after']
    action = 'rollback' if rollback else 'apply'
    for lane in LANES:
        run(sys.executable, HEALTH, 'snapshot', '--lane', lane, '--output', folder / (action + '-schedulers-before-' + lane + '.json'))
    for name, entry in info['files'].items():
        static_release.atomic(Path(name), Path(entry['backup' if rollback else 'next']).read_bytes(), entry)
    # The two control owners drain their own accepted HTTP work normally.
    # Native upload/render/publish services and the main API are untouched.
    for _, _, _, unit, _ in LANES.values():
        run('systemctl', 'restart', '--no-block', unit)
    info['requested_action'] = action
    save(folder / 'manifest.json', info)
    print(json.dumps(dict(status='switch_requested', action=action, commit=info['commit'],
                          units=[row[3] for row in LANES.values()])))


def verify(folder):
    info = json.loads((folder / 'manifest.json').read_text())
    rollback = info.get('requested_action') == 'rollback'
    action = 'rollback' if rollback else 'apply'
    for lane, (_, name, _, unit, db) in LANES.items():
        assert run('systemctl', 'show', unit, '-p', 'ActiveState', '--value') == 'active', 'owner_not_active:' + lane
        run('curl', '--max-time', '5', '-fsS', 'http://127.0.0.1:' + ('18835' if lane == 'fb' else '18843') + '/health')
        run(sys.executable, HEALTH, 'restore', '--baseline', folder / (action + '-schedulers-before-' + lane + '.json'),
            '--wait-seconds', '25', '--output', folder / (action + '-schedulers-after-' + lane + '.json'))
        assert config_hash(db, lane) == info['config_before'][lane], 'template_configuration_changed:' + lane
    for name, entry in info['files'].items():
        assert digest(name) == entry['before' if rollback else 'after'], 'installed_hash_mismatch:' + name
    result = dict(status='rolled_back' if rollback else 'active', commit=info['commit'],
        files=len(info['files']), templates_unchanged=True, services_after=services(),
        restarted_units=[row[3] for row in LANES.values()], real_test_posts=0, historical_replays=0)
    save(folder / ('verification-rollback.json' if rollback else 'verification.json'), result)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'apply', 'rollback', 'verify'])
    parser.add_argument('--folder', type=Path, required=True)
    args = parser.parse_args()
    folder = args.folder.resolve()
    if args.action == 'prepare': prepare(folder)
    elif args.action == 'verify': verify(folder)
    else: switch(folder, args.action == 'rollback')
