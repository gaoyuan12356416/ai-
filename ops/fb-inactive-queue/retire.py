"""Retire unsubmitted Facebook auto work and converge its dispatch projection."""
import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

PENDING = "('planned','queued','ready')"
INACTIVE = "('disabled','deleted')"
NOW = "strftime('%Y-%m-%dT%H:%M:%S','now')||'+00:00'"
REASON = "'fb_auto_queue_template_'||t.status"
ELIGIBLE = f"""
    status IN {PENDING} AND lease_owner='' AND lease_expires_at_utc=''
    AND graph_post_id='' AND unknown_outcome=0
    AND EXISTS (SELECT 1 FROM fb_auto_run r WHERE r.id=fb_auto_task.run_id
                AND r.template_id=fb_auto_task.template_id AND r.trigger_type='auto')
    AND NOT EXISTS (SELECT 1 FROM fb_auto_publish_attempt a WHERE a.task_id=fb_auto_task.id)
    AND NOT EXISTS (SELECT 1 FROM fb_auto_publish_ledger l WHERE l.task_id=fb_auto_task.id)
    AND NOT EXISTS (SELECT 1 FROM pipeline_fb_operation o WHERE o.source_key='fb:auto:'||fb_auto_task.id
                    AND o.stage='publish')
    AND NOT EXISTS (SELECT 1 FROM pipeline_canonical_hold h WHERE h.source_key='fb:auto:'||fb_auto_task.id)
"""
MARK = f"""UPDATE fb_auto_task SET status='skipped',
    skip_reason=(SELECT {REASON} FROM fb_auto_template t WHERE t.id=fb_auto_task.template_id),
    error_code=(SELECT {REASON} FROM fb_auto_template t WHERE t.id=fb_auto_task.template_id),
    error_message='自动发布模板已停用或删除，未提交任务已移出队列',completed_at_utc={NOW}
    WHERE {ELIGIBLE} AND """
SLOTS = f"""UPDATE fb_auto_due_slot SET status='missed',
    error_code=(SELECT {REASON} FROM fb_auto_template t WHERE t.id=fb_auto_due_slot.template_id),
    updated_at_utc={NOW} WHERE trigger_type='auto' AND status='pending' AND lease_owner='' AND """
AUDIT = """CREATE TABLE IF NOT EXISTS fb_inactive_queue_log(
    task_id INTEGER PRIMARY KEY,run_id INTEGER NOT NULL,template_id INTEGER NOT NULL,
    old_status TEXT NOT NULL,old_reason TEXT NOT NULL,old_code TEXT NOT NULL,
    old_message TEXT NOT NULL,old_completed TEXT NOT NULL,reason TEXT NOT NULL,
    retired_at TEXT NOT NULL,run_refreshed INTEGER NOT NULL DEFAULT 0)"""
TRIGGERS = {
    'fb_inactive_queue_audit': f"""AFTER UPDATE OF status ON fb_auto_task
        WHEN OLD.status IN {PENDING} AND NEW.status='skipped'
        AND NEW.skip_reason IN ('fb_auto_queue_template_disabled','fb_auto_queue_template_deleted')
        BEGIN INSERT OR REPLACE INTO fb_inactive_queue_log VALUES
        (OLD.id,OLD.run_id,OLD.template_id,OLD.status,OLD.skip_reason,OLD.error_code,OLD.error_message,
         OLD.completed_at_utc,NEW.skip_reason,NEW.completed_at_utc,0); END""",
    'fb_inactive_queue_template': f"""AFTER UPDATE OF status ON fb_auto_template
        WHEN NEW.status IN {INACTIVE} BEGIN {MARK} template_id=NEW.id;
        {SLOTS} template_id=NEW.id; END""",
}
for name, event in [('insert', 'INSERT'), ('return', 'UPDATE OF status')]:
    TRIGGERS['fb_inactive_queue_' + name] = f"""AFTER {event} ON fb_auto_task
        WHEN NEW.status IN {PENDING} AND EXISTS (SELECT 1 FROM fb_auto_template t
        WHERE t.id=NEW.template_id AND t.status IN {INACTIVE})
        BEGIN {MARK} id=NEW.id; END"""


def install(conn):
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(AUDIT)
    conn.execute("CREATE INDEX IF NOT EXISTS fb_inactive_queue_refresh ON fb_inactive_queue_log(run_refreshed,run_id)")
    for name, definition in TRIGGERS.items():
        conn.execute(f"CREATE TRIGGER IF NOT EXISTS {name} {definition}")
    inactive = f"template_id IN (SELECT id FROM fb_auto_template WHERE status IN {INACTIVE})"
    tasks = conn.execute(MARK + inactive).rowcount
    slots = conn.execute(SLOTS + inactive).rowcount
    conn.commit()
    return dict(tasks=tasks, slots=slots)


