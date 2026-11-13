"""Compatibility entry at the old report work directory; consumes its pinned catalog."""
from pathlib import Path
import os
import sys

code=Path('/opt/report-material-catalog/current/reports/yuesiying/catalog')
python='/mnt/data-disk/report-material-catalog/venv/bin/python'
os.execv(python,[python,'-B',str(code/'build_localizations.py'),'--workdir',str(Path(__file__).resolve().parent),*sys.argv[1:]])
