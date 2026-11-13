"""Bounded preparation lane and generator subprocess lifecycle."""
import logging
import os
import signal
import sqlite3
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path


class GeneratorCancelled(Exception):
    """The durable task no longer authorizes this generation attempt."""


def generation_cancel_check(db_path, task_id, version):
    def cancelled():
        # Never initialize or migrate the production database from this watcher.
        with closing(sqlite3.connect(Path(db_path).resolve().as_uri() + '?mode=ro',
                                     uri=True, timeout=1)) as connection:
            row = connection.execute(
                'SELECT state,version,lease_until FROM youtube_auto_preparation WHERE id=?',
                (task_id,)).fetchone()
        return (row is None or row[0] != 'generating' or row[1] != version
                or row[2] <= time.time())
    return cancelled


def _process_tree(root_pid):
    """Track PID start times, including children that create their own session."""
    rows = {}
    if os.name == 'posix' and Path('/proc').is_dir():
        for path in Path('/proc').iterdir():
            if not path.name.isdigit():
                continue
            try:
                fields = (path / 'stat').read_text().rsplit(')', 1)[1].split()
                rows[int(path.name)] = (int(fields[1]), fields[19])
            except (OSError, ValueError, IndexError):
                continue
    found = {root_pid}
    while True:
        children = {pid for pid, (parent, _) in rows.items() if parent in found}
        if children <= found:
            break
        found.update(children)
    return {pid: rows[pid][1] for pid in found if pid in rows}


def _stop_generator(process, descendants):
    if os.name == 'posix':
        descendants.update(_process_tree(process.pid))
        # Code-mode children may use setsid(), so killing the launcher's group
        # alone does not reclaim their capture pipes. Never target a reused PID.
        for pid, started in descendants.items():
            if pid == process.pid:
                continue
            try:
                fields = (Path('/proc') / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()
                if fields[19] == started:
                    os.kill(pid, signal.SIGKILL)
            except (OSError, ValueError, IndexError):
                pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    else:
        process.kill()


class PreparationLane:
    """Continuously refill bounded preparation slots independently of uploads.

    The workflow's SQLite transaction and lease remain the sole claim authority.
    Do not put unclaimed tasks into an executor queue or retry a failed task here.
    """

    def __init__(self, workers=2, *, enabled=lambda: True, idle_seconds=1):
        if str(workers) not in ('1', '2', '3', '4'):
            raise ValueError('YOUTUBE_AUTO_PREPARATION_WORKERS must be between 1 and 4')
        self.workers = int(workers)
        self.enabled = enabled
        self.idle_seconds = idle_seconds
        self.stopping = threading.Event()
        self.pool = ThreadPoolExecutor(max_workers=self.workers,
                                       thread_name_prefix='youtube-preparation')
        self.futures = []

    def start(self, workflow, worker_id):
        if self.futures or self.stopping.is_set():
            return
        self.futures = [self.pool.submit(self._run, workflow, '%s:prepare:%d' % (worker_id, slot))
                        for slot in range(self.workers)]

    def _run(self, workflow, worker_id):
        while not self.stopping.is_set():
            try:
                if self.enabled() and not self.stopping.is_set():
                    result = workflow.run_once(worker_id)
                    if result and result.get('claimed'):
                        logging.info('YouTube preparation task=%s processed', result.get('task_id', ''))
                        continue
            except Exception:
                # Keep other slots and publishing alive; no content, SQL or
                # credentials from an exception may enter worker logs.
                logging.error('YouTube preparation iteration failed; inspect persisted task status')
            self.stopping.wait(self.idle_seconds)

    def close(self):
        self.stopping.set()
        self.pool.shutdown(wait=True)


def run_generator(cmd, *, input, env, timeout, should_cancel=None):
    if should_cancel is not None and should_cancel():
        raise GeneratorCancelled()
    with subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, env=env,
                          start_new_session=(os.name == 'posix')) as process:
        deadline = time.monotonic() + timeout
        descendants = {}
        pending_input = input
        partial_output = ''
        partial_error = ''
        try:
            while True:
                descendants.update(_process_tree(process.pid))
                if should_cancel is not None and should_cancel():
                    raise GeneratorCancelled()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(cmd, timeout)
                try:
                    stdout, stderr = process.communicate(pending_input, timeout=min(.5, remaining))
                    break
                except subprocess.TimeoutExpired as waiting:
                    # communicate retains partially written input and captured
                    # output across timeout calls; do not send the prompt twice.
                    pending_input = None
                    partial_output, partial_error = waiting.output, waiting.stderr
        except BaseException as error:
            # The Node launcher owns a native child. Killing only the launcher
            # leaves image generation running and can keep capture pipes open.
            _stop_generator(process, descendants)
            try:
                stdout, stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                # A detached child must not extend the generation deadline.
                # Preserve captured JSON for the existing image-origin check.
                _stop_generator(process, descendants)
                stdout, stderr = partial_output or '', partial_error or ''
                if isinstance(stdout, bytes):
                    stdout = stdout.decode(errors='replace')
                if isinstance(stderr, bytes):
                    stderr = stderr.decode(errors='replace')
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()
            if isinstance(error, subprocess.TimeoutExpired):
                error.output, error.stderr = stdout, stderr
            raise
        return subprocess.CompletedProcess(cmd, process.returncode, stdout, stderr)
