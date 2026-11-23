"""Add only indexes for observed full scans under coordinator write locks.

Rollback: DROP INDEX for these three names only. Never restore business data.
"""
import json
from pathlib import Path
import sqlite3
import time

ROOT=Path('/mnt/data-disk/production-audit-20261123')


def main():
    c=sqlite3.connect('/mnt/data-disk/pipeline-dispatch/dispatch.sqlite3',timeout=30,isolation_level=None)
    names=['pipeline_operation','pipeline_receipt','pipeline_checkpoint']
    evidence=[]
    before={table:c.execute('PRAGMA index_list('+table+')').fetchall() for table in names}
    (ROOT/'dispatch-indexes-before.json').write_text(json.dumps(before,indent=2))
    for table in names:
        start=time.monotonic()
        c.execute('CREATE INDEX IF NOT EXISTS '+table+'_job_generation ON '+table+'(job_id,generation)')
        plan=c.execute('EXPLAIN QUERY PLAN SELECT 1 FROM '+table+' WHERE job_id=93868 LIMIT 1').fetchall()
        evidence.append({'table':table,'seconds':time.monotonic()-start,'plan':plan})
        (ROOT/'dispatch-indexes-result.json').write_text(json.dumps(evidence,indent=2))
    c.close();print(json.dumps(evidence))


if __name__=='__main__':main()
