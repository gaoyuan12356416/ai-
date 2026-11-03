"""Resume only intake for one explicitly selected job; never generate images."""
import argparse
import contextlib
import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from features import cover_material_source as material


def environment():
    pid = subprocess.check_output(['systemctl', 'show', 'drama-material-api.service', '-p', 'MainPID', '--value'], text=True).strip()
    entries = Path('/proc/' + pid + '/environ').read_bytes().split(b'\0')
    return {k.decode(): v.decode() for k, v in (s.split(b'=', 1) for s in entries if b'=' in s)}


def read_job(db, job_id):
    with contextlib.closing(material.readonly(db)) as conn:
        row = conn.execute('SELECT * FROM drama_screenshot_job WHERE job_id=?', (job_id,)).fetchone()
    if not row:
        raise RuntimeError('job_missing')
    return dict(row)


def read_uploads(db, job_id):
    keys = {'screenshot:' + job_id + ':' + size for size, _ in material.SPECS}
    with contextlib.closing(material.readonly(db)) as conn:
        rows = conn.execute('SELECT * FROM uploads WHERE source_json LIKE ? ORDER BY key', ('%' + job_id + '%',)).fetchall()
    return [dict(r) for r in rows if json.loads(r['source_json']).get('source_key') in keys]


def save(state, name, value):
    with (state / name).open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())


def validate_confirmed(before, after, images):
    if {r['key'] for r in after} != {material.identity(i) for i in images}:
        raise RuntimeError('image_identity_or_receipt_set_changed')
    if any(r['state'] != 'confirmed' or not material.valid_api_receipt(r) for r in after):
        raise RuntimeError('not_all_three_intakes_confirmed')
    old = {r['key']: r for r in before}
    for row in after:
        previous = old.get(row['key'])
        if previous and previous['state'] == 'confirmed' and row != previous:
            raise RuntimeError('previously_confirmed_receipt_changed')
        limit = previous['attempts'] if previous and previous['attempts'] else 1
        if row['attempts'] > limit:
            raise RuntimeError('unexpected_repeat_submission')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('freeze', 'recover'))
    parser.add_argument('--job-id', required=True)
    parser.add_argument('--state', required=True)
    args = parser.parse_args()
    env = environment()
    db = Path(env.get('DRAMA_JOB_DB_PATH', material.SOURCE_DB))
    uploads_db = Path(env.get('AI_COVER_SOURCE_ROOT', material.ROOT)) / 'upload.sqlite3'
    state = Path(args.state).resolve()
    if not state.is_relative_to(Path('/mnt/data-disk/ai-cover-material-source')):
        raise RuntimeError('evidence_must_be_on_cover_data_disk')
    mounted = subprocess.check_output(['findmnt', '-n', '-o', 'UUID', '--mountpoint', '/mnt/data-disk'], text=True).strip()
    if mounted != '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8':
        raise RuntimeError('data_disk_unavailable')
    os.umask(0o077)
    state.mkdir(parents=True, exist_ok=True)
    with material.file_lock(state / 'recovery.lock'):
        current = read_job(db, args.job_id)
        if current['status'] != 'failed' or not all(current[size + '_url'] for size, _ in material.SPECS):
            raise RuntimeError('callback_only_requires_failed_job_and_three_existing_images')
        job = dict(current, assets=json.loads(current['assets_json']))
        if args.action == 'freeze':
            images = material.screenshot_sources(job, str(db))
            before = read_uploads(uploads_db, args.job_id)
            for name, path in [('source', db), ('uploads', uploads_db)]:
                dest = state / (name + '-before.sqlite3')
                if dest.exists():
                    raise RuntimeError('evidence_backup_exists')
                with contextlib.closing(material.readonly(path)) as src, sqlite3.connect(dest) as dst:
                    src.backup(dst)
                    if dst.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                        raise RuntimeError('backup_integrity_failed')
            save(state, 'before.json', dict(job=current, uploads=before, images=images, at=material.utcnow()))
            print(json.dumps(dict(frozen=args.job_id, existing_confirmed=sum(r['state']=='confirmed' for r in before), images_verified=3)))
            return
        frozen = json.loads((state / 'before.json').read_text())
        if current != frozen['job']:
            raise RuntimeError('job_changed_since_freeze')
        images = material.screenshot_sources(job, str(db))
        if [(i['source_key'], i['sha256']) for i in images] != [(i['source_key'], i['sha256']) for i in frozen['images']]:
            raise RuntimeError('existing_images_changed')
        start = time.monotonic()
        result = material.upload_screenshot(job, env)
        after = read_uploads(uploads_db, args.job_id)
        validate_confirmed(frozen['uploads'], after, images)
        # Preserve the exact original row unless all callbacks have verified receipts.
        with sqlite3.connect(db, timeout=15) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('BEGIN IMMEDIATE')
            latest = conn.execute('SELECT * FROM drama_screenshot_job WHERE job_id=?', (args.job_id,)).fetchone()
            if dict(latest) != current:
                raise RuntimeError('job_changed_after_intake_no_status_overwrite')
            now = dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
            conn.execute("UPDATE drama_screenshot_job SET status='done', progress=100, progress_detail=?, error_message='', "
                         'finished_at=?, updated_at=?, elapsed_seconds=? WHERE job_id=? AND status=\'failed\'',
                         ('三种截图素材已全部生成并入库；已复用原图补完入库', now, now,
                          int(current['elapsed_seconds'] or 0) + round(time.monotonic()-start), args.job_id))
        new_posts = sum(r['attempts'] for r in after) - sum(r['attempts'] for r in frozen['uploads'])
        evidence = dict(job=read_job(db, args.job_id), uploads=after, result=result,
                        at=material.utcnow(), new_posts=new_posts, images=images)
        save(state, 'recovered.json', evidence)
        print(json.dumps(dict(job_id=args.job_id, status=evidence['job']['status'],
                              confirmed_ids=result['confirmed_ids'], new_posts=new_posts, reused_images=3)))


if __name__ == '__main__':
    main()
