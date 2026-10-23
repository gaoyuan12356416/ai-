#!/usr/bin/env python3
"""Tick personal pools through the sidecar; never receives X access tokens."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import sys
import threading
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.x_video_publish.transport import request as request_json
from features.x_video_publish.errors import PublishingError


def run(environ=None, stop=None, request=None):
    env = os.environ if environ is None else environ
    stop = threading.Event() if stop is None else stop
    request = request or request_json
    if env.get('X_VIDEO_PUBLISH_POOL_SCHEDULER_ENABLED', '0') != '1':
        print('X personal pool scheduler disabled', flush=True)
        return 0
    base = env.get('X_POST_DAILY_INTERNAL_URL', env.get('X_POST_AUTOMATION_INTERNAL_URL', 'http://127.0.0.1:8810')).rstrip('/')
    token = env.get('X_POST_DAILY_INTERNAL_TOKEN', '')
    worker = 'xpool-' + uuid.uuid4().hex
    started = False
    while not stop.is_set():
        try:
            action = 'tick' if started else 'start'
            result = request(base + '/internal/video-publish/pool/' + action, token, {'worker_id': worker})
            if result.get('status') == 'not_started':
                started = False  # Sidecar restarted: new cutoff; never catch up.
            elif action == 'start':
                started = result.get('status') == 'started'
            elif result.get('slots_recorded'):
                print(json.dumps({'status': result['status'], 'slots_recorded': result['slots_recorded'],
                                  'slots_created': result.get('slots_created', 0)}), flush=True)
        except PublishingError as exc:
            print(json.dumps({'status': 'unavailable', 'code': exc.code}), flush=True)
        if stop.wait(10):
            break
    return 0


if __name__ == '__main__':
    stopping = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    signal.signal(signal.SIGINT, lambda *_: stopping.set())
    raise SystemExit(run(stop=stopping))
