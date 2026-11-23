"""A template's material reuse window never expires active or unknown work."""
from datetime import datetime, timedelta, timezone

POLICY = 'x-material-window-v1'
DEFAULT_DAYS = 30


def days(value):
    if type(value) is not int or not 1 <= value <= 3650:
        raise ValueError('material_window_days must be an integer between 1 and 3650')
    return value


def history_conflict(source, account_id, completed_at, now=None):
    """Called only after checking active/uncertain state, including other accounts.

    A missing field retains the old frozen task policy. Known failures retain
    their completed-use timestamp; successful posts use the publication receipt.
    Missing/invalid completion times cannot prove that the window has elapsed.
    """
    window = source.get('_material_window_days') if source else None
    if window is None and source and source.get('_pool_run') and not source['_effect_policy']:
        return True
    if source and str(source['account_id']) != str(account_id):
        return False
    if window is None:
        return True
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=days(window))
    try:
        completed = datetime.fromisoformat(str(completed_at or '').replace('Z', '+00:00'))
        if completed.tzinfo is None:
            return True
    except ValueError:
        return True
    return completed > cutoff


def task_completed_at(task):
    if task.get('published_at_utc'):
        return task['published_at_utc']
    # Never infer successful publication time from selection or grant creation.
    if task['status'] == 'published':
        return ''
    return task.get('finished_at_utc') or task.get('updated_at') or ''


QUEUE_GUARD = """CREATE TRIGGER trg_x_effect_account_dedupe BEFORE INSERT ON x_post_queue
WHEN EXISTS(
    SELECT 1 FROM x_post_manual_run m JOIN x_post_queue q
        ON q.material_key=NEW.material_key AND q.account_id=NEW.account_id
    LEFT JOIN x_post_publish_log l ON l.queue_id=q.id
    LEFT JOIN x_post_repost_ledger relay ON relay.queue_id=q.id
    WHERE m.id=NEW.manual_run_id AND m.reuse_context_json<>'{}' AND (
        json_extract(m.reuse_context_json,'$.material_window_days') IS NULL
        OR q.status NOT IN ('published','failed','cancelled')
        OR l.unknown_outcome=1 OR l.status IN ('post_creating','repost_creating')
        OR relay.unknown_outcome=1 OR relay.status='needs_review'
        OR coalesce(julianday(CASE
            WHEN q.status='published' OR coalesce(l.x_post_id,'')<>'' OR coalesce(relay.repost_id,'')<>''
            THEN coalesce(nullif(relay.reposted_at,''),l.published_at) ELSE q.updated_at END)
            > julianday('now') - json_extract(m.reuse_context_json,'$.material_window_days'),1)
    )
)
BEGIN SELECT RAISE(ABORT,'effect material already used by account'); END"""


def install_queue_guard(db):
    old = db.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name='trg_x_effect_account_dedupe'").fetchone()
    if old and old[0] == QUEUE_GUARD:
        return False
    db.execute('SAVEPOINT material_window_guard')
    try:
        db.execute('DROP TRIGGER IF EXISTS trg_x_effect_account_dedupe')
        db.execute(QUEUE_GUARD)
        db.execute('RELEASE material_window_guard')
    except Exception:
        db.execute('ROLLBACK TO material_window_guard')
        db.execute('RELEASE material_window_guard')
        raise
    return True
