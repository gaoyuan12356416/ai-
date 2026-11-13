"""Read-only execution identity for the explicitly authorized Run 446 retry.

Normal execution never creates or updates this audit table.  The operational
plan/apply script is the only writer, after proving the prior run has no queue.
"""
import contextlib
import hashlib
import json
from pathlib import Path
import sqlite3

TABLE = "x_auto_operator_retry"
TARGET = {"task_id": 2712, "run_id": 446, "account_id": "46",
          "template_id": 4, "template_version": 6, "prior_run_id": 1657}
GENERATION = 1
EXTERNAL_KEY = "x-auto-task-2712-retry-1"
SCHEMA = """
CREATE TABLE IF NOT EXISTS x_auto_operator_retry (
 task_id INTEGER PRIMARY KEY, generation INTEGER NOT NULL CHECK(generation=1),
 run_id INTEGER NOT NULL, account_id TEXT NOT NULL,
 template_id INTEGER NOT NULL, template_version INTEGER NOT NULL,
 prior_run_id INTEGER NOT NULL, external_task_key TEXT NOT NULL UNIQUE,
 identity_sha256 TEXT NOT NULL, config_sha256 TEXT NOT NULL,
 prior_task_sha256 TEXT NOT NULL, prior_run_sha256 TEXT NOT NULL,
 plan_sha256 TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT NOT NULL,
 created_at TEXT NOT NULL
)
"""


class OperatorRetryError(RuntimeError):
    code = "x_auto_operator_retry_identity_invalid"
    status = 409


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def frozen_identity(task):
    return {
        "task_id": int(task.id), "run_id": int(task.run_id),
        "account_id": str(task.account_id), "template_id": int(task.template_id),
        "template_version": int(task.template_version), "language": task.language,
        "account_snapshot_version": int(task.account_snapshot_version),
        "account_snapshot": task.account_snapshot,
        "scheduled_at_utc": task.scheduled_at_utc,
    }


def execution_key(store, task):
    """Use the normal key unless an exact, valid operator receipt exists."""
    default = f"x-auto-task-{task.id}"
    path = getattr(store, "db_path", None)
    if not path:
        return default
    with contextlib.closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro",
                                          uri=True, timeout=5)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                            (TABLE,)).fetchone():
            return default
        row = conn.execute("SELECT * FROM x_auto_operator_retry WHERE task_id=?",
                           (task.id,)).fetchone()
        if row is None:
            return default
        actual = frozen_identity(task)
        valid = all(row[key] == value for key, value in TARGET.items())
        valid = valid and all(actual[key] == value for key, value in TARGET.items()
                              if key != "prior_run_id")
        run = conn.execute("SELECT config_sha256 FROM x_auto_run WHERE id=?",
                           (task.run_id,)).fetchone()
        valid = (valid and row["generation"] == GENERATION
                 and row["external_task_key"] == EXTERNAL_KEY
                 and row["identity_sha256"] == fingerprint(actual)
                 and run is not None and row["config_sha256"] == run["config_sha256"]
                 and all(len(row[key]) == 64 for key in
                         ("prior_task_sha256", "prior_run_sha256", "plan_sha256"))
                 and bool(row["actor"] and row["reason"] and row["created_at"]))
        if not valid:
            raise OperatorRetryError("operator retry receipt does not match the frozen task")
        return EXTERNAL_KEY
