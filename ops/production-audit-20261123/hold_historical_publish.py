"""Freeze only historical publication phases before authorized preparation recovery."""
import argparse
import datetime as dt
import json
from pathlib import Path
import sqlite3


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--db', required=True)
    p.add_argument('--evidence', required=True)
    p.add_argument('--apply', action='store_true')
    a = p.parse_args()
    c = sqlite3.connect(a.db, timeout=30, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute('BEGIN IMMEDIATE' if a.apply else 'BEGIN')
    rows = c.execute("""SELECT * FROM pipeline_job WHERE stage='prepare' AND
        ((status='failed' AND finished_at>='2026-11-21T02:01:10' AND finished_at<'2026-11-23T02:01:10')
        OR (id IN (96377,108613) AND generation=0)) ORDER BY id""").fetchall()
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec='milliseconds')
    plans, skipped = [], []
    for raw in rows:
        row = dict(raw)
        publish = c.execute("SELECT * FROM pipeline_job WHERE source_key=? AND stage='publish'", (row['source_key'],)).fetchone()
        if publish:
            skipped.append({'source_key': row['source_key'], 'publication_state': publish['status']})
            continue  # Never replace any existing publication or its receipts.
        clone = dict(row)
        clone.pop('id')
        clone.update(stage='publish', status='backlog_paused', hold_reason='historical_preparation_only',
            generation=0, node_id='', worker_id='', claim_hash='', lease_expires_at='', external_intent=0,
            created_at=now, updated_at=now, started_at='', finished_at='', error_code='', error_message='',
            canonical_receipt='', payload_json='{}', artifact_json='{}', resource_keys_json='[]',
            capabilities_json=json.dumps(['publish:' + row['channel']]), grandfathered=0)
        plans.append({'prepare_id': row['id'], 'source_key': row['source_key'], 'previous_status': row['status'], 'generation': row['generation']})
        if a.apply:
            cols = list(clone)
            inserted = c.execute('INSERT INTO pipeline_job(' + ','.join(cols) + ') VALUES(' + ','.join('?' for _ in cols) + ')', tuple(clone[k] for k in cols)).lastrowid
            c.execute('INSERT INTO pipeline_event(job_id,event_type,details_json,created_at) VALUES(?,?,?,?)',
                (inserted, 'historical_preparation_only_authorized', json.dumps({'prepare_job_id': row['id'],
                    'release_condition': 'explicit_user_authorization_for_historical_publication'}), now))
    evidence = {'apply': a.apply, 'at': now, 'tasks': plans, 'existing_publications_untouched': skipped}
    # Save the exact scope before commit. No original row is removed or reset.
    target = Path(a.evidence)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open('x') as f:
        json.dump(evidence, f, indent=2)
        f.flush()
        __import__('os').fsync(f.fileno())
    c.commit()
    print(json.dumps({'publication_holds': len(plans), 'existing_publications_untouched': len(skipped), 'apply': a.apply}))


if __name__ == '__main__':
    main()
