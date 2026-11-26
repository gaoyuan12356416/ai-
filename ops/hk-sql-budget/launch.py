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
        os.execv(sys.executable, [sys.executable, "-B",str(directory/"bridge/preflight.py"),sys.argv[1]])
    os.execv(sys.executable, [sys.executable, "-m", sys.argv[1]])
