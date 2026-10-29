#!/usr/bin/env python3
"""Prepare/switch only the cover release; never replace the API or ledgers."""
import argparse
import ast
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time

from install_cover import (ROOT, WEB, SHARED, MODULE, STORE, UNITS, OLD_UNITS,
                           atomic, digest, disk, require_disabled_legacy, run)
from install_cover_report import apply as switch_report


SCOPE = 'cover-worker-only'
SYSTEMD = Path('/etc/systemd/system')
COVER_SERVICES = ('meta-cover-scan.service', 'meta-cover-worker.service', 'meta-cover-notify.service')
GUARD_METHODS = ('_connect', '_transaction', '_cover_identity', '_cover_key',
                 'reserve_cover_resources', 'release_cover_resources', 'cover_reservations',
                 'claim_cover_delete_once', 'get_cover_delete_once', 'finish_cover_delete', 'claim_object')
GUARD_HELPERS = ('_id', '_now', '_json')


def unit_state():
    return {unit: {field: subprocess.run(['systemctl', field, unit], capture_output=True,
                                        text=True).stdout.strip()
                   for field in ('is-enabled', 'is-active')}
            for unit in UNITS + OLD_UNITS}


def require_cover_idle(state):
    # Other backend deletion jobs retain their own reservations. This narrow
    # deployment neither runs their schema migration nor replaces their API.
    for unit in COVER_SERVICES:
        if state.get(unit, {}).get('is-active') not in ('inactive', 'failed'):
            raise RuntimeError('service_not_idle:' + unit)


def guard_ast(path):
    """Compare shared mutual-exclusion behavior without gating unrelated UI code."""
    tree = ast.parse(path.read_text(encoding='utf-8-sig'))
    functions = {node.name: node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    store = next((node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'Store'), None)
    methods = {node.name: node for node in store.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))} if store else {}
    result = {}
    for prefix, names, available in (('Store.', GUARD_METHODS, methods), ('', GUARD_HELPERS, functions)):
        for name in names:
            node = available.get(name)
            if node is None:
                raise RuntimeError('shared_cover_guard_missing:' + prefix + name)
            if node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant) \
                    and isinstance(node.body[0].value.value, str):
                node.body = node.body[1:]
            result[prefix + name] = ast.dump(node, include_attributes=False)
    return result


def require_compatible_guards(reference, api):
    expected, actual = guard_ast(reference), guard_ast(api)
    changed = sorted(name for name in expected if expected[name] != actual[name])
    if changed:
        raise RuntimeError('shared_cover_guard_changed:' + ','.join(changed))
    return sorted(expected)


def online_snapshot(source, target):
    # A fixed read transaction prevents an active external writer from making
    # every incremental backup step restart. DELETE-journal writers may wait
    # briefly; keep this read lock below their existing 30-second busy timeout.
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as src, \
         closing(sqlite3.connect(str(target))) as dst:
        started = time.monotonic()
        def progress(status, remaining, total):
            if time.monotonic() - started >= 15:
                raise TimeoutError('backup_snapshot_deadline_exceeded')
        try:
            src.execute('BEGIN')
            src.execute('SELECT name FROM sqlite_master LIMIT 1').fetchone()
            src.backup(dst, pages=256, sleep=.05, progress=progress)
        finally:
            src.rollback()
        # Integrity verification can be expensive; it must not extend the
        # source read transaction or block unrelated backend writes.
        if dst.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise RuntimeError('backup_integrity_failed')


