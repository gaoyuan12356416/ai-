"""Re-ingest existing hi/ms screenshot outputs with original idempotency keys."""
import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

sys.path.insert(0,'/root/drama_material_service')
from features.cover_material_source import upload_screenshot

ROOT=Path('/mnt/data-disk/production-audit-20261123')
DB='/root/drama_material_service/data/drama_material_jobs.sqlite3'
IDS=('87ad6f3ff9bc47898164e6973ad26768','36f943d148fa43f8b00923d4e214e7b8')


def main():
    pid=int(subprocess.check_output(['systemctl','show','drama-material-api.service','-p','MainPID','--value']))
    env=dict(x.decode().split('=',1) for x in Path('/proc/%d/environ'%pid).read_bytes().split(b'\0') if b'=' in x)
    c=sqlite3.connect(DB,timeout=20);c.row_factory=sqlite3.Row
    results=[]
    for identity in IDS:
        job=dict(c.execute('SELECT * FROM drama_screenshot_job WHERE job_id=?',(identity,)).fetchone())
        if job['status']=='done':
            results.append({'job_id':identity,'already_done':True});continue
        if job['status']!='failed' or job['progress']!=96 or 'language_unmapped:' not in job['error_message']:
            raise RuntimeError('screenshot_state_changed:'+identity)
        frozen=dict(job);job['assets']=json.loads(job['assets_json'] or '{}')
        evidence=ROOT/('screenshot-before-'+identity+'.json')
        if not evidence.exists():
            evidence.write_text(json.dumps(frozen,ensure_ascii=False));os.chmod(str(evidence),0o600)
        receipt=upload_screenshot(job,env=env)
        c.execute('BEGIN IMMEDIATE')
        current=dict(c.execute('SELECT * FROM drama_screenshot_job WHERE job_id=?',(identity,)).fetchone())
        if current!=frozen:
            c.rollback();raise RuntimeError('screenshot_changed_during_ingestion:'+identity)
        assets=json.loads(job['assets_json'] or '{}');assets['material_source_ingestion']=receipt
        now=dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        c.execute("UPDATE drama_screenshot_job SET status='done',progress=100,progress_detail='图片制作完成，素材已确认入库',error_message='',assets_json=?,updated_at=? WHERE job_id=?",
                  (json.dumps(assets,ensure_ascii=False),now,identity));c.commit()
        results.append({'job_id':identity,'receipt':receipt,'reused_existing_images':True})
        (ROOT/'screenshot-recovery-result.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
    print(json.dumps(results,ensure_ascii=False))


if __name__=='__main__':main()
