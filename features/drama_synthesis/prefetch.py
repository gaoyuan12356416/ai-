"""Bounded download-only lookahead using the renderer's exact checkpoints."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import shutil
import threading
import time

from .async_runtime import validate_render_payload
from .media_pipeline import download_episode_with_route


class PrefetchStopped(Exception):
    """Optional lookahead stopped; normal execution retains all checkpoints."""

    def __init__(self, code="drama_prefetch_stopped"):
        super().__init__(code)
        self.code = code


def prefetch_episodes(payload, root, *, stop_event, progress_callback=None,
                      downloader=download_episode_with_route, workers=None,
                      max_bytes=None, min_free_bytes=None, disk_usage=shutil.disk_usage):
    validate_render_payload(payload)
    workers = int(os.environ.get("DRAMA_GPU_PREFETCH_WORKERS", "2")) if workers is None else workers
    max_bytes = int(os.environ.get("DRAMA_GPU_PREFETCH_MAX_BYTES", str(16 * 1024**3))) if max_bytes is None else max_bytes
    min_free_bytes = int(os.environ.get("DRAMA_GPU_PREFETCH_MIN_FREE_BYTES", str(20 * 1024**3))) if min_free_bytes is None else min_free_bytes
    if (type(workers) is not int or workers not in (1, 2, 4)
            or type(max_bytes) is not int or max_bytes <= 0
            or type(min_free_bytes) is not int or min_free_bytes < 0):
        raise ValueError("invalid prefetch limits")
    job_dir = Path(root).absolute() / payload["job_id"]
    downloads = job_dir / "downloads"
    if job_dir.is_symlink() or downloads.is_symlink():
        raise PrefetchStopped("drama_prefetch_path_invalid")
    downloads.mkdir(mode=0o700, parents=True, exist_ok=True)
    rows = payload["episodes"]
    sizes, totals, complete = [0] * len(rows), [0] * len(rows), set()
    mutex = threading.Lock()
    last_report, last_bytes = [0.0], [0]
    local_stop = threading.Event()
    failures = []

    class Stop:
        def is_set(self):
            return stop_event.is_set() or local_stop.is_set()

        def wait(self, seconds):
            deadline = time.monotonic() + seconds
            while not self.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                local_stop.wait(min(remaining, 0.1))
            return True

    stop = Stop()

    def check_budget():
        remaining = sum(max(0, total - size) for size, total in zip(sizes, totals))
        if stop.is_set():
            raise PrefetchStopped()
        if sum(totals) > max_bytes or disk_usage(downloads).free < min_free_bytes + remaining:
            local_stop.set()
            raise PrefetchStopped("drama_prefetch_budget_exceeded")

    def report(force=False):
        now = time.monotonic()
        elapsed = now - last_report[0]
        if not force and elapsed < 1:
            return
        total = sum(sizes)
        speed = max(0, total - last_bytes[0]) / elapsed if last_report[0] and elapsed else 0
        last_report[0], last_bytes[0] = now, total
        if progress_callback:
            progress_callback(downloaded_bytes=total, total_bytes=sum(totals) if all(totals) else 0,
                              completed_episodes=len(complete), total_episodes=len(rows),
                              bytes_per_second=round(speed, 1), download_workers=workers)

    def download(index):
        with mutex:
            check_budget()

        def on_bytes(done, total):
            with mutex:
                sizes[index], totals[index] = done, total
                check_budget()
                report()

        row = rows[index]
        try:
            value = downloader(row["episode_url"], str(downloads / ("%03d.mp4" % row["episode_number"])),
                               row.get("download_route"), on_bytes, stop_event=stop,
                               connect_timeout=10, read_timeout=30, max_attempts=3)
            with mutex:
                complete.add(index)
                report(force=True)
            return value
        except Exception as exc:
            with mutex:
                if not failures:
                    failures.append(exc)
                local_stop.set()
            raise

    try:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="drama-prefetch-download") as pool:
            list(pool.map(download, range(len(rows))))
    except Exception:
        if failures:
            raise failures[0] from None
        raise
    return {"completed_episodes": len(complete), "downloaded_bytes": sum(sizes)}
