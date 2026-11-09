#!/usr/bin/env python3
"""Upgrade only tag portraits and the independent publisher; preserve all ledgers."""
import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import os
import time

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
from scripts import deploy_x_tag_publish as lane
from features.x_tag_revenue.refresh import refresh, WAREHOUSE_ROOT
from features.x_tag_revenue.tags import VERSION

PORTRAITS = Path('/mnt/data-disk/x-channel-tag-revenue')
BACKUPS = PORTRAITS / 'semantic-backups'
API = 'x-channel-tag-revenue.service'
# Metric collection does not execute publishing or import selection logic.
TRIGGERS = tuple(v for v in lane.TRIGGERS if v != 'x-tag-metric.timer')
WORKERS = tuple(v+'.service' for v in lane.WORKERS if v != 'x-tag-metric')


def active(name):
    return lane.cmd('systemctl', 'is-active', name, check=False) in ('active', 'activating')


def record(path, info):
    lane.atomic(path, json.dumps(info, indent=2).encode())


@contextmanager
def refresh_lock():
    with (PORTRAITS / 'refresh.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def quiet_lane(info):
    lane.cmd('systemctl', 'stop', *TRIGGERS)
    # Let only this lane's already-running timer requests finish; never kill them.
    deadline = time.monotonic()+45
    while any(active(name) for name in WORKERS):
        if time.monotonic() > deadline:
            raise RuntimeError('new_lane_workers_active:retry_after_their_completion')
        time.sleep(1)
    for path, sql in [(lane.ROOT/'posts.sqlite3', "SELECT COUNT(*) FROM x_post_queue WHERE status='publishing'"),
                      (lane.ROOT/'auto/x-auto-post.sqlite3', "SELECT COUNT(*) FROM x_auto_task WHERE claim_token<>''")]:
        with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True)) as db:
            if db.execute(sql).fetchone()[0]:
                raise RuntimeError('new_lane_in_flight:preserve_execution_and_retry_later')


def restore_triggers(info):
    selected = [name for name, enabled in info['triggers_active'].items() if enabled]
    if selected:
        lane.cmd('systemctl', 'start', *selected)


def switch_current(target):
    nxt = lane.CURRENT.with_name('current.semantic-next')
    nxt.unlink(missing_ok=True)
    nxt.symlink_to(target)
    nxt.replace(lane.CURRENT)


def reload_services():
    # No original publishing, main API, or warehouse service is restarted.
    for name, port, path in [(lane.PARENTS[0],8821,'/health'), (lane.PARENTS[1],18843,'/health'),
                             (lane.PARENTS[2],8898,'/healthz'), (API,8896,'/healthz')]:
        lane.cmd('systemctl', 'restart', name, timeout=45)
        lane.health(port, path)


def restore_files(info):
    if str(lane.CURRENT.resolve()) not in (info['release'], info['previous_current']):
        raise RuntimeError('current_release_changed')
    for target,row in info['files'].items():
        if lane.sha(target) not in (row['before'],row['after']):
            raise RuntimeError('rollback_target_changed:'+target)
    for target,row in info['files'].items():
        if row['before'] is None: Path(target).unlink(missing_ok=True)
        else: lane.atomic(target,Path(row['backup']).read_bytes())
    pointer = info['previous_pointer']
    lane.atomic(PORTRAITS/pointer['file'],(Path(info['backup'])/'portrait-before.json').read_bytes())
    record(PORTRAITS/'current.json',pointer)
    record(PORTRAITS/'status.json',info['previous_status'])
    switch_current(info['previous_current'])
    reload_services()


