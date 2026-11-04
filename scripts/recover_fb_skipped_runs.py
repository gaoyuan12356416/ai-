"""Operator-only, dry-run-by-default recovery of explicitly selected skipped runs.

Reads production credentials only in memory from the owning service environment.
Uses the reserved manual read-only SQL pool, not runtime publishing connections.
"""
import argparse
import contextlib
import dataclasses
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import random
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from features.fb_auto_posts.core import FBAutoPostStore, utc_now
from features.fb_auto_posts.languages import page_language
from features.fb_auto_posts.repositories import ReadOnlyMySQL, PagePoolRepository, MaterialRepository, MaterialCandidate, CandidateSnapshot, RepositoryError
from features.fb_auto_posts.skipped_recovery import recover_skipped
from features.fb_auto_posts.strategy import daily_limit


def existing_store(path):
    store = FBAutoPostStore.__new__(FBAutoPostStore)
    store.path, store.now_fn, store.rng = str(path), utc_now, random.SystemRandom()
    store._lock, store.max_late_seconds = threading.RLock(), 1800
    return store


def service_env():
    pid = subprocess.check_output(['systemctl', 'show', 'fb-auto-post-service.service', '-p', 'MainPID', '--value'], text=True).strip()
    return dict(part.split('=', 1) for part in Path('/proc/' + pid + '/environ').read_text().split('\0') if '=' in part)


def repositories(env, operation):
    import pymysql
    assert env['FB_AUTO_MYSQL_HOST'] == '101.32.56.53' and env['FB_AUTO_MYSQL_PORT'] == '63350'
    def connect():
        conn = pymysql.connect(host=env['FB_AUTO_MYSQL_HOST'], port=63350,
            user=env['FB_AUTO_MYSQL_USER'], password=env['FB_AUTO_MYSQL_PASSWORD'],
            database=env['FB_AUTO_MYSQL_DATABASE'], charset='utf8mb4', autocommit=True,
            connect_timeout=8, read_timeout=50, write_timeout=5)
        with conn.cursor() as cur:
            cur.execute('SELECT @@read_only')
            if cur.fetchone()[0] != 1:
                conn.close()
                raise RuntimeError('read_only_endpoint_required')
            cur.execute('SET SESSION MAX_EXECUTION_TIME=45000')
            cur.execute('SET SESSION TRANSACTION READ ONLY')
        return conn
    class OperatorReads(ReadOnlyMySQL):
        def select(self, sql, params):
            # SELECT-only reads may retry once; no publication API is used here.
            try:
                return super().select(sql, params)
            except RepositoryError as exc:
                if exc.code != 'fb_auto_source_query_failed':
                    raise
                time.sleep(1)
                return super().select(sql, params)
    mysql = OperatorReads(connect, env['FB_AUTO_MYSQL_DATABASE'], env.get('FB_AUTO_BLACKLIST_MYSQL_DATABASE', 'ads_setting'))
    return (PagePoolRepository(mysql), MaterialRepository(mysql, existing_store(env['FB_AUTO_METRIC_DB_PATH']),
                                                        rng=random.Random(operation)))


def save(path, value):
    path = Path(path)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + '\n')
    path.chmod(0o600)


def decode_snapshot(raw):
    candidates = []
    for item in raw['candidates']:
        item = dict(item)
        for key in ('duration_seconds', 'material_spend', 'material_roas', 'drama_spend', 'drama_roas'):
            if item[key] is not None:
                item[key] = Decimal(item[key])
        candidates.append(MaterialCandidate(**item))
    return CandidateSnapshot(tuple(candidates), tuple(raw['metric_generation_ids']), tuple(raw['metric_dates']))