def prepare(release):
    disk()
    release = release.resolve()
    current = (ROOT / 'current').resolve()
    if ROOT / 'releases' not in release.parents or ROOT / 'releases' not in current.parents:
        raise RuntimeError('invalid_release_path')
    state = unit_state()
    require_cover_idle(state)
    config = json.loads((ROOT / 'config.json').read_text(encoding='utf-8'))
    require_disabled_legacy(config, state)
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=release, text=True).strip()
    if subprocess.check_output(['git', 'status', '--porcelain'], cwd=release, text=True).strip():
        raise RuntimeError('release_dirty')
    shared_adapter_hash = digest(current / STORE)
    if digest(release / STORE) != shared_adapter_hash:
        raise RuntimeError('shared_adapter_changed_in_cover_release')
    guards = require_compatible_guards(current / STORE, WEB / STORE)
    api_hash = digest(WEB / STORE)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup = ROOT / 'backups' / ('cover-worker-' + stamp + '-' + commit[:12])
    backup.mkdir(parents=True, mode=0o700)
    shutil.copy2(ROOT / 'config.json', backup / 'config.json')
    shutil.copy2(WEB / STORE, backup / 'web-store.py')
    for unit in UNITS + OLD_UNITS:
        path = SYSTEMD / unit
        if path.exists():
            shutil.copy2(path, backup / unit)
        elif unit in UNITS:
            raise RuntimeError('cover_unit_missing:' + unit)
    # SQLite backup reads a consistent snapshot while unrelated API jobs keep
    # running. These files are audit copies, never rollback restore inputs.
    for source, name in ((ROOT / 'cover.sqlite3', 'cover.sqlite3'),
                         (ROOT / 'cover-audit/runs.sqlite3', 'cover-audit.sqlite3'),
                         (SHARED, 'shared-tasks.sqlite3')):
        if not source.is_file():
            raise RuntimeError('required_ledger_missing:' + str(source))
        online_snapshot(source, backup / name)
    final_state = unit_state()
    require_cover_idle(final_state)
    require_disabled_legacy(json.loads((ROOT / 'config.json').read_text(encoding='utf-8')), final_state)
    if ((ROOT / 'current').resolve() != current or digest(current / STORE) != shared_adapter_hash
            or digest(release / STORE) != shared_adapter_hash
            or digest(ROOT / 'config.json') != digest(backup / 'config.json')):
        raise RuntimeError('cover_state_changed_during_backup')
    require_compatible_guards(current / STORE, WEB / STORE)
    plan = {'scope': SCOPE, 'release': str(release), 'commit': commit,
            'previous_release': str(current), 'backup': str(backup), 'unit_state': state,
            'web_store_before': api_hash, 'web_store_after_backup': digest(WEB / STORE),
            'shared_adapter_sha256': shared_adapter_hash, 'compatible_guard_methods': guards,
            'active_ledgers_only': True, 'ledgers_are_audit_only': True,
            'legacy_ledger_preserved_in_place': str(ROOT / 'runs.sqlite3')}
    (backup / 'plan.json').write_text(json.dumps(plan, indent=2), encoding='utf-8')
    (backup / 'hashes.json').write_text(json.dumps({p.name: digest(p) for p in backup.iterdir() if p.is_file()}, indent=2), encoding='utf-8')
    print(json.dumps({'backup': str(backup), 'commit': commit, 'scope': SCOPE,
                      'api_changed': False, 'shared_schema_changed': False}))
    return backup


def apply(backup, rollback=False):
    plan = json.loads((backup / 'plan.json').read_text())
    if plan.get('scope') == SCOPE:
        reference = Path(plan['previous_release']) / STORE
        if (digest(reference) != plan['shared_adapter_sha256']
                or digest(Path(plan['release']) / STORE) != plan['shared_adapter_sha256']):
            raise RuntimeError('shared_adapter_changed_since_backup')
        require_compatible_guards(reference, WEB / STORE)
        require_disabled_legacy(json.loads((ROOT / 'config.json').read_text(encoding='utf-8')), unit_state())
    if rollback:
        run(['systemctl', 'stop', 'meta-cover-worker.service'])
    switch_report(backup, rollback)
    source = backup if rollback else Path(plan['release']) / MODULE / 'deploy'
    atomic(source / 'meta-cover-worker.service', SYSTEMD / 'meta-cover-worker.service')
    run(['systemctl', 'daemon-reload'])
    # A bounded acceptance run precedes continuous activation. Rollback also
    # leaves execution stopped, preserving current outcomes and unknown fences.
    print(json.dumps({'worker_unit_updated': True, 'worker_started': False,
                      'backup': str(backup), 'rollback': rollback}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=('prepare', 'apply', 'rollback'))
    parser.add_argument('path', type=Path)
    args = parser.parse_args()
    prepare(args.path) if args.mode == 'prepare' else apply(args.path.resolve(), args.mode == 'rollback')
