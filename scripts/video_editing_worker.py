#!/usr/bin/env python3
"""Run the independent editing dispatcher: python scripts/video_editing_worker.py [--once]."""
import argparse
import logging
from pathlib import Path
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--once',action='store_true');parser.add_argument('--interval',type=float,default=3)
    args=parser.parse_args()
    import app
    from features.video_editing.service import get_service
    from features.video_editing.worker import Dispatcher
    logging.basicConfig(level=logging.INFO)
    worker=Dispatcher(get_service(app))
    while True:
        try:worker.tick()
        except Exception:logging.warning('video editing dispatcher tick failed; retrying durable task state')
        if args.once:return
        time.sleep(max(1,args.interval))


if __name__=='__main__':main()
