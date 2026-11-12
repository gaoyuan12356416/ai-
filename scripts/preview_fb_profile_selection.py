"""Read-only production preview: no Runtime, schema initialization or publish call."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import random
import sqlite3
import subprocess
import sys
import time

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
from features.fb_auto_posts.core import FBAutoPostStore, _ClosingConnection, utc_iso
from features.fb_auto_posts.effects import load_scores
from features.fb_auto_posts.languages import page_language
from features.fb_auto_posts.profile_material import choose_profile_material, metadata_for
from features.fb_auto_posts.profile_selection import Decision, load_profiles
from features.fb_auto_posts.repositories import MaterialRepository, PagePoolRepository, ReadOnlyMySQL
from features.fb_auto_posts.strategy import cooldown_content_ids, diverse_candidates


class ReadOnlyStore(FBAutoPostStore):
    """Reuse only read methods, with SQLite independently rejecting all writes."""
    def __init__(self, path, now):
        self.path, self.now_fn = str(path), lambda: now

    def connect(self):
        path = Path(self.path).resolve()
        conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=15,
                               isolation_level=None, factory=_ClosingConnection)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA query_only=ON')
        return conn


def live_environment():
    pid = subprocess.check_output(['systemctl', 'show', 'fb-auto-post-service.service',
                                   '-p', 'MainPID', '--value'], text=True).strip()
    assert pid.isdigit() and int(pid) > 0, 'fb_service_not_running'
    raw = Path('/proc', pid, 'environ').read_bytes()
    return dict(value.decode().split('=', 1) for value in raw.split(b'\0') if b'=' in value)


def source_reader(env):
    import pymysql
    port = int(env.get('FB_AUTO_MYSQL_PORT', '63350'))
    assert port == 63350, 'read_replica_port_required'
    def connect():
        db = pymysql.connect(host=env.get('FB_AUTO_MYSQL_HOST', '127.0.0.1'), port=port,
            user=env.get('FB_AUTO_MYSQL_USER', ''), password=env.get('FB_AUTO_MYSQL_PASSWORD', ''),
            database=env.get('FB_AUTO_MYSQL_DATABASE', 'kunlunads_dev'), charset='utf8mb4',
            autocommit=True, connect_timeout=5, read_timeout=60, write_timeout=5)
        try:
            with db.cursor() as cursor:
                cursor.execute('SELECT @@read_only')
                assert cursor.fetchone()[0] == 1, 'server_not_readonly'
            return db
        except Exception:
            db.close()
            raise
    return ReadOnlyMySQL(connect, env.get('FB_AUTO_MYSQL_DATABASE', 'kunlunads_dev'),
                        env.get('FB_AUTO_BLACKLIST_MYSQL_DATABASE', 'ads_setting'))


def preview(args):
    began = time.monotonic()
    now = datetime.now(timezone.utc)
    env = live_environment()
    source = source_reader(env)
    ledger = ReadOnlyStore(env.get('FB_AUTO_POST_DB_PATH', '/mnt/data-disk/fb-auto-post-publisher/fb-auto-post.sqlite3'), now)
    metrics = ReadOnlyStore(env.get('FB_AUTO_METRIC_DB_PATH', '/mnt/data-disk/fb-auto-post-publisher/fb-auto-metric.sqlite3'), now)
    pages = PagePoolRepository(source)
    with ledger.connect() as conn:
        sql = '''SELECT t.*,v.config_json FROM fb_auto_template t JOIN fb_auto_template_version v
                 ON v.template_id=t.id AND v.version=t.current_version WHERE t.status='enabled' '''
        parameters = ()
        if args.template_id:
            sql += 'AND t.id=? '
            parameters = (args.template_id,)
        templates = [dict(row) for row in conn.execute(sql + 'ORDER BY t.id', parameters)]
    assert templates, 'enabled_template_not_found'
    chosen = None
    for template in templates:
        config = json.loads(template['config_json'])
        if str(config.get('app_id')) != '1479':
            continue
        page_rows = pages.list_pages(config['group_ids'], is_admin=bool(template['scope_is_admin']),
                                     owner_user_id=str(template['owner_user_id']))
        page_rows = [p for p in page_rows if p.eligible_token_count > 0 and page_language(p.language)]
        snapshot = load_profiles([p.page_id for p in page_rows], root=env.get('FB_AUTO_PROFILE_ROOT'), now=now)
        identity = [template['id'], template['current_version'], 'read-only-preview:' + utc_iso(now)]
        eligible = [p for p in page_rows if not Decision(p.page_id, identity, snapshot).reason]
        if args.language:
            eligible = [p for p in eligible if page_language(p.language) == page_language(args.language)]
        if eligible:
            language = page_language(args.language) if args.language else Counter(
                page_language(p.language) for p in eligible).most_common(1)[0][0]
            chosen = template, config, page_rows, snapshot, identity, language
            break
    assert chosen, 'no_valid_profile_in_enabled_template_language'
    template, config, page_rows, profile_snapshot, identity, language = chosen
    samples = sorted((p for p in page_rows if page_language(p.language) == language),
                     key=lambda p: (bool(Decision(p.page_id, identity, profile_snapshot).reason), p.page_id))[:10]
    with ledger.connect() as conn:
        maximum = max((len(ledger._cooldown_material_ids(conn, p.page_id, int(config['cooldown_days'])))
                       for p in page_rows), default=0)
    limit = min(5000, max(1000 if config.get('drama_cooldown_hours') else 500, maximum + 100))
    repository = MaterialRepository(source, metrics, now_fn=lambda: now, rng=random.Random(0))
    scan_began = time.monotonic()
    candidate_snapshot = repository.candidate_snapshot(dict(config, language=language,
        _candidate_limit=limit, _profile_selection=True))
    scan_seconds = round(time.monotonic() - scan_began, 3)
    examples = []
    slot = identity[-1]
    with ledger.connect() as conn:
        conn.execute('BEGIN')
        scores = load_scores(conn, config, now)
        for page in samples:
            decision = Decision(page.page_id, identity, profile_snapshot)
            excluded = ledger._cooldown_material_ids(conn, page.page_id, int(config['cooldown_days']))
            blocked = cooldown_content_ids(conn, page.page_id, utc_iso(now),
                                           int(config.get('drama_cooldown_hours', 0)), utc_iso(now))
            candidates = candidate_snapshot.candidates
            if decision.reason or candidate_snapshot.profile_error:
                candidates = diverse_candidates(candidates, limit, 20 if int(config.get('drama_cooldown_hours', 0)) else 0)
            candidates = [item for item in candidates if item.content_id not in blocked]
            material, evidence = choose_profile_material(candidates, excluded, decision=decision,
                snapshot=candidate_snapshot, candidate_limit=limit, materials=repository,
                config=config, page_id=page.page_id, slot_key=slot, scores=scores)
            audit = evidence['tag_profile']
            examples.append(dict(page_id=page.page_id,
                preferred_path=audit['preferred_path'], selected_path=audit['selected_path'],
                fallback_reason=audit['fallback_reason'], material_id=material.material_id if material else '',
                content_id=material.content_id if material else '', eligible_candidates=len(candidates),
                excluded_materials=len(excluded), excluded_dramas=len(blocked)))
    classified = candidate_snapshot.profile_classifications
    count = len(candidate_snapshot.candidates)
    coverage = sum(bool(metadata_for(m, classified)) for m in candidate_snapshot.candidates)
    return dict(read_only=True, template_id=template['id'], template_version=template['current_version'],
        language=language, publishable_pages=len(page_rows), sampled_pages=len(examples),
        candidate_count=count, candidate_drama_count=len({m.content_id for m in candidate_snapshot.candidates}),
        classified_candidate_count=coverage, classification_count=len(classified),
        classification_error=candidate_snapshot.profile_error,
        profile_version=profile_snapshot[1].get('version', ''), profile_stale=profile_snapshot[1].get('stale', False),
        metric_dates=candidate_snapshot.metric_dates, examples=examples,
        candidate_scan_seconds=scan_seconds, elapsed_seconds=round(time.monotonic() - began, 3))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--template-id', type=int)
    parser.add_argument('--language')
    args = parser.parse_args()
    try:
        result = preview(args)
    except Exception as error:
        # Driver exception messages can contain SQL or connection information.
        print(json.dumps(dict(read_only=True, ok=False, error_type=type(error).__name__,
                              error_code=getattr(error, 'code', 'preview_failed'))), flush=True)
        return 1
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
