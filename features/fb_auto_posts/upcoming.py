"""Keep a Page's original slot while its discovered Moboreels stories mature."""
import json
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

from features import moboreels_upcoming as catalog
from features.fb_video_publish.moboreels import contains
from features.material_products import selected
from .languages import page_language
from .repositories import RepositoryError
from .strategy import daily_limit


def enabled(config):
    return (contains(config.get('message_template')) and not config.get('hit_drama_publish')
            and not config.get('hit_material_publish') and catalog.applies(config.get('drama_launch_window_days', 0))
            and any(p.casefold() in {'moboreels', 'moboshort'} for p in selected(config)))


def discover(repo, config, now):
    return catalog.discover(repo.mysql.select, repo.mysql.schema, config['drama_launch_window_days'],
                            int(now.timestamp()), material_config=config)


def reviews(repo, plan, languages, now):
    if plan is None:
        return {}
    result = {}
    for language in sorted(set(languages) - {''}):
        try:
            result[language] = catalog.assess(repo.mysql.select, repo.mysql.schema, plan, language, int(now.timestamp()))
        except RepositoryError as exc:
            if exc.status < 500:
                raise
            result[language] = PendingRead({}, {}, int(now.timestamp()) + 300, exc.code)
    return result


@dataclass
class PendingRead(catalog.Availability):
    error: str


