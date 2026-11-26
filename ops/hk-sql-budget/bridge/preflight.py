"""Verify the old consumer can import its overlay and use the shared ledger without opening MySQL."""
import importlib.util
import os
from pathlib import Path
import sqlite3
import sys

import pymysql
from features.gpu_offload import sql_client
from features.hk_sql_budget import legacy
from features.hk_sql_budget.ledger import DEFAULT_DIRECTORY


def check(module):
    if sql_client.read_session is not legacy.read_session or sql_client.Broker is not legacy.Broker:
        raise RuntimeError("old_client_not_adapted")
    if importlib.util.find_spec(module) is None:
        raise RuntimeError("original_entrypoint_missing")
    path = Path(os.environ.get("HK_SQL_BUDGET_DIR", DEFAULT_DIRECTORY))/"leases.sqlite3"
    db = sqlite3.connect(path.resolve().as_uri()+"?mode=rw",uri=True,timeout=5)
    try:
        db.execute("BEGIN IMMEDIATE")
        db.execute("SELECT capacity FROM config WHERE id=1").fetchone()
        db.rollback()
    finally:
        db.close()
    print("hk_sql_consumer_preflight_ok")


if __name__=="__main__":
    check(sys.argv[1])
