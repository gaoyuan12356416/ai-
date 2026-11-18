"""Run deferred cover jobs while the shared HTTP service finishes its native drain."""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time


def api_pid():
    return subprocess.check_output(['systemctl', 'show', 'drama-material-api', '-p', 'MainPID',
                                    '--value'], text=True).strip()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--old-pid', required=True)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--since', required=True)
    args = parser.parse_args()
    if api_pid() != args.old_pid:
        return
    if os.environ.get('COVER_DRAIN_WORKER_PID') != str(os.getpid()):
        values = Path('/proc', args.old_pid, 'environ').read_bytes().split(b'\0')
        env = {k.decode(): v.decode() for k, v in (item.split(b'=', 1) for item in values if b'=' in item)}
        env['COVER_DRAIN_WORKER_PID'] = str(os.getpid())
        os.execve(sys.executable, [sys.executable, *sys.argv], env)

    live = Path('/root/drama_material_service')
    os.chdir(live)
    sys.path.insert(0, str(live))
    import app
    from features import cover_material_source as material
    reference = Path(__file__).resolve().parents[2] / 'features/cover_material_source.py'
    if Path(material.__file__).read_bytes() != reference.read_bytes():
        raise RuntimeError('cover_release_mismatch')
    db = os.environ.get('DRAMA_JOB_DB_PATH', material.SOURCE_DB)
    with (args.state / 'native-start.lock').open('a') as lock:
        while api_pid() == args.old_pid:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                if api_pid() != args.old_pid:
                    return
                with contextlib.closing(material.readonly(db)) as c:
                    guard = c.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND "
                                      "name='cover_pause_old_start_20261118'").fetchone()
                    if not guard:
                        raise RuntimeError('old_cover_admission_not_held')
                    rows = c.execute("SELECT job_id FROM drama_screenshot_job WHERE status='queued' "
                                     "AND COALESCE(started_at,'')='' ORDER BY id").fetchall()
                if rows:
                    log = subprocess.run(['journalctl', '_PID='+args.old_pid, '--since', args.since,
                                          '-o', 'cat', '--no-pager', '--grep',
                                          'Exception in thread screenshot-job-'], capture_output=True, text=True)
                    if log.returncode not in (0, 1):
                        raise RuntimeError('old_thread_receipts_unavailable')
                    stopped = set(re.findall(r'Exception in thread screenshot-job-([0-9a-f]{8}):', log.stdout))
                    eligible = [r[0] for r in rows if r[0][:8] in stopped]
                    if eligible:
                        job_id = eligible[0]
                        job = app.fetch_screenshot_job_row(job_id)
                        if job['status'] != 'queued' or job.get('started_at'):
                            continue
                        # The old thread has exited at begin_screenshot_job_run.
                        # Use the native generator, retries, callbacks and final readback.
                        job['status'] = 'validating'
                        app.run_screenshot_job_async(job)
                        for thread in threading.enumerate():
                            if thread.name == 'screenshot-job-' + job_id[:8]:
                                thread.join()
                                break
                        result = app.fetch_screenshot_job_row(job_id)
                        print(json.dumps({'job_id': job_id, 'state': result['status'],
                                          'module_sha': hashlib.sha256(reference.read_bytes()).hexdigest()}),
                              flush=True)
                        if result['status'] not in ('done', 'failed'):
                            raise RuntimeError('native_cover_not_terminal')
                        continue
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
            time.sleep(5)


if __name__ == '__main__':
    main()
