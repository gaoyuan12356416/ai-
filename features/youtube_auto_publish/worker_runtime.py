"""Bounded preparation lane and generator subprocess lifecycle."""
import os
import signal
import subprocess
from concurrent.futures import ThreadPoolExecutor


class PreparationLane:
    def __init__(self):
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='youtube-preparation')
        self.pending = None

    def tick(self, workflow, worker_id):
        result = None
        if self.pending is not None and self.pending.done():
            completed, self.pending = self.pending, None
            result = completed.result()
        if self.pending is None:
            self.pending = self.pool.submit(workflow.run_once, worker_id)
        return result

    def close(self):
        self.pool.shutdown(wait=True)


def run_generator(cmd, *, input, env, timeout):
    with subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, env=env,
                          start_new_session=(os.name == 'posix')) as process:
        try:
            stdout, stderr = process.communicate(input, timeout=timeout)
        except BaseException as error:
            # The Node launcher owns a native child. Killing only the launcher
            # leaves image generation running and can keep capture pipes open.
            if os.name == 'posix':
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                process.kill()
            stdout, stderr = process.communicate()
            if isinstance(error, subprocess.TimeoutExpired):
                error.output, error.stderr = stdout, stderr
            raise
        return subprocess.CompletedProcess(cmd, process.returncode, stdout, stderr)