def install():
    commit = lane.cmd('git','-C',str(SOURCE),'rev-parse','HEAD')
    if lane.cmd('git','-C',str(SOURCE),'status','--porcelain') or not lane.cmd('git','-C',str(SOURCE),'branch','-r','--contains',commit):
        raise RuntimeError('verified_github_source_required')
    release = lane.ROOT/'releases'/commit
    lane.build(lane.LEGACY_SOURCE, SOURCE, release)
    lane.cmd('/usr/bin/python3','-B',str(SOURCE/'scripts/test_x_tag_revenue.py'),timeout=120)
    lane.cmd('/usr/bin/python3','-B',str(SOURCE/'scripts/test_x_tag_publish.py'),
             env=dict(os.environ,X_TAG_TEST_RUNTIME=str(release)),timeout=180)
    staging = PORTRAITS/'semantic-staging'/commit
    result = refresh(staging, WAREHOUSE_ROOT, str(lane.MAIN/'.env'))
    if result['status'] != 'ok':
        raise RuntimeError('recomputed_portrait_required')
    next_pointer = json.loads((staging/'current.json').read_text())
    next_snapshot = json.loads((staging/next_pointer['file']).read_text())
    if next_snapshot['meta']['tag_mapping_version'] != VERSION:
        raise RuntimeError('portrait_mapping_version_mismatch')
    # All amounts are reconstructed from dramas, never merged from old tag totals.
    for profile in next_snapshot['accounts'].values():
        if sum(t['revenue_cents'] for t in profile['tags']) != profile['revenue_cents']:
            raise RuntimeError('portrait_revenue_not_conserved')
    targets = lane.prepare_targets(release)
    for p in (SOURCE/'features/x_tag_revenue').glob('*'):
        if p.suffix in ('.py','.json'):
            targets[str(lane.MAIN/'features/x_tag_revenue'/p.name)] = p.read_bytes()
    for root in (lane.MAIN/'static', lane.WEB):
        targets[str(root/'x-tag-revenue.js')] = (SOURCE/'static/x-tag-revenue.js').read_bytes()
        channel = str(root/'x-account-list.html')
        targets[channel] = targets[channel].replace(b'x-tag-revenue.js?v=20261109v1', b'x-tag-revenue.js?v=20261109semantic1')
    targets = {p:data for p,data in targets.items() if lane.sha(p) != hashlib.sha256(data).hexdigest()}
    backup = BACKUPS/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup.mkdir(parents=True)
    info = dict(commit=commit, mapping_version=VERSION, release=str(release), backup=str(backup),
                previous_current=str(lane.CURRENT.resolve()), status='prepared', files={}, refresh=result,
                legacy_pids_before=lane.pid_state(), triggers_active={name:active(name) for name in TRIGGERS})
    with refresh_lock():
        before_pointer = json.loads((PORTRAITS/'current.json').read_text())
        info['previous_pointer'] = before_pointer
        shutil.copy2(PORTRAITS/before_pointer['file'],backup/'portrait-before.json')
        info['previous_status'] = json.loads((PORTRAITS/'status.json').read_text())
        for n,(target,data) in enumerate(targets.items()):
            saved = backup/str(n)
            if Path(target).exists(): shutil.copy2(target,saved)
            info['files'][target] = dict(before=lane.sha(target), after=hashlib.sha256(data).hexdigest(), backup=str(saved))
        record(backup/'deployment.json',info)
        changed = False
        try:
            quiet_lane(info)
            for target in targets:
                if lane.sha(target) != info['files'][target]['before']:
                    raise RuntimeError('deployment_target_changed:'+target)
            changed = True
            for target,data in targets.items():
                lane.atomic(target,data)
            switch_current(release)
            lane.atomic(PORTRAITS/next_pointer['file'],(staging/next_pointer['file']).read_bytes())
            next_pointer['previous'] = before_pointer['file']
            record(PORTRAITS/'current.json',next_pointer)
            lane.atomic(PORTRAITS/'status.json',(staging/'status.json').read_bytes())
            reload_services()
            info['legacy_pids_after'] = lane.pid_state()
            info['legacy_pids_unchanged'] = info['legacy_pids_before'] == info['legacy_pids_after']
            info['status'] = 'active'
        except Exception as exc:
            info['status'] = 'failed'
            info['error'] = str(exc)
            if changed:
                restore_files(info)
                info['restored_previous_release'] = True
            raise
        finally:
            record(backup/'deployment.json',info)
            restore_triggers(info)
    print(json.dumps(info, ensure_ascii=True))


def rollback(backup):
    backup = Path(backup).resolve()
    if backup.parent != BACKUPS.resolve(): raise RuntimeError('invalid_backup')
    info = json.loads((backup/'deployment.json').read_text())
    with refresh_lock():
        try:
            quiet_lane(info)
            restore_files(info)
            record(backup/'rollback.json',dict(status='rolled_back',business_databases_retained=True))
        finally:
            restore_triggers(info)
    print(json.dumps(dict(status='rolled_back',backup=str(backup),business_databases_retained=True)))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action',choices=['install','rollback']);parser.add_argument('--backup')
    args = parser.parse_args()
    install() if args.action=='install' else rollback(args.backup)