def checkpoint_snapshots(folder, config, pages, materials, operation_id, candidate_limit=1000):
    fingerprint = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
    snapshots, errors, pending = {}, {}, []
    for language in sorted({page_language(p.language) for p in pages} - {''}):
        path = folder / ('candidate-' + language + '.json')
        cached = json.loads(path.read_text()) if path.exists() else None
        if (cached and cached['operation_id'] == operation_id and cached['config_sha256'] == fingerprint
                and cached.get('candidate_limit',1000) == candidate_limit
                and 0 <= (utc_now() - datetime.fromisoformat(cached['finished_at_utc'])).total_seconds() <= 900):
            snapshots[language] = decode_snapshot(cached['snapshot'])
        else:
            pending.append(language)
    def collect(language):
        print(json.dumps({'phase': 'language_started', 'language': language}), flush=True)
        return materials.candidate_snapshot({**config, '_candidate_limit': candidate_limit, 'language': language})
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(collect, language): language for language in pending}
        for future in as_completed(futures):
            language = futures[future]
            try:
                snapshot = future.result()
                snapshot = CandidateSnapshot(tuple(m for m in snapshot.candidates if page_language(m.language) == language),
                                             snapshot.metric_generation_ids, snapshot.metric_dates)
                snapshots[language] = snapshot
                save(folder / ('candidate-' + language + '.json'), {'operation_id': operation_id,
                    'config_sha256': fingerprint, 'candidate_limit': candidate_limit,
                    'finished_at_utc': utc_now().isoformat(), 'snapshot': dataclasses.asdict(snapshot)})
                print(json.dumps({'phase': 'language_finished', 'language': language, 'candidates': len(snapshot.candidates)}), flush=True)
            except RepositoryError as exc:
                errors[language] = exc.code
                print(json.dumps({'phase': 'language_failed', 'language': language, 'error': exc.code}), flush=True)
    if errors:
        save(folder / 'catalog-errors.json', errors)
        raise RuntimeError('incomplete_catalog_recheck_failed_languages_only')
    return snapshots


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-ids', type=int, nargs='+', required=True)
    parser.add_argument('--operation-id', required=True)
    parser.add_argument('--folder', required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--candidate-limit', type=int, default=1000, help='Operator catalog bound; all source rules and final reservations remain unchanged')
    args = parser.parse_args()
    assert 50 <= args.candidate_limit <= 5000
    folder = Path(args.folder).resolve()
    disk = Path('/mnt/data-disk')
    assert disk.is_mount() and folder.is_relative_to(disk)
    assert subprocess.check_output(['findmnt', '-n', '-o', 'UUID', str(disk)], text=True).strip() == '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    env = service_env()
    store = existing_store(env['FB_AUTO_POST_DB_PATH'])
    if not args.apply:
        rehearsal = folder / 'preview.sqlite3'
        with contextlib.closing(sqlite3.connect(Path(store.path).as_uri() + '?mode=ro', uri=True)) as source:
            with contextlib.closing(sqlite3.connect(rehearsal)) as dest:
                source.backup(dest)
        rehearsal.chmod(0o600)
        store = FBAutoPostStore(rehearsal)
    with store.connect() as conn:
        runs = [dict(conn.execute('SELECT * FROM fb_auto_run WHERE id=?', (rid,)).fetchone()) for rid in args.run_ids]
        assert len({r['config_json'] for r in runs}) == 1
        template = dict(conn.execute('SELECT * FROM fb_auto_template WHERE id=?', (runs[0]['template_id'],)).fetchone())
    config_json = runs[0]['config_json']
    config = json.loads(config_json)
    page_repo, materials = repositories(env, args.operation_id)
    cache = folder / 'candidate-snapshot.json'
    if args.apply:
        frozen = json.loads(cache.read_text())
        assert frozen['operation_id'] == args.operation_id and frozen['run_ids'] == args.run_ids
        assert frozen['config_sha256'] == hashlib.sha256(config_json.encode()).hexdigest()
        age = (utc_now() - datetime.fromisoformat(frozen['finished_at_utc'])).total_seconds()
        assert 0 <= age <= 900, 'candidate_snapshot_expired_rerun_preview'
        snapshots = {language: decode_snapshot(raw) for language, raw in frozen['snapshots'].items()}
    else:
        pages = page_repo.list_pages(config['group_ids'], is_admin=bool(template['scope_is_admin']), owner_user_id=template['owner_user_id'])
        with store.connect() as conn:
            targets = {r[0] for r in conn.execute("SELECT page_id FROM fb_auto_task WHERE status='skipped' "
                "AND skip_reason='fb_auto_page_unknown_block' AND run_id IN (" + ','.join('?' for _ in args.run_ids) + ')', args.run_ids)}
        catalog_pages = [p for p in pages if p.page_id in targets and p.eligible_token_count > 0 and daily_limit(config,p.page_id)>0]
        print(json.dumps({'phase': 'current_pages', 'pages': len(pages), 'catalog_pages': len(catalog_pages)}), flush=True)
        snapshots = checkpoint_snapshots(folder, config, catalog_pages, materials, args.operation_id, args.candidate_limit)
        save(cache, {'operation_id': args.operation_id, 'run_ids': args.run_ids,
                     'config_sha256': hashlib.sha256(config_json.encode()).hexdigest(),
                     'finished_at_utc': utc_now().isoformat(),
                     'snapshots': {k: dataclasses.asdict(v) for k, v in snapshots.items()}})
        print(json.dumps({'phase': 'candidates', 'languages': {k: len(v.candidates) for k,v in snapshots.items()}}), flush=True)
    # Re-read mutable Page scope/authorization after the potentially slow catalog.
    pages = page_repo.list_pages(config['group_ids'], is_admin=bool(template['scope_is_admin']), owner_user_id=template['owner_user_id'])
    if page_repo.legacy_conflicts(config['group_ids']):
        raise RuntimeError('legacy_queue_conflict')
    if args.apply:
        backup = folder / 'before-recovery.sqlite3'
        if not backup.exists():
            with contextlib.closing(sqlite3.connect(Path(store.path).as_uri() + '?mode=ro', uri=True)) as source:
                with contextlib.closing(sqlite3.connect(backup)) as dest:
                    source.backup(dest)
                    assert dest.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
            backup.chmod(0o600)
    report = recover_skipped(store, args.run_ids, pages, materials, snapshots,
        operation_id=args.operation_id, expected_config=config_json, apply=args.apply)
    save(folder / ('applied.json' if args.apply else 'preview.json'), report)
    print(json.dumps({'applied': args.apply, 'operation_id': args.operation_id,
        'runs': [{'run_id': r['run_id'], 'restored': len(r['restored']), 'retained': len(r['retained']),
                  'already_restored': len(r['already_restored'])} for r in report['runs']]}), flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Driver errors may contain connection details; expose only safe classes/codes.
        print(json.dumps({'ok': False, 'error': getattr(exc, 'code', type(exc).__name__)}))
        sys.exit(1)
