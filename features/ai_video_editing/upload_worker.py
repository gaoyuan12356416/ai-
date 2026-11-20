"""Verify uploaded COS objects in Hong Kong, independently of rendering."""
import json
import logging
import os
from pathlib import Path
import signal
import tempfile
import threading

from .cloud_media import download
from .render_worker import InternalAPI, LeaseLost
from .runtime import Fault, probe

LOG = logging.getLogger(__name__)


def verify(api, job, root):
    done, lost = threading.Event(), threading.Event()
    def heartbeat():
        while not done.wait(30):
            try:
                api.post('/uploads/' + job['id'] + '/heartbeat', job['lease'])
            except LeaseLost:
                lost.set()
                return
            except Exception:
                LOG.warning('upload heartbeat deferred id=%s', job['id'])
    pulse = threading.Thread(target=heartbeat, daemon=True)
    pulse.start()
    try:
        with tempfile.TemporaryDirectory(prefix='verify-', dir=root) as directory:
            source = Path(directory) / 'source.mp4'
            digest = download(job['location'], source, lost)
            metadata = probe(source)
            payload = json.dumps({'sha256': digest, 'metadata': metadata}).encode()
            with api.open('/uploads/' + job['id'] + '/verified', lease=job['lease'], data=payload,
                          headers={'Content-Type': 'application/json'}, timeout=30) as response:
                answer = json.loads(response.read(65536))
                if not answer.get('ok'):
                    raise ValueError('verification receipt missing')
            LOG.info('upload verified id=%s size=%s', job['id'], job['location']['size'])
    except Fault as exc:
        if exc.status in (409, 422) and not lost.is_set():
            api.post('/uploads/' + job['id'] + '/failed', job['lease'])
        raise
    finally:
        done.set()
        pulse.join(1)


def main():
    logging.basicConfig(level=logging.INFO)
    os.umask(0o077)
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    root = Path(os.getenv('AI_VIDEO_EDITING_UPLOAD_WORK', '/data/ai-video-editing/upload-work'))
    root.mkdir(parents=True, exist_ok=True)
    token = Path(os.getenv('AI_VIDEO_EDITING_WORKER_TOKEN_FILE', '/etc/ai-video-editing/worker.token')).read_text().strip()
    api = InternalAPI(os.getenv('AI_VIDEO_EDITING_INTERNAL_URL',
                      'https://ai.yingliangads.com/api/ai-video-editing/internal'), token)
    import fcntl
    with (root / 'worker.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        while not stopped.is_set():
            try:
                job = api.post('/uploads/claim').get('item')
                if job:
                    verify(api, job, root)
                    continue
            except Exception as exc:
                LOG.warning('upload verification deferred (%s)', type(exc).__name__)
            stopped.wait(3)


if __name__ == '__main__':
    main()
