"""Start an existing HK program with the SQL adapter scoped to its process tree."""
import os
from pathlib import Path
import sys


if __name__ == "__main__":
    modules = ("features.gpu_offload.metadata_reader", "features.gpu_offload.campaign_schedule")
    if len(sys.argv) != 2 or sys.argv[1] not in modules:
        raise SystemExit("unknown HK reader entry point")
    directory = Path(__file__).resolve().parent
    old = os.environ.get("PYTHONPATH")
    os.environ["HK_SQL_BUDGET_RELEASE"] = str(directory.parent.parent)
    os.environ["PYTHONPATH"] = os.pathsep.join(p for p in (str(directory/"bridge"),old) if p)
    os.execv(sys.executable, [sys.executable, "-m", sys.argv[1]])
