#!/usr/bin/env python3
"""Dedicated synthesis material outbox; render and publication workers are separate."""
import logging
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    import app
    from features.drama_synthesis.material_source import get_service
    from features.video_editing.material_source import MaterialSourceWorker
    logging.basicConfig(level=logging.INFO)
    worker = MaterialSourceWorker(get_service(app))
    while True:
        try:
            worker.tick()
        except Exception:
            logging.exception('synthesis material-source tick failed; durable state retained')
        time.sleep(5)


if __name__ == '__main__':
    main()
