#!/usr/bin/env python3
"""Single-claim asynchronous YouTube publisher for drama synthesis.

Credentials are read on demand from the existing server-side account tables.
No token, client secret, resumable URI, or source file is logged.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app as drama_app  # noqa: E402
from features.drama_synthesis.unified_youtube import (  # noqa: E402
    UnifiedYouTubeWriter, run_sync_outbox_once,
)
from features.drama_synthesis.youtube import (  # noqa: E402
    YouTubeHTTPClient,
    YouTubePublishEngine,
)


STOP = False


def stop_handler(_signum, _frame):
    global STOP
    STOP = True


def env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(int(os.environ.get(name, str(default))), high))
    except (TypeError, ValueError):
        return default


def build_engine() -> YouTubePublishEngine:
    source_hosts = tuple(
        item.strip().lower()
        for item in os.environ.get("DRAMA_YOUTUBE_SOURCE_HOSTS", "").split(",")
        if item.strip()
    )
    return YouTubePublishEngine(
        drama_app.DRAMA_SYNTHESIS_STORE,
        drama_app.drama_youtube_repository(),
        YouTubeHTTPClient(timeout=env_int("DRAMA_YOUTUBE_HTTP_TIMEOUT", 120, 30, 600)),
        work_root=os.environ.get("DRAMA_YOUTUBE_WORK_ROOT", "/mnt/data-disk/drama-youtube-publish"),
        allowed_source_hosts=source_hosts,
    )


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)
    drama_app.DRAMA_SYNTHESIS_STORE.ensure_storage()
    engine = build_engine()
    worker_id = "%s:%s" % (socket.gethostname(), os.getpid())
    poll_seconds = env_int("DRAMA_YOUTUBE_POLL_SECONDS", 10, 1, 300)
    sync_enabled = os.environ.get("DRAMA_YOUTUBE_UNIFIED_SYNC_ENABLED", "0") == "1"
    sync_writer = UnifiedYouTubeWriter(None)
    while not STOP:
        if os.environ.get("YOUTUBE_LIVE_ENABLED", "0") != "1":
            time.sleep(poll_seconds)
            continue
        try:
            result = engine.run_once(worker_id)
            if result.get("claimed"):
                logging.info(
                    "YouTube task processed: task_id=%s status=%s",
                    result.get("task_id"),
                    result.get("status"),
                )
                time.sleep(poll_seconds)
                continue
            if sync_enabled:
                sync_result = run_sync_outbox_once(drama_app.DRAMA_SYNTHESIS_STORE, sync_writer, worker_id + ":sync")
                if sync_result.get("claimed"):
                    logging.info("YouTube unified sync processed: outbox_id=%s status=%s", sync_result.get("outbox_id"), sync_result.get("status"))
                    time.sleep(poll_seconds)
                    continue
        except Exception:
            logging.exception("YouTube worker loop failed before external task handling")
        time.sleep(poll_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
