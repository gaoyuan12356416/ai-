"""Scoped startup overlay, inherited by campaign_schedule's refresh subprocess."""
import importlib
from importlib.machinery import PathFinder
from importlib.util import module_from_spec
import os
from pathlib import Path
import sys


def _load():
    bridge = Path(__file__).resolve().parent
    old = PathFinder.find_spec("sitecustomize", [p for p in sys.path if Path(p or ".").resolve() != bridge])
    if old and old.loader:
        old.loader.exec_module(module_from_spec(old))
    root = Path(os.environ["HK_SQL_BUDGET_RELEASE"]).resolve()
    if root != bridge.parents[2]:
        raise RuntimeError("unexpected adapter path")
    # A systemd WorkingDirectory is the original application's repository. Its
    # regular 'features' package must keep resolving its other modules there.
    sys.path.insert(0, os.getcwd())
    features = importlib.import_module("features")
    features.__path__.append(str(root/"features"))
    old_client = importlib.import_module("features.gpu_offload.sql_client")
    adapter = importlib.import_module("features.hk_sql_budget.legacy")
    old_client.Broker = adapter.Broker
    old_client.read_session = adapter.read_session


if os.environ.get("HK_SQL_BUDGET_RELEASE"):
    try:
        _load()
    except Exception as ex:
        # Python normally ignores sitecustomize import errors. Falling through
        # would start a real query outside the shared 30-connection budget.
        print("HK private SQL adapter startup failed ("+type(ex).__name__+")", file=sys.stderr, flush=True)
        os._exit(78)
