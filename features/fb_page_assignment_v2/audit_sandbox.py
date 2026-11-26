"""Explicit injection points for offline and network-isolated integration tests."""
from __future__ import annotations

import json
import re
import sqlite3
import subprocess
from contextlib import closing, contextmanager
from pathlib import Path

from .audit import COLS, MySQLSink, missing_records, verify
from .ledger import digest


class SQLiteAuditSink:
    """Exercise actual transaction commits, primary keys, digests and readback locally."""
    def __init__(self, path):
        self.db = str(path)
        with closing(sqlite3.connect(self.db)) as c:
            c.execute("PRAGMA journal_mode=WAL")
            ints = {"item_index", "pool_group_id", "http_status", "revision"}
            c.execute("CREATE TABLE IF NOT EXISTS audit(" +
                      ",".join(k + (" INTEGER" if k in ints else " TEXT") for k in COLS) +
                      ",PRIMARY KEY(task_id,item_index))")
            c.commit()

    @staticmethod
    def _read(c, expected):
        found = {}
        ids = sorted({k[0] for k in expected})
        for i in range(0, len(ids), 100):
            batch = ids[i:i+100]
            rows = c.execute("SELECT task_id,item_index,revision,payload_hash,SHA256(diagnostics_json),"
                             "external_task_id,pool_group_id,drama_id,page_id,status,selection_stage,error_code,"
                             "http_status FROM audit WHERE task_id IN (" + ",".join(["?"] * len(batch)) +
                             ")", batch)
            for row in rows:
                found[row[0], row[1]] = tuple(row[2:])
        return found

    def write(self, records, expected):
        with closing(sqlite3.connect(self.db, timeout=5)) as c:
            c.create_function("SHA256", 1, lambda s: digest(s.encode()))
            c.execute("BEGIN IMMEDIATE")
            missing = missing_records(records, expected, self._read(c, expected))
            converted = [tuple(v.isoformat() if hasattr(v, "isoformat") else v for v in row) for row in missing]
            c.executemany("INSERT INTO audit VALUES(" + ",".join(["?"] * len(COLS)) +
                          ") ON CONFLICT(task_id,item_index) DO NOTHING", converted)
            c.commit()
            verify(expected, self._read(c, expected))


def isolated_mysql_sink(container):
    """Connect only through the UNIX socket of a running network=none QA container."""
    import pymysql
    if not re.fullmatch(r"fb-page-v2-qa-[A-Za-z0-9_-]+", container):
        raise ValueError("invalid_qa_container_name")
    doc = json.loads(subprocess.check_output(["docker", "inspect", container], timeout=5))[0]
    if doc["HostConfig"]["NetworkMode"] != "none" or not doc["State"]["Running"]:
        raise ValueError("mysql_qa_container_must_be_isolated")
    socket = "/proc/%d/root/var/run/mysqld/mysqld.sock" % doc["State"]["Pid"]
    def connect():
        return pymysql.connect(unix_socket=socket, user="root", password="", charset="utf8mb4",
                               connect_timeout=4, read_timeout=8, write_timeout=8, autocommit=True)
    with closing(connect()) as conn, conn.cursor() as c:
        c.execute("SELECT @@hostname")
        if not doc["Id"].startswith(c.fetchone()[0]):
            raise ValueError("isolated_mysql_identity_mismatch")
        c.execute("CREATE DATABASE IF NOT EXISTS ads_ai")
        schema = Path(__file__).parents[2] / "features/fb_page_assignment/schema.sql"
        c.execute("\n".join(line for line in schema.read_text().splitlines() if not line.startswith("--")))

    @contextmanager
    def writer(role, deadline):
        if role not in ("read","write"):
            raise ValueError("unexpected_sandbox_role")
        with closing(connect()) as conn:
            with conn.cursor() as cur:
                cur.execute("START TRANSACTION READ ONLY" if role=="read" else "START TRANSACTION READ WRITE")
            try:
                yield conn
            finally:
                conn.rollback()

    return MySQLSink(writer)
