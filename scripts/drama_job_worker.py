#!/usr/bin/env python3
"""External drama synthesis worker with SQLite leasing and heartbeat."""

import argparse
import logging
import os
import signal
import socket
import sqlite3
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app as drama_app  # noqa: E402
from features.drama_synthesis.cpu_runtime import LeaseIdentity, LeaseLostError, get_remote_status  # noqa: E402
from features.drama_synthesis.remote_client import RemoteJobError, RemotePollingInterrupted  # noqa: E402


RUNNING_JOB_STATUSES = ("queued", "validating", "downloading", "processing_cover", "rendering")
TERMINAL_LEASE_STATUSES = ("done", "failed", "missing", "deleted")
STOP_EVENT = threading.Event()


def env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def env_int(name, default):
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


DB_PATH = os.environ.get("DRAMA_JOB_DB_PATH", drama_app.JOB_DB_PATH)
WORKER_ID = os.environ.get("DRAMA_JOB_WORKER_ID") or "%s:%s" % (socket.gethostname(), os.getpid())
POLL_SECONDS = max(1, env_int("DRAMA_JOB_WORKER_POLL_SECONDS", 10))
HEARTBEAT_SECONDS = max(1, env_int("DRAMA_JOB_WORKER_HEARTBEAT_SECONDS", 15))
STALE_SECONDS = max(60, env_int("DRAMA_JOB_WORKER_STALE_SECONDS", 900))
RECOVER_LEGACY = env_bool("DRAMA_JOB_WORKER_RECOVER_LEGACY", False)


def now_text():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def parse_time(value):
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(str(value).split(".")[0], fmt)
        except ValueError:
            continue
    return None


def is_stale(value):
    parsed = parse_time(value)
    if not parsed:
        return True
    return (datetime.now() - parsed).total_seconds() >= STALE_SECONDS


