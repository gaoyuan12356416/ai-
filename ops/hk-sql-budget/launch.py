"""Start an existing HK program with the SQL adapter scoped to its process tree."""
import os
from pathlib import Path
import sys


if __name__ == "__main__":
    modules = ("features.gpu_offload.metadata_reader", "features.gpu_offload.campaign_schedule")
    checking = len(sys.argv) == 3 and sys.argv[2] == "--check"
    if (not checking and len(sys.argv) != 2) or sys.argv[1] not in modules:
        raise SystemExit("unknown HK reader entry point")
    directory = Path(__file__).resolve().parent
    old = os.environ.get("PYTHONPATH")
    os.environ["HK_SQL_BUDGET_RELEASE"] = str(directory.parent.parent)
    os.environ["PYTHONPATH"] = os.pathsep.join(p for p in (str(directory/"bridge"),old) if p)
    if checking:
        probe = """
import importlib.util, os, pathlib, sqlite3, sys
import pymysql
from features.gpu_offload import sql_client
from features.hk_sql_budget import legacy
from features.hk_sql_budget.ledger import DEFAULT_DIRECTORY
if sql_client.read_session is not legacy.read_session or sql_client.Broker is not legacy.Broker:
    raise RuntimeError('old_client_not_adapted')
if importlib.util.find_spec(sys.argv[1]) is None:
    raise RuntimeError('original_entrypoint_missing')
path = pathlib.Path(os.environ.get('HK_SQL_BUDGET_DIR', DEFAULT_DIRECTORY)) / 'leases.sqlite3'
db = sqlite3.connect(path.resolve().as_uri()+'?mode=rw',uri=True,timeout=5)
try:
    db.execute('BEGIN IMMEDIATE')
    db.execute('SELECT capacity FROM config WHERE id=1').fetchone()
    db.rollback()
finally:
    db.close()
print('hk_sql_consumer_preflight_ok')
"""
        os.execv(sys.executable, [sys.executable, "-B", "-c", probe, sys.argv[1]])
    os.execv(sys.executable, [sys.executable, "-m", sys.argv[1]])