def ensure_schema(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS fb_auto_mobo_plan (
            run_id INTEGER PRIMARY KEY REFERENCES fb_auto_run(id),plan_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS fb_auto_mobo_wait (
            run_id INTEGER NOT NULL REFERENCES fb_auto_mobo_plan(run_id),page_id TEXT NOT NULL,
            original_due TEXT NOT NULL, next_check INTEGER NOT NULL, lease_token TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(run_id,page_id)
        );
        CREATE INDEX IF NOT EXISTS idx_fb_mobo_next_check ON fb_auto_mobo_wait(next_check,run_id);
    """)


WAIT_REASON = '等待 Moboreels 新剧上架及同语种素材'


@dataclass
class Claim:
    run: dict
    scope_admin: bool
    owner: str
    pages: dict
    plan: dict
    token: str

    def scoped(self, pages):
        result = []
        for page in pages:
            frozen = self.pages.get(page.page_id)
            if (frozen and page_language(page.language) == frozen['language']
                    and page.owner_user_id == frozen['owner_user_id'] and frozen['group_id'] in page.group_ids):
                result.append(replace(page, group_id=frozen['group_id']))
        return result


def claim(store, lease_seconds):
    now = int(store.now_fn().timestamp())
    with store.connect() as conn:
        if not conn.execute('SELECT 1 FROM fb_auto_mobo_wait WHERE next_check<=? LIMIT 1',(now,)).fetchone():
            return None
    with store._lock, store.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute("""
            SELECT w.run_id,t.scope_is_admin,t.owner_user_id
              FROM fb_auto_mobo_wait w JOIN fb_auto_run r ON r.id=w.run_id
              JOIN fb_auto_template t ON t.id=r.template_id
             WHERE w.next_check<=? AND t.status='enabled'
               AND (t.current_version=r.template_version OR r.schedule_policy='target_midnight_v1')
             ORDER BY w.next_check,w.run_id,w.page_id LIMIT 1
        """, (now,)).fetchone()
        if row is None:
            return None
        run_id = row['run_id']
        run = conn.execute('SELECT * FROM fb_auto_run WHERE id=?', (run_id,)).fetchone()
        frozen = conn.execute('SELECT plan_json FROM fb_auto_mobo_plan WHERE run_id=?', (run_id,)).fetchone()
        pages = {r['page_id']: dict(r) for r in conn.execute("""
            SELECT w.page_id,w.original_due,p.group_id,p.owner_user_id,p.language
              FROM fb_auto_mobo_wait w JOIN fb_auto_run_page p ON p.run_id=w.run_id AND p.page_id=w.page_id
             WHERE w.run_id=? AND w.next_check<=? ORDER BY w.next_check,w.page_id LIMIT 100
        """, (run_id,now))}
        token = uuid.uuid4().hex
        conn.executemany('UPDATE fb_auto_mobo_wait SET lease_token=?,next_check=? WHERE run_id=? AND page_id=?',
                         [(token,now + max(1,int(lease_seconds)),run_id,pid) for pid in pages])
        return Claim(dict(run), bool(row['scope_is_admin']), row['owner_user_id'], pages, json.loads(frozen[0]), token)


def owned(conn, claim, now):
    return {r[0] for r in conn.execute(
        'SELECT page_id FROM fb_auto_mobo_wait WHERE run_id=? AND lease_token=? AND next_check>?',
        (claim.run['id'],claim.token,int(now.timestamp())))}


def wait(conn, run_id, page_id, original_due, review):
    conn.execute("""INSERT INTO fb_auto_mobo_wait(run_id,page_id,original_due,next_check) VALUES(?,?,?,?)
        ON CONFLICT(run_id,page_id) DO UPDATE SET next_check=excluded.next_check,lease_token=''""",
        (run_id,page_id,original_due,review.retry_at))
    reason = (WAIT_REASON + ' · ' + review.error) if isinstance(review,PendingRead) else WAIT_REASON
    conn.execute("UPDATE fb_auto_run_page SET snapshot_status='waiting_mobo',skip_reason=? WHERE run_id=? AND page_id=?",
                 (reason,run_id,page_id))


def finish_absent(conn, run_id, page_ids):
    for pid in page_ids:
        conn.execute("UPDATE fb_auto_run_page SET snapshot_status='skipped',skip_reason='fb_auto_page_scope_changed' "
                     'WHERE run_id=? AND page_id=?', (run_id,pid))
        conn.execute('DELETE FROM fb_auto_mobo_wait WHERE run_id=? AND page_id=?',(run_id,pid))


def discovery_summary(plan):
    languages = {}
    for row in plan['stories'].values():
        lang = row['language']
        languages[lang] = languages.get(lang, 0) + 1
    result = {'scanned_at': plan['scanned_at'], 'story_count': len(plan['stories']), 'languages': languages}
    if plan.get('material_scan') == 'unavailable':
        result['material_scan'] = 'unavailable'
    else:
        result['stories_with_source_material'] = sum(
            int(row.get('source_material_count', 0)) > 0 for row in plan['stories'].values())
    return result


def cancel(store, conn, template_id, now, reason, *, manual_only=False, superseded=False):
    filters = ['r.template_id=?']
    if manual_only:
        filters.append("r.trigger_type='manual'")
    if superseded:
        filters.append("r.schedule_policy<>'target_midnight_v1'")
    waits = conn.execute('SELECT w.run_id,w.page_id FROM fb_auto_mobo_wait w JOIN fb_auto_run r ON r.id=w.run_id WHERE '
                         + ' AND '.join(filters), (template_id,)).fetchall()
    counts = {}
    for rid,pid in waits:
        conn.execute("UPDATE fb_auto_run_page SET snapshot_status='skipped',skip_reason=? WHERE run_id=? AND page_id=?",
                     (reason,rid,pid))
        conn.execute('DELETE FROM fb_auto_mobo_wait WHERE run_id=? AND page_id=?', (rid,pid))
        counts[rid] = counts.get(rid,0) + 1
    for rid,count in counts.items():
        conn.execute('UPDATE fb_auto_run SET skipped_tasks=skipped_tasks+? WHERE id=?', (count,rid))
        store._refresh_run(conn,rid,now)


def day_full(conn, template_id, page_id, config, publish_at):
    # An old waiting slot must not add another automatic post to a full Beijing day.
    when = datetime.fromisoformat(publish_at)
    start = when.astimezone(timezone(timedelta(hours=8))).replace(hour=0,minute=0,second=0,microsecond=0)
    low, high = (d.astimezone(timezone.utc).isoformat(timespec='seconds') for d in (start,start+timedelta(days=1)))
    total = conn.execute("""SELECT COUNT(*) FROM fb_auto_task t JOIN fb_auto_run r ON r.id=t.run_id
        WHERE t.template_id=? AND t.page_id=? AND r.trigger_type='auto'
          AND ((t.status IN ('planned','preparing','ready','queued','running','submitted','unknown','published')
                AND t.planned_publish_at_utc>=? AND t.planned_publish_at_utc<?)
               OR (t.status IN ('running','submitted','unknown') AND t.started_at_utc>=? AND t.started_at_utc<?)
               OR (t.status='published' AND t.completed_at_utc>=? AND t.completed_at_utc<?))""",
                         (template_id,page_id,low,high,low,high,low,high)).fetchone()[0]
    return total >= daily_limit(config,page_id)


def defer(store, claim, error_code):
    with store.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        now = store.now_fn()
        for pid in owned(conn,claim,now):
            conn.execute("UPDATE fb_auto_run_page SET skip_reason=? WHERE run_id=? AND page_id=?",
                         (WAIT_REASON + ' · ' + str(error_code),claim.run['id'],pid))
        conn.execute("UPDATE fb_auto_mobo_wait SET next_check=?,lease_token='' WHERE run_id=? AND lease_token=? AND next_check>?",
                     (int(now.timestamp())+300,claim.run['id'],claim.token,int(now.timestamp())))


def resume(runtime, lease_seconds):
    from .core import ActorScope, StoreError
    from .repositories import RepositoryError
    item = claim(runtime.store, lease_seconds)
    if item is None:
        return {'ok': True, 'status': 'no_due_slot', 'claimed': False}
    r = item.run
    try:
        return runtime.store.create_run(r['template_id'],r['slot_key'],r['trigger_type'],
            ActorScope('scheduler','自动调度',item.scope_admin,item.owner),runtime.pages,runtime.materials,
            planned_publish_at_utc=r['planned_publish_at_utc'],expected_template_version=r['template_version'],
            max_publishable_pages=runtime.max_publishable_pages,max_jobs_per_slot=runtime.max_jobs_per_slot,
            max_daily_jobs=runtime.max_daily_jobs,upcoming_claim=item)
    except (RepositoryError,StoreError) as exc:
        defer(runtime.store,item,exc.code)
        return {'ok': False,'status': 'deferred','run_id': r['id'],'error': exc.code}