def refresh_runs(conn):
    from features.fb_auto_posts.core import FBAutoPostStore
    if not conn.execute("SELECT 1 FROM fb_inactive_queue_log WHERE run_refreshed=0 LIMIT 1").fetchone():
        return 0
    conn.execute("BEGIN IMMEDIATE")
    runs = conn.execute("SELECT DISTINCT run_id FROM fb_inactive_queue_log WHERE run_refreshed=0").fetchall()
    now = datetime.now(timezone.utc).isoformat(timespec='seconds')
    for (run_id,) in runs:
        FBAutoPostStore._refresh_run(conn, run_id, now)
    conn.execute("UPDATE fb_inactive_queue_log SET run_refreshed=1 WHERE run_refreshed=0")
    conn.commit()
    return len(runs)


# A native skip is durable. A late collector observation or a claim returning
# to the queue is handled on a later pass. Never retire a live dispatch claim.
MATCH = """FROM fb.fb_inactive_queue_log a
    JOIN fb.fb_auto_task t ON t.id=a.task_id AND t.status='skipped' AND t.skip_reason=a.reason
    JOIN pipeline_job j ON j.source_key='fb:auto:'||a.task_id
    WHERE j.channel='fb' AND j.family='auto'
    AND j.status IN ('queued','ready','failed') AND j.hold_reason<>'metadata_only'
    AND j.external_intent=0 AND j.canonical_receipt=''
    AND NOT EXISTS (SELECT 1 FROM pipeline_publication p WHERE p.source_key=j.source_key)
    AND NOT EXISTS (SELECT 1 FROM pipeline_operation o WHERE o.job_id=j.id)
    AND NOT EXISTS (SELECT 1 FROM pipeline_checkpoint k WHERE k.job_id=j.id)
    AND NOT EXISTS (SELECT 1 FROM pipeline_receipt r WHERE r.job_id=j.id)
    AND NOT EXISTS (SELECT 1 FROM pipeline_cloud_intent i WHERE i.job_id=j.id)
    AND j.id NOT IN (SELECT job_id FROM pipeline_resource)
    AND j.id NOT IN (SELECT job_id FROM pipeline_legacy_resource)
"""


def converge(conn):
    fields = "SELECT j.id,j.status,j.stage,j.source_status,j.hold_reason,j.error_code,a.reason "
    if not conn.execute("SELECT 1 " + MATCH + " LIMIT 1").fetchone():
        return 0
    # BEGIN IMMEDIATE would request a write lock on the read-only FB attachment.
    # Acquire the coordinator write lock before checking current claims.
    conn.execute("BEGIN")
    conn.execute("UPDATE main.pipeline_job SET status=status WHERE 0")
    jobs = conn.execute(fields + MATCH).fetchall()
    now = datetime.now(timezone.utc).isoformat(timespec='milliseconds')
    for row in jobs:
        job, status, stage, source_status, hold, code, reason = row
        detail = dict(status=status, stage=stage, source_status=source_status, hold_reason=hold,
                      error_code=code, reason=reason)
        conn.execute("""INSERT INTO pipeline_event(job_id,event_type,details_json,created_at)
                        VALUES(?,'fb_inactive_template_retired',?,?)""", (job, json.dumps(detail), now))
        conn.execute("""UPDATE pipeline_job SET status='completed',hold_reason='metadata_only',source_status='skipped',
                     finished_at=coalesce(nullif(finished_at,''),?),updated_at=? WHERE id=?""", (now, now, job))
    conn.commit()
    return len(jobs)


def connect(path):
    conn = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=rw', uri=True, timeout=8)
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('install', 'run', 'uninstall'))
    parser.add_argument('--native', required=True)
    parser.add_argument('--dispatch', required=True)
    parser.add_argument('--runtime', required=True)
    args = parser.parse_args()
    sys.path.insert(0, args.runtime)
    with connect(args.native) as native:
        if args.action == 'uninstall':
            native.execute("BEGIN IMMEDIATE")
            for name in TRIGGERS:
                native.execute("DROP TRIGGER IF EXISTS " + name)
            native.commit()
            return
        result = install(native) if args.action == 'install' else {}
        result['runs'] = refresh_runs(native)
    with connect(args.dispatch) as dispatch:
        dispatch.execute("ATTACH DATABASE ? AS fb", (Path(args.native).resolve().as_uri() + '?mode=ro',))
        result['dispatch'] = converge(dispatch)
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
