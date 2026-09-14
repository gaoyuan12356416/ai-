#!/usr/bin/env python3
"""Read-only ledger and HTTP/ffprobe audit. Never calls a publishing endpoint."""
import argparse
import concurrent.futures
import datetime
import json
import pathlib
import shutil
import sqlite3
import subprocess
import urllib.request


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    conn = sqlite3.connect('file:' + args.db + '?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute('''
        SELECT q.id queue_id,q.material_id,q.source_type,q.material_url,
        q.preflight_size,q.preflight_sha256,q.media_repair_job_key,q.status,
        q.schedule_run_id,l.status log_status,l.attempt_count,l.error_code,
        l.error_message,l.unknown_outcome,l.x_post_id,
        r.status batch_status,r.error_code batch_error
        FROM x_post_queue q LEFT JOIN x_post_publish_log l ON l.queue_id=q.id
        LEFT JOIN x_post_schedule_run r ON r.id=q.schedule_run_id
        WHERE q.status<>'published' ORDER BY q.id
    ''')]
    pool = [dict(r) for r in conn.execute("SELECT id,material_id,status,last_error_code,source_material_language FROM x_post_material_pool WHERE status='unpublished'")]
    ffprobe = shutil.which('ffprobe')

    def inspect(row):
        url = row.pop('material_url')
        if row['status'] == 'publishing' or row['batch_status'] == 'running':
            row['media_check'] = 'active_batch_skipped'
            return row
        try:
            with urllib.request.urlopen(urllib.request.Request(url, method='HEAD'), timeout=20) as response:
                size = int(response.headers.get('Content-Length', 0))
                row.update(http_status=response.status, size=size, content_type=response.headers.get('Content-Type'))
            result = subprocess.run([ffprobe, '-v', 'error', '-rw_timeout', '15000000', '-show_entries',
                'format=duration,size:stream=codec_type,codec_name,pix_fmt,width,height,r_frame_rate',
                '-of', 'json', url], capture_output=True, text=True, timeout=50)
            if result.returncode:
                row['media_check'] = 'probe_unavailable'
            else:
                probe = json.loads(result.stdout)
                row['probe'] = probe
                videos = [s for s in probe.get('streams', []) if s.get('codec_type') == 'video']
                row['media_check'] = 'passed' if videos and all(s.get('codec_name') == 'h264' and s.get('pix_fmt') == 'yuv420p' and s.get('width', 0) > 0 and s.get('height', 0) > 0 for s in videos) else 'invalid_media'
            row['stored_size_matches'] = None if not row['preflight_size'] else row['preflight_size'] == size
        except Exception as exc:
            row['media_check'] = 'unavailable'
            row['check_error_type'] = type(exc).__name__
        return row

    output = {'checked_at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'pool': pool, 'queues': []}
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        for row in executor.map(inspect, rows):
            output['queues'].append(row)
            print(json.dumps({k: row.get(k) for k in ['queue_id','material_id','media_check','stored_size_matches']}, ensure_ascii=False), flush=True)
    pathlib.Path(args.output).write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
