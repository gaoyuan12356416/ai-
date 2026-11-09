"""Initialize empty business stores and a private copy of recent metrics only."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3

from .policy import ID_BASE, initialize_ids, lane_paths


def initialize(source_metrics_path='/mnt/data-disk/x-auto-post-publisher/x-auto-post.sqlite3'):
    from features.x_posts.service import XPostStore
    from features.x_posts.attribution import SCHEMA
    from features.x_auto_posts.core import XAutoPostStore
    from .runtime import AUDIT_DDL
    paths = lane_paths()
    for path in paths.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            raise RuntimeError('bootstrap_requires_new_business_databases')
    XPostStore(paths['X_POST_DB_PATH'])
    XAutoPostStore(paths['X_AUTO_POST_DB_PATH'])
    for path in paths.values():
        initialize_ids(path)
    with closing(sqlite3.connect(str(paths['X_POST_DB_PATH']))) as db, db:
        db.execute(SCHEMA)
        db.execute(AUDIT_DDL)
        db.execute("UPDATE x_post_schedule_config SET enabled=0,account_ids_json='[]',publish_times_json='[]',random_daily_count=0")
    old = Path(source_metrics_path).resolve().as_uri() + '?mode=ro'
    first = (datetime.now(timezone.utc).date() - timedelta(days=31)).isoformat()
    with closing(sqlite3.connect(str(paths['X_AUTO_POST_DB_PATH']), uri=True, timeout=60)) as db, db:
        db.execute('ATTACH DATABASE ? AS metrics_source', (old,))
        db.execute('BEGIN IMMEDIATE')
        columns = [r[1] for r in db.execute('PRAGMA table_info(x_auto_metric_generation)')]
        selected = ','.join('g.id+%d' % ID_BASE if col == 'id' else 'g.' + col for col in columns)
        db.execute('INSERT INTO x_auto_metric_generation SELECT ' + selected +
            ' FROM metrics_source.x_auto_metric_generation g JOIN metrics_source.x_auto_metric_active_pointer p ON p.generation_id=g.id WHERE g.status=\'ready\' AND p.metric_date>=?', (first,))
        db.execute('INSERT INTO x_auto_metric_daily SELECT d.generation_id+?,d.content_id,d.material_id,d.spend,d.af_revenue0 '
            'FROM metrics_source.x_auto_metric_daily d JOIN metrics_source.x_auto_metric_active_pointer p ON p.generation_id=d.generation_id '
            'JOIN metrics_source.x_auto_metric_generation g ON g.id=p.generation_id WHERE g.status=\'ready\' AND p.metric_date>=?', (ID_BASE, first))
        db.execute('INSERT INTO x_auto_metric_active_pointer SELECT p.platform,p.metric_date,p.product,p.generation_id+?,p.activated_by_user_id,p.activated_by_name,p.activated_at_utc '
            'FROM metrics_source.x_auto_metric_active_pointer p JOIN metrics_source.x_auto_metric_generation g ON g.id=p.generation_id '
            'WHERE g.status=\'ready\' AND p.metric_date>=?', (ID_BASE, first))
        count = db.execute('SELECT COUNT(*) FROM x_auto_metric_active_pointer').fetchone()[0]
        if db.execute('PRAGMA foreign_key_check').fetchone():
            raise RuntimeError('metric_copy_inconsistent')
    return dict(business_empty=True, metric_days=count, queue_id_floor=ID_BASE)


if __name__ == '__main__':
    print(json.dumps(initialize()))
