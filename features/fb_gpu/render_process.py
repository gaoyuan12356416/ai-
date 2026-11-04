"""Bound a render by actual output progress as well as total wall time."""
import subprocess
import os
import signal
from contextlib import ExitStack
import tempfile
import time
from pathlib import Path


class RenderStalled(subprocess.TimeoutExpired):
    pass


def run_render(command, *, capture_output=True, text=True, timeout=9000,
               check=True, stall_seconds=180, poll_seconds=2):
    output = Path(command[-1])
    native = 'features.random_gpu.native_pipeline' in command
    progress = Path(str(output)+'.progress') if native else None
    if progress is not None:
        progress.unlink(missing_ok=True)
    started = last_progress = time.monotonic()
    previous_size = -1
    previous_progress = None
    # ffmpeg logs can be large; spool to disk instead of filling a pipe.
    with ExitStack() as resources:
        log = resources.enter_context(tempfile.TemporaryFile())
        if native:
            scratch = resources.enter_context(tempfile.TemporaryDirectory(prefix='.native-',dir=output.parent))
            command = [*command[:-1], '--work-dir', scratch, command[-1]]
        isolated_group = native and os.name == 'posix'
        process = subprocess.Popen(command, stdout=log, stderr=log,
                                   start_new_session=isolated_group)
        try:
            while process.poll() is None:
                now = time.monotonic()
                size = output.stat().st_size if output.exists() else 0
                if size > previous_size:
                    previous_size, last_progress = size, now
                if progress is not None:
                    try:
                        token = progress.read_bytes()
                    except FileNotFoundError:
                        token = None
                    if token and token != previous_progress:
                        previous_progress, last_progress = token, now
                if now - started >= timeout:
                    raise subprocess.TimeoutExpired(command, timeout)
                if now - last_progress >= stall_seconds:
                    raise RenderStalled(command, stall_seconds)
                time.sleep(poll_seconds)
            tail=None
            if process.returncode:
                log.seek(0,2)
                end=log.tell()
                log.seek(max(0,end-8192))
                tail=log.read().decode('utf-8',errors='replace')
                if check:
                    raise subprocess.CalledProcessError(process.returncode, command, stderr=tail)
            return subprocess.CompletedProcess(command, process.returncode, stderr=tail)
        finally:
            running = process.poll() is None
            if running or (isolated_group and process.returncode != 0):
                if isolated_group:
                    try:
                        os.killpg(process.pid,signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                elif running:
                    process.kill()
            process.wait()
            if progress is not None:
                progress.unlink(missing_ok=True)