def connect_db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def ensure_worker_tables(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS drama_material_job_worker_lease (
          job_id TEXT PRIMARY KEY,
          worker_id TEXT NOT NULL DEFAULT '',
          pid INTEGER NOT NULL DEFAULT 0,
          status TEXT NOT NULL DEFAULT 'idle',
          claimed_at TEXT NOT NULL DEFAULT '',
          heartbeat_at TEXT NOT NULL DEFAULT '',
          released_at TEXT NOT NULL DEFAULT '',
          attempt INTEGER NOT NULL DEFAULT 0,
          last_error TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS drama_material_job_worker_event (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          job_id TEXT NOT NULL DEFAULT '',
          worker_id TEXT NOT NULL DEFAULT '',
          event TEXT NOT NULL DEFAULT '',
          detail TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_drama_job_worker_event_job_id ON drama_material_job_worker_event(job_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_drama_job_worker_lease_status_heartbeat "
        "ON drama_material_job_worker_lease(status, heartbeat_at)"
    )
    conn.commit()


def event(conn, job_id, name, detail=""):
    conn.execute(
        """
        INSERT INTO drama_material_job_worker_event (job_id, worker_id, event, detail, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (job_id, WORKER_ID, name, detail[:2000], now_text()),
    )


def active_fresh_lease(row):
    return row["lease_status"] == "running" and not is_stale(row["heartbeat_at"])


def row_can_be_claimed(row):
    status = row["job_status"]
    if active_fresh_lease(row):
        return False
    if status == "queued":
        return True
    if row["lease_status"] == "interrupted":
        return True
    if row["lease_job_id"] and is_stale(row["heartbeat_at"]):
        return True
    if RECOVER_LEGACY and is_stale(row["updated_at"]):
        return True
    return False


def claim_next_job():
    conn = connect_db()
    try:
        ensure_worker_tables(conn)
        conn.execute("BEGIN IMMEDIATE")
        placeholders = ", ".join("?" for _ in RUNNING_JOB_STATUSES)
        rows = conn.execute(
            """
            SELECT
              j.job_id,
              j.status AS job_status,
              j.updated_at,
              l.job_id AS lease_job_id,
              l.status AS lease_status,
              l.heartbeat_at
            FROM drama_material_job j
            LEFT JOIN drama_material_job_worker_lease l ON l.job_id = j.job_id
            WHERE j.status IN ({placeholders})
            ORDER BY CASE WHEN j.status = 'queued' THEN 0 ELSE 1 END, j.updated_at ASC
            LIMIT 50
            """.format(placeholders=placeholders),
            RUNNING_JOB_STATUSES,
        ).fetchall()
        selected = next((row for row in rows if row_can_be_claimed(row)), None)
        if not selected:
            conn.rollback()
            return None
        job_id = selected["job_id"]
        existing = conn.execute(
            "SELECT attempt FROM drama_material_job_worker_lease WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        attempt = int(existing["attempt"]) + 1 if existing else 1
        if existing:
            conn.execute(
                """
                UPDATE drama_material_job_worker_lease
                SET worker_id = ?, pid = ?, status = 'running', claimed_at = ?,
                    heartbeat_at = ?, released_at = '', attempt = ?, last_error = ''
                WHERE job_id = ?
                """,
                (WORKER_ID, os.getpid(), now_text(), now_text(), attempt, job_id),
            )
        else:
            conn.execute(
                """
                INSERT INTO drama_material_job_worker_lease
                  (job_id, worker_id, pid, status, claimed_at, heartbeat_at, released_at, attempt, last_error)
                VALUES (?, ?, ?, 'running', ?, ?, '', ?, '')
                """,
                (job_id, WORKER_ID, os.getpid(), now_text(), now_text(), attempt),
            )
        event(conn, job_id, "claimed", selected["job_status"])
        conn.commit()
        return job_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def owned_lease(job_id):
    conn = connect_db()
    try:
        row = conn.execute(
            "SELECT worker_id,attempt FROM drama_material_job_worker_lease WHERE job_id=? AND worker_id=? AND status='running'",
            (job_id, WORKER_ID),
        ).fetchone()
        if row is None:
            raise LeaseLostError()
        return LeaseIdentity(str(row["worker_id"]), int(row["attempt"]))
    finally:
        conn.close()


def update_heartbeat(job_id, attempt):
    conn = connect_db()
    try:
        changed = conn.execute(
            """
            UPDATE drama_material_job_worker_lease
            SET heartbeat_at = ?, pid = ?
            WHERE job_id = ? AND worker_id = ? AND attempt = ? AND status = 'running'
            """,
            (now_text(), os.getpid(), job_id, WORKER_ID, attempt),
        )
        conn.commit()
        return changed.rowcount == 1
    finally:
        conn.close()


def heartbeat_loop(job_id, stop_event, attempt, observer_stop):
    while not stop_event.wait(HEARTBEAT_SECONDS):
        try:
            if not update_heartbeat(job_id, attempt):
                observer_stop.set()
                return
        except Exception:
            logging.exception("heartbeat failed: %s", job_id)


def release_lease(job_id, status, error="", *, attempt):
    conn = connect_db()
    try:
        ensure_worker_tables(conn)
        changed = conn.execute(
            """
            UPDATE drama_material_job_worker_lease
            SET status = ?, released_at = ?, heartbeat_at = ?, last_error = ?
            WHERE job_id = ? AND worker_id = ? AND attempt = ? AND status = 'running'
            """,
            (status, now_text(), now_text(), error[:2000], job_id, WORKER_ID, attempt),
        )
        if changed.rowcount:
            event(conn, job_id, "released", "%s %s" % (status, error[:500]))
        conn.commit()
    finally:
        conn.close()


def prepare_job_for_run(job):
    drama_app.clear_job_deleted_marker(job["job_id"])
    job["error_message"] = ""
    job["progress_detail"] = "\u540e\u53f0 worker \u4ece\u65ad\u70b9\u7ee7\u7eed\u6267\u884c"
    if get_remote_status(DB_PATH, job["job_id"]):
        # Remote jobs finish only through the verified GPU result plus atomic
        # consumption.  URL presence must not bypass a failed cache check.
        job["status"] = "queued"
        job["progress"] = max(2, drama_app.clamp_progress(job.get("progress", 0)))
        job["progress_detail"] = "继续跟踪原制作任务"
        drama_app.upsert_job_record(job)
        return True
    # Legacy jobs do not use atomic_complete_job to replace a failure notice.
    # Preserve their original retry contract so success can be notified again.
    job["completion_notified_at"] = ""
    job["completion_notification_error"] = ""
    if drama_app.selected_job_outputs_ready(job):
        job["status"] = "done"
        job["progress"] = 100
        job["progress_detail"] = "\u5168\u90e8\u4ea7\u7269\u5df2\u751f\u6210"
        drama_app.upsert_job_record(job)
        notify_without_restarting_media(job)
        return False
    if job.get("output_video_url") and not job.get("output_video_no_bgm_url"):
        job["status"] = "rendering"
        job["progress"] = max(82, drama_app.clamp_progress(job.get("progress", 0)))
    elif job.get("cover_16x9_url"):
        job["status"] = "processing_cover"
        job["progress"] = max(44, drama_app.clamp_progress(job.get("progress", 0)))
    else:
        job["status"] = "queued"
        job["progress"] = max(2, drama_app.clamp_progress(job.get("progress", 0)))
    drama_app.upsert_job_record(job)
    return True


def notify_without_restarting_media(job):
    try:
        drama_app.notify_job_creator_on_completion(job)
    except Exception:
        logging.exception("job notification failed; media state retained: %s", job["job_id"])


def mark_job_failed(job, exc):
    message = str(exc).strip() or exc.__class__.__name__
    trace = traceback.format_exc(limit=8)
    job["status"] = "failed"
    job["progress"] = drama_app.clamp_progress(job.get("progress", 0))
    job["error_message"] = message if isinstance(exc, RemoteJobError) else "%s\n%s" % (message, trace)
    drama_app.upsert_job_record(job)
    notify_without_restarting_media(job)
    return message


def should_auto_retry(exc):
    if isinstance(exc, (RemoteJobError, RemotePollingInterrupted, LeaseLostError)):
        return False
    checker = getattr(drama_app, "should_auto_retry_job", None)
    return bool(checker and checker(exc))


class ObserverStop:
    """Observe service shutdown and lease loss without cancelling GPU work."""

    def __init__(self, lost):
        self.lost = lost

    def is_set(self):
        return STOP_EVENT.is_set() or self.lost.is_set()

    def wait(self, seconds):
        deadline = time.monotonic() + max(0, float(seconds))
        while not self.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            self.lost.wait(min(remaining, 0.25))
        return True


def run_claimed_job(job_id):
    try:
        identity = owned_lease(job_id)
    except LeaseLostError:
        logging.info("job lease no longer owned: %s", job_id)
        return
    release_status = "interrupted"
    release_error = ""
    heartbeat_stop = threading.Event()
    observer_lost = threading.Event()
    observer_stop = ObserverStop(observer_lost)
    heartbeat = threading.Thread(target=heartbeat_loop, args=(job_id, heartbeat_stop, identity.attempt, observer_lost), daemon=True)
    heartbeat.start()
    try:
        attempts = max(1, int(getattr(drama_app, "JOB_AUTO_RETRY_ATTEMPTS", 1)) + 1)
        for attempt in range(1, attempts + 1):
            job = drama_app.fetch_job_row(job_id)
            if not job:
                release_status = "missing"
                return
            job["_fenced_lease"] = identity.as_dict()
            job["_remote_stop_event"] = observer_stop
            try:
                if observer_stop.is_set():
                    raise RemotePollingInterrupted()
                if attempt > 1:
                    drama_app.set_job_progress(
                        job,
                        status="queued",
                        progress=max(2, drama_app.clamp_progress(job.get("progress", 0))),
                        detail="\u4efb\u52a1\u5931\u8d25\uff0c\u540e\u53f0 worker \u5f00\u59cb\u81ea\u52a8\u91cd\u8bd5\uff08\u7b2c %d/%d \u6b21\uff09"
                        % (attempt, attempts),
                    )
                if not prepare_job_for_run(job):
                    release_status = "done"
                    return
                drama_app.process_job(job)
                final_job = drama_app.fetch_job_row(job_id) or job
                release_status = final_job.get("status") or "done"
                if release_status not in TERMINAL_LEASE_STATUSES:
                    release_status = "interrupted"
                return
            except (RemotePollingInterrupted, LeaseLostError) as exc:
                logging.info("observer stopped without changing media result: %s %s", job_id, exc)
                release_status = "interrupted"
                release_error = str(exc)
                return
            except Exception as exc:
                logging.exception("job failed: %s", job_id)
                if drama_app.is_job_deleted(job_id) and str(exc).strip() == "job deleted":
                    release_status = "deleted"
                    return
                # Completion has already committed; a later notification error
                # must not turn it into a failed job or start another renderer.
                persisted = drama_app.fetch_job_row(job_id)
                if persisted and persisted.get("status") == "done":
                    release_status = "done"
                    return
                if attempt < attempts and should_auto_retry(exc):
                    continue
                release_status = "failed"
                try:
                    release_error = mark_job_failed(job, exc)
                except LeaseLostError:
                    release_status = "interrupted"
                return
    finally:
        heartbeat_stop.set()
        heartbeat.join(timeout=HEARTBEAT_SECONDS + 2)
        release_lease(job_id, release_status, release_error, attempt=identity.attempt)


def handle_signal(signum, _frame):
    logging.info("received signal %s, stopping worker after current job", signum)
    STOP_EVENT.set()


def main():
    parser = argparse.ArgumentParser(description="Run drama synthesis jobs outside the API process.")
    parser.add_argument("--once", action="store_true", help="Process at most one available job and exit.")
    parser.add_argument("--init-only", action="store_true", help="Create worker tables and exit.")
    parser.add_argument("--log-level", default=os.environ.get("DRAMA_JOB_WORKER_LOG_LEVEL", "INFO"))
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    conn = connect_db()
    try:
        ensure_worker_tables(conn)
    finally:
        conn.close()
    if args.init_only:
        return 0

    logging.info(
        "drama job worker started: worker_id=%s poll=%ss heartbeat=%ss stale=%ss recover_legacy=%s",
        WORKER_ID,
        POLL_SECONDS,
        HEARTBEAT_SECONDS,
        STALE_SECONDS,
        RECOVER_LEGACY,
    )
    while not STOP_EVENT.is_set():
        job_id = claim_next_job()
        if job_id:
            logging.info("processing job: %s", job_id)
            run_claimed_job(job_id)
            if args.once:
                return 0
            continue
        if args.once:
            logging.info("no job available")
            return 0
        STOP_EVENT.wait(POLL_SECONDS)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
