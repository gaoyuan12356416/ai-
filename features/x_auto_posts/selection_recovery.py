"""Execution lifetime fencing and selection-only recovery.

The OS lock proves request lifetime, independently of SQLite heartbeat writes.
Recovery owns admission exclusively and never takes a lock away from an executor.
"""
import contextlib
import datetime as dt
import json
import logging
import os
from pathlib import Path
import threading

LOG = logging.getLogger(__name__)


def stamp():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


@contextlib.contextmanager
def file_lock(directory, name, *, shared=False, blocking=True):
    if not directory:
        yield True
        return
    import fcntl
    path = Path(directory)
    path.mkdir(parents=True, exist_ok=True)
    with (path / name).open("a+") as handle:
        try:
            fcntl.flock(handle, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
                        | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        try:
            yield handle
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def execution_available(directory, task_id):
    with file_lock(directory, str(int(task_id)) + ".lock", blocking=False) as handle:
        return bool(handle)


class ClaimLifetime:
    """Hold the request lock until execution *and* error handling have ended."""
    def __init__(self, store, claim, lock_handle, *, lease_seconds, now_fn,
                 interval=30):
        self.store, self.claim, self.handle = store, claim, lock_handle
        self.lease_seconds, self.now_fn = lease_seconds, now_fn
        self.interval = interval
        self.stop = threading.Event()
        self.thread = None

    def pulse(self):
        with self.store._reader() as conn:
            task = conn.execute("SELECT status,claim_token FROM x_auto_task WHERE id=?",
                                (self.claim.task.id,)).fetchone()
        if task is None or task["claim_token"] != self.claim.reveal_claim_token():
            return False
        seconds = min(self.lease_seconds, 180 if task["status"] == "selecting"
                      else 900 if task["status"] in {"reserved", "preparing"}
                      else self.lease_seconds)
        self.store.renew_task_claim(self.claim.task.id, self.claim.reveal_claim_token(),
                                   lease_seconds=seconds, now=self.now_fn())
        return True

    def loop(self):
        while not self.stop.wait(self.interval):
            try:
                if not self.pulse():
                    return
            except Exception as exc:
                # Retain the OS lock on heartbeat failure. Expiry alone must not
                # start a second request while the first is still executing.
                LOG.warning("selection_heartbeat_failed task=%s error=%s",
                            self.claim.task.id, type(exc).__name__)

    def __enter__(self):
        self.thread = threading.Thread(target=self.loop, daemon=True,
                                       name="x-auto-claim-heartbeat")
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stop.set()
        # Do not release the lifetime lock while a DB renewal is still pending.
        self.thread.join()


def pure_selection(row):
    return (row["status"] == "selecting" and row["claim_phase"] == "selection"
            and not any(row[key] for key in (
                "material_id", "execution_run_id", "execution_queue_id",
                "execution_log_id", "publish_id", "unknown_outcome",
                "preparation_attempt_count", "publish_attempt_count",
                "reserved_at_utc", "bridged_at_utc", "published_at_utc")))


def recover_proven_selection(store, task_id, expected_updated_at, *, proof):
    """Caller holds admission + per-task OS locks and has checked other ledgers."""
    now = store._now_iso()
    with store._transaction() as conn:
        row = conn.execute("SELECT * FROM x_auto_task WHERE id=?", (task_id,)).fetchone()
        if (row is None or not pure_selection(row)
                or row["updated_at"] != expected_updated_at
                or conn.execute("SELECT 1 FROM x_auto_material_ledger WHERE task_id=?",
                                (task_id,)).fetchone()):
            return False
        conn.execute("UPDATE x_auto_task SET status='retry_wait',claim_phase='selection',"
                     "claim_worker='',claim_token='',lease_expires_at_utc='',"
                     "next_attempt_at_utc=?,updated_at=?,error_code='',error_message='' "
                     "WHERE id=?", (now, now, task_id))
        store._event(conn, run_id=row["run_id"], task_id=task_id,
                     event_type="task_selection_claim_recovered", created_at=now,
                     from_status="selecting", to_status="retry_wait",
                     message="Ended selection request recovered after ledger checks",
                     details=proof)
    return True
