#!/usr/bin/env python3
"""Run the loopback-only X post GPU media-repair worker."""

from __future__ import annotations

import signal
import sys
import threading
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from features.x_posts.media_repair import (  # noqa: E402
    MediaRepairError,
    MediaRepairHTTPServer,
    MediaRepairProcessor,
    WorkerConfig,
)


def main():
    try:
        config = WorkerConfig.from_env()
        processor = MediaRepairProcessor(config)
        server = MediaRepairHTTPServer((config.host, config.port), processor, config.token)
    except MediaRepairError as exc:
        print("x-post-media-repair startup failed [%s]: %s" % (exc.code, exc), file=sys.stderr)
        return 1

    def stop(_signum, _frame):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
