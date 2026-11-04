"""Process-wide exclusion for the complete lifetime of one frozen FB job.

Keep the lock inode: unlinking it would let a waiter and a new caller hold
different locks for the same job. The lock covers download, render and upload.
"""
from contextlib import contextmanager
from pathlib import Path
import os
import time


@contextmanager
def exclusive_job(path, check=lambda: None, poll_seconds=.2):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt
            handle.seek(0, 2)
            if not handle.tell():
                handle.write(b"0"); handle.flush()
            def acquire():
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            def release():
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            def acquire():
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            def release():
                fcntl.flock(handle, fcntl.LOCK_UN)
        while True:
            check()
            try:
                acquire()
                break
            except (BlockingIOError, PermissionError):
                time.sleep(poll_seconds)
            except OSError as exc:
                if os.name != "nt" or exc.errno not in (13, 36):
                    raise
                time.sleep(poll_seconds)
        try:
            check()
            yield
        finally:
            release()
