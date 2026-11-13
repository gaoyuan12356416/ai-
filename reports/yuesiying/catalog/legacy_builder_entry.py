"""Compatibility entry at the old report work directory; consumes its pinned catalog."""
from pathlib import Path
import runpy
import sys

code=Path('/opt/report-material-catalog/current/reports/yuesiying/catalog')
sys.path.insert(0,str(code))
sys.argv=[str(code/'build_localizations.py'),'--workdir',str(Path(__file__).resolve().parent),*sys.argv[1:]]
runpy.run_path(str(code/'build_localizations.py'),run_name='__main__')
