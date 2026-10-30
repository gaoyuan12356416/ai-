#!/usr/bin/env python3
"""Category-only CPU release. Default check is read-only; no schema/config changes.

Reuse the established deployment primitives, but own an exact seven-target
manifest. Close admission, drain outbound requests, restart only API/replication
worker, then restore the original gate. Rollback never restores a database.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import deploy_video_editing_material_replication as common

BASE = '4cff9d3a10af861d4d42184a5deee2f57d4a1461'
REF = 'refs/heads/codex/video-editing-material-category-20261030'
MODULE = 'features/video_editing/material_replication.py'
GATE_TEXT = b'material category rollout: admission paused\n'


def targets():
    return [(common.LIVE / MODULE, MODULE)] + [
        (directory / name, 'static/' + name) for name in common.STATIC
        for directory in (common.LIVE / 'static', common.PUBLIC)]


def published(expected):
    common.require(re.fullmatch(r'[0-9a-f]{40}', expected or ''), 'exact_commit_required')
    run = lambda *args: common.run('git', '-C', str(common.ROOT), *args)
    common.require(run('rev-parse', 'HEAD') == expected, 'checkout_commit_mismatch')
    common.require(not run('status', '--porcelain', '--untracked-files=all'), 'dirty_release_checkout')
    common.require(run('remote', 'get-url', 'origin') in {
        'git@github.com:gaoyuan12356416/ai-.git',
        'https://github.com/gaoyuan12356416/ai-.git'}, 'unexpected_repository')
    common.require(run('ls-remote', 'origin', REF).split() == [expected, REF], 'commit_not_pushed')
    run('merge-base', '--is-ancestor', BASE, expected)
    return expected


def build_plan(commit):
    rows = []
    for target, source in targets():
        before = common.digest(common.git_bytes(BASE, source))
        data = common.git_bytes(commit, source)
        common.require(common.file_sha(target) == before, 'live_file_drift:' + str(target))
        if source.endswith('.py'):
            compile(data, str(target), 'exec')
        rows.append({'path': str(target), 'source': source, 'before': before,
                     'after': common.digest(data), 'mode': target.stat().st_mode & 0o777, 'data': data})
    return rows


def gate_state():
    return {'sha256': common.file_sha(common.MARKER)}


def close_gate(prior):
    actual = common.file_sha(common.MARKER)
    expected = prior['sha256']
    common.require(actual == expected or expected is None and actual == common.digest(GATE_TEXT),
                   'gate_changed_by_another_operator')
    if actual is None:
        common.atomic(common.MARKER, GATE_TEXT)


def restore_gate(prior):
    actual = common.file_sha(common.MARKER)
    if prior['sha256'] is not None:
        common.require(actual == prior['sha256'], 'preexisting_gate_changed')
    else:
        common.require(actual in {None, common.digest(GATE_TEXT)}, 'foreign_gate_preserved')
        common.MARKER.unlink(missing_ok=True)


def submitting(*, barrier=False):
    # After the marker closes, this short writer barrier waits for transactions
    # that passed the last gate check to commit their durable outbound marker.
    uri = common.DB.as_uri() + ('?mode=rw' if barrier else '?mode=ro')
    with closing(sqlite3.connect(uri, uri=True, timeout=5, isolation_level=None)) as db:
        if barrier:
            db.execute('BEGIN IMMEDIATE')
        try:
            return db.execute("SELECT COUNT(*) FROM ve_material_replication WHERE status='submitting'").fetchone()[0]
        finally:
            if barrier:
                db.rollback()


def drain(timeout):
    deadline = time.monotonic() + timeout
    while True:
        try:
            if submitting(barrier=True) == 0:
                return
        except sqlite3.OperationalError:
            pass
        common.require(time.monotonic() < deadline, 'outbound_request_not_drained_worker_untouched')
        time.sleep(0.5)


def resume_worker(prior):
    common.run('systemctl', 'start' if prior.get('ActiveState') == 'active' else 'stop', common.WORKER)
    after = common.state(common.WORKER)
    common.require(after.get('ActiveState') == prior.get('ActiveState'), 'replication_worker_state_changed')
    if prior.get('ActiveState') == 'active':
        common.require(int(after.get('MainPID', '0')) > 0, 'replication_worker_not_running')


def verify_rows(rows, backup):
    common.require({row['path'] for row in rows} == {str(path) for path, _ in targets()},
                   'category_manifest_scope_mismatch')
    common.verify_backup(rows, backup)


def snapshot(commit, plan):
    backup = common.DATA / 'backups' / ('material-category-' + time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
                                      + '-' + commit[:12])
    backup.mkdir(parents=True, mode=0o700)
    manifest = {'commit': commit, 'base': BASE, 'status': 'prepared', 'files': [],
                'gate_before': gate_state(), 'protected_before': common.protected_state(),
                'api_before': common.state(common.API), 'worker_before': common.state(common.WORKER)}
    for key in ('api_before', 'worker_before'):
        common.require(manifest[key].get('ActiveState') in {'active', 'inactive'} and
                       manifest[key].get('LoadState') != 'not-found', 'service_not_stable:' + key)
    for index, row in enumerate(plan):
        common.require(common.file_sha(row['path']) == row['before'], 'live_file_changed_before_backup')
        saved = backup / ('file-' + str(index))
        shutil.copy2(row['path'], saved)
        saved.chmod(0o600)
        common.require(common.file_sha(saved) == row['before'], 'backup_checksum_mismatch')
        manifest['files'].append({**{key: value for key, value in row.items() if key != 'data'}, 'backup': saved.name})
    common.backup_database(backup / 'state-before.sqlite3')
    manifest['sqlite_backup_sha256'] = common.file_sha(backup / 'state-before.sqlite3')
    common.save(backup / 'manifest.json', manifest)
    return backup / 'manifest.json', manifest


def apply(commit, plan, timeout):
    path, manifest = snapshot(commit, plan)
    installed = []
    stop_started = False
    try:
        close_gate(manifest['gate_before'])
        drain(timeout)
        stop_started = True
        common.run('systemctl', 'stop', common.WORKER)
        for row in plan:
            common.require(common.file_sha(row['path']) == row['before'], 'live_file_changed_before_install')
        for row in plan:
            common.atomic(row['path'], row['data'], row['mode'])
            installed.append(next(item for item in manifest['files'] if item['path'] == row['path']))
        common.restore_api(manifest['api_before'])
        resume_worker(manifest['worker_before'])
        verify_rows(manifest['files'], path.parent)
        manifest['protected_after'] = common.check_protected(manifest['protected_before'])
        common.integrity(common.DB)
        manifest['status'] = 'installed_gate_closed'
        common.save(path, manifest)
        restore_gate(manifest['gate_before'])
        manifest['status'] = 'installed'
        common.save(path, manifest)
    except BaseException:
        try:
            close_gate(manifest['gate_before'])
            if stop_started:
                # Also covers failure after the gate reopened: never interrupt a
                # newly acquired outbound request while attempting file recovery.
                drain(timeout)
                common.run('systemctl', 'stop', common.WORKER)
                if installed:
                    common.verify_backup(installed, path.parent)
                    common.run('systemctl', 'stop', common.API)
                    common.restore_files(installed, path.parent)
                    common.restore_api(manifest['api_before'])
                resume_worker(manifest['worker_before'])
            common.check_protected(manifest['protected_before'])
            restore_gate(manifest['gate_before'])
            manifest['status'] = 'failed_files_restored_ledger_preserved' if stop_started else 'drain_failed_original_gate_restored'
        except BaseException:
            manifest['status'] = 'failed_manual_recovery_required'
        common.save(path, manifest)
        raise
    return {'status': manifest['status'], 'commit': commit, 'manifest': str(path)}


def load_manifest(value):
    path = Path(value).resolve()
    common.require((common.DATA / 'backups').resolve() in path.parents and path.name == 'manifest.json'
                   and path.parent.name.startswith('material-category-'), 'category_manifest_path_invalid')
    manifest = json.loads(path.read_text(encoding='utf-8'))
    common.require(manifest.get('status') in {'installed', 'installed_gate_closed'}, 'category_manifest_not_installed')
    verify_rows(manifest['files'], path.parent)
    return path, manifest


def rollback(path, manifest, timeout):
    verify_rows(manifest['files'], path.parent)
    gate, protected = gate_state(), common.protected_state()
    api, worker = common.state(common.API), common.state(common.WORKER)
    common.backup_database(path.parent / 'state-before-rollback.sqlite3')
    stop_started = False
    try:
        close_gate(gate)
        drain(timeout)
        stop_started = True
        common.run('systemctl', 'stop', common.WORKER)
        common.run('systemctl', 'stop', common.API)
        common.restore_files(manifest['files'], path.parent)
        common.restore_api(api)
        resume_worker(worker)
        common.check_protected(protected)
        common.integrity(common.DB)
        restore_gate(gate)
        manifest['status'] = 'rolled_back_category_files_ledger_preserved'
    except BaseException:
        if not stop_started:
            restore_gate(gate)
        manifest['status'] = 'rollback_failed_manual_recovery_required' if stop_started else 'installed'
        common.save(path, manifest)
        raise
    common.save(path, manifest)
    return {'status': manifest['status'], 'manifest': str(path)}


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument('--apply', action='store_true')
    actions.add_argument('--rollback', metavar='MANIFEST')
    parser.add_argument('--commit')
    parser.add_argument('--drain-timeout', type=int, default=90)
    args = parser.parse_args(argv)
    common.require(1 <= args.drain_timeout <= 180, 'invalid_drain_timeout')
    common.storage(require_space=True)
    if args.rollback:
        path, manifest = load_manifest(args.rollback)
        with common.deployment_lock():
            result = rollback(path, manifest, args.drain_timeout)
    else:
        commit = published(args.commit)
        plan = build_plan(commit)
        result = {'status': 'preflight_passed', 'commit': commit, 'submitting': submitting(),
                  'files': [{key: row[key] for key in ('path', 'before', 'after')} for row in plan]}
        if args.apply:
            with common.deployment_lock():
                result = apply(commit, plan, args.drain_timeout)
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        reason = str(exc) if type(exc) is RuntimeError else type(exc).__name__
        print(json.dumps({'status': 'failed', 'error': reason}), file=sys.stderr)
        sys.exit(1)
