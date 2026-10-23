#!/usr/bin/env python3
"""Independent video-editing material-source outbox worker."""
import argparse
import logging
from pathlib import Path
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--once',action='store_true')
    parser.add_argument('--interval',type=float,default=5)
    args=parser.parse_args()
    import app
    from features.video_editing.service import get_service
    from features.video_editing.material_source import MaterialSourceWorker
    logging.basicConfig(level=logging.INFO)
    worker=MaterialSourceWorker(get_service(app).material_source)
    while True:
        try:worker.tick()
        except Exception:logging.warning('video editing material-source tick failed; durable state retained')
        if args.once:return
        time.sleep(max(1,min(60,args.interval)))


if __name__=='__main__':main()
