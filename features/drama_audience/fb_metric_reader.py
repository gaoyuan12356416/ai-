"""Open an existing FB metric reader without running unrelated schema writes."""
from contextlib import closing
from pathlib import Path
import sqlite3


def open_existing_metrics(store_class, path):
    class ExistingMetricStore(store_class):
        def ensure_storage(self):
            # Service startup only reads metrics. Collector-owned initialization
            # remains unchanged; an absent/incomplete database must fail clearly.
            uri=Path(self.path).resolve().as_uri()+'?mode=ro'
            with closing(sqlite3.connect(uri,uri=True,timeout=5)) as db:
                for table in ('fb_auto_metric_generation','fb_auto_metric_daily','fb_auto_metric_active_pointer'):
                    db.execute('SELECT * FROM '+table+' LIMIT 0')
    return ExistingMetricStore(path)
