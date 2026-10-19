"""Additive random-video settings and immutable queue preparation receipts."""
from __future__ import annotations

import contextlib
import json
import sqlite3
from datetime import datetime, timezone

FLAG = "random_template_enabled"
TABLES = ("x_post_queue", "x_post_schedule_config", "x_post_schedule_run",
          "x_post_schedule_random_plan", "x_post_manual_run")


def migrate(conn):
    for table in TABLES:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)}
        if FLAG not in columns:
            conn.execute("ALTER TABLE %s ADD COLUMN %s INTEGER NOT NULL DEFAULT 0 CHECK(%s IN (0,1))" % (table, FLAG, FLAG))
            if table == "x_post_schedule_config":
                for suffix in ("insert", "update"):
                    conn.execute("DROP TRIGGER IF EXISTS trg_x_post_schedule_config_audit_" + suffix)
    for table in ("x_post_queue", "x_post_schedule_run", "x_post_manual_run"):
        conn.execute("""CREATE TRIGGER IF NOT EXISTS trg_%s_random_template_frozen
            BEFORE UPDATE OF random_template_enabled ON %s
            WHEN OLD.random_template_enabled<>NEW.random_template_enabled
            BEGIN SELECT RAISE(ABORT, 'random template mode is frozen'); END""" % (table, table))
    conn.execute("""CREATE TRIGGER IF NOT EXISTS trg_x_post_queue_random_template_parent
        BEFORE INSERT ON x_post_queue
        WHEN NEW.random_template_enabled<>COALESCE(
          (SELECT random_template_enabled FROM x_post_manual_run WHERE id=NEW.manual_run_id),
          (SELECT random_template_enabled FROM x_post_schedule_run WHERE id=NEW.schedule_run_id),0)
        BEGIN SELECT RAISE(ABORT, 'random template parent mismatch'); END""")
    conn.execute("""CREATE TABLE IF NOT EXISTS x_post_random_template_preparation (
        queue_id INTEGER PRIMARY KEY REFERENCES x_post_queue(id),
        job_key TEXT NOT NULL UNIQUE,
        request_json TEXT NOT NULL,
        result_json TEXT NOT NULL DEFAULT '{}',
        status TEXT NOT NULL CHECK(status IN ('frozen','ready','failed')),
        error_code TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
    conn.execute("""CREATE TRIGGER IF NOT EXISTS trg_x_post_random_template_request_frozen
        BEFORE UPDATE OF queue_id,job_key,request_json ON x_post_random_template_preparation
        WHEN NEW.queue_id<>OLD.queue_id OR NEW.job_key<>OLD.job_key OR NEW.request_json<>OLD.request_json
        BEGIN SELECT RAISE(ABORT, 'random template request is frozen'); END""")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class PreparationStore:
    def __init__(self, db_path):
        self.db_path = str(db_path)

    @contextlib.contextmanager
    def connect(self):
        with contextlib.closing(sqlite3.connect(self.db_path, timeout=30)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn

    def get(self, queue_id):
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM x_post_random_template_preparation WHERE queue_id=?", (queue_id,)).fetchone()
        if row is None:
            return None
        return {**dict(row), "request": json.loads(row["request_json"]), "result": json.loads(row["result_json"])}

    def freeze(self, queue_id, request):
        stamp = datetime.now(timezone.utc).isoformat()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            queue = conn.execute("SELECT random_template_enabled FROM x_post_queue WHERE id=?", (queue_id,)).fetchone()
            if queue is None or queue[0] != 1:
                raise ValueError("random template queue is not enabled")
            existing = conn.execute("SELECT request_json FROM x_post_random_template_preparation WHERE queue_id=?", (queue_id,)).fetchone()
            if existing is not None:
                if existing[0] != canonical(request):
                    raise ValueError("random template source or identity changed")
            else:
                conn.execute("INSERT INTO x_post_random_template_preparation(queue_id,job_key,request_json,status,created_at,updated_at) VALUES(?,?,?,'frozen',?,?)",
                             (queue_id, request["job_key"], canonical(request), stamp, stamp))
            conn.commit()
        return self.get(queue_id)

    def complete(self, queue_id, result):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT status,result_json FROM x_post_random_template_preparation WHERE queue_id=?", (queue_id,)).fetchone()
            if row is None or (row["status"] == "ready" and row["result_json"] != canonical(result)):
                raise ValueError("random template receipt changed")
            conn.execute("UPDATE x_post_random_template_preparation SET status='ready',result_json=?,error_code='',updated_at=? WHERE queue_id=?",
                         (canonical(result), datetime.now(timezone.utc).isoformat(), queue_id))
            conn.commit()

    def fail(self, queue_id, code):
        with self.connect() as conn:
            conn.execute("UPDATE x_post_random_template_preparation SET status='failed',error_code=?,updated_at=? WHERE queue_id=? AND status<>'ready'",
                         (str(code)[:96], datetime.now(timezone.utc).isoformat(), queue_id))
            conn.commit()
