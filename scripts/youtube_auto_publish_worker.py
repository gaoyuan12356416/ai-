#!/usr/bin/env python3
"""One preparation/notification or reviewed publish claim per iteration."""
import logging
import os
import signal
import socket
import sys
import time
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import app
from features.youtube_auto_publish.runtime import build_service,readonly_runner
from features.youtube_auto_publish.engine import ReviewedYouTubePublishEngine,ReviewedYouTubeHTTPClient
from features.youtube_auto_publish.failure_notifications import build_failure_notifications
from features.drama_synthesis.youtube import YouTubeCredentialRepository,YouTubeRemoteMediaExecutor

STOP=False
def stop(_signal,_frame):
    global STOP
    STOP=True

def notify_failures(failures):
    try:
        notice=failures.run_once()
        if notice.get('claimed'):logging.info('YouTube failure notification task=%s state=%s',notice.get('task_id',''),notice.get('state',''))
    except Exception:
        logging.error('YouTube failure notification scan unavailable')

def build_engine():
    storage=Path(os.environ.get('YOUTUBE_AUTO_STORAGE_ROOT','/mnt/data-disk/youtube-auto-publish')).resolve()
    executor_url=os.environ.get('DRAMA_YOUTUBE_MEDIA_EXECUTOR_URL','').strip()
    if not executor_url:raise RuntimeError('Existing YouTube media executor must be configured')
    executor=YouTubeRemoteMediaExecutor(executor_url,os.environ.get('GPU_VIDEO_WORKER_TOKEN',''),timeout=7200)
    return ReviewedYouTubePublishEngine(app.DRAMA_SYNTHESIS_STORE,
        YouTubeCredentialRepository(readonly_runner(app),schema=app.DB_NAME),ReviewedYouTubeHTTPClient(timeout=120),
        work_root=storage/'media',approved_cover_root=storage/'assets',
        allowed_source_hosts=tuple(x.strip().lower() for x in os.environ.get('DRAMA_YOUTUBE_SOURCE_HOSTS','').split(',') if x.strip()),
        media_executor=executor)

def main():
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    app.DRAMA_SYNTHESIS_STORE.ensure_storage()
    workflow=build_service(app);engine=build_engine();failures=build_failure_notifications(app)
    worker_id='youtube-auto:%s:%s' % (socket.gethostname(),os.getpid())
    while not STOP:
        if os.environ.get('YOUTUBE_AUTO_ENABLED','0')=='1' and os.environ.get('YOUTUBE_LIVE_ENABLED','0')=='1':
            try:
                # Give existing approved uploads a turn even when generation keeps arriving.
                published=engine.run_once(worker_id)
                notify_failures(failures)
                prepared=workflow.run_once(worker_id)
                for result in (published,prepared):
                    if result.get('claimed'):logging.info('YouTube auto task=%s processed',result.get('task_id',''))
            except Exception:
                # Never log SQL, tokens, uploaded content or model output.
                logging.error('YouTube auto worker iteration failed; inspect persisted task status')
            notify_failures(failures)
        for _ in range(5):
            if STOP:break
            time.sleep(1)
    return 0

if __name__=='__main__':raise SystemExit(main())
