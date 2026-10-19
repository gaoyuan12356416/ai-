"""Bound a render by actual output progress as well as total wall time."""
import subprocess
import tempfile
import time
from pathlib import Path


class RenderStalled(subprocess.TimeoutExpired):
    pass


def run_render(command, *, capture_output=True, text=True, timeout=9000,
               check=True, stall_seconds=180, poll_seconds=2):
    output = Path(command[-1])
    started = last_progress = time.monotonic()
    previous_size = -1
    # ffmpeg logs can be large; spool to disk instead of filling a pipe.
    with tempfile.TemporaryFile() as log:
        process = subprocess.Popen(command, stdout=log, stderr=log)
        try:
            while process.poll() is None:
                now = time.monotonic()
                size = output.stat().st_size if output.exists() else 0
                if size > previous_size:
                    previous_size, last_progress = size, now
                if now - started >= timeout:
                    raise subprocess.TimeoutExpired(command, timeout)
                if now - last_progress >= stall_seconds:
                    raise RenderStalled(command, stall_seconds)
                time.sleep(poll_seconds)
            if check and process.returncode:
                raise subprocess.CalledProcessError(process.returncode, command)
            return subprocess.CompletedProcess(command, process.returncode)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
