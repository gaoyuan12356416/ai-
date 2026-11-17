"""A single cover-only worker using the existing authenticated dispatch protocol.

No publisher is instantiated here. Existing reservation, receipt and image
provenance checks remain in the pinned production runtime.
"""
import os
import sys
import threading
from pathlib import Path


CAPABILITIES = ['backend:cpu', 'prepare:youtube', 'youtube_prepare']


def assert_cover_only(executor):
    caps = executor.capabilities()
    if set(caps) != set(CAPABILITIES):
        raise RuntimeError('cover-only capability mismatch')


def main():
    runtime = Path(os.environ['YOUTUBE_COVER_LANE_RUNTIME']).resolve()
    sys.path.insert(0, str(runtime))
    from features.pipeline_dispatch.worker import configuration, process_worker
    from features.pipeline_dispatch.executors import NodeExecutors
    from features.pipeline_dispatch.client import CoordinatorClient
    config = configuration()
    if config['node_id'] != 'cpu-youtube-cover' or os.environ.get('PIPELINE_EXECUTORS') != 'youtube_prepare':
        raise RuntimeError('cover-only identity required')
    assert_cover_only(NodeExecutors(config))
    # Shared artifact storage must be configured before the first claim.
    # Without this preflight, configuration failures would consume tasks.
    from features.x_posts.media_repair import WorkerConfig, CosObjectStore
    CosObjectStore(WorkerConfig.from_env())
    from features.youtube_auto_publish.runtime import generate_cover_factory
    from features.youtube_auto_publish.images import decode_cover
    if not (Path(os.environ['CODEX_HOME']) / 'auth.json').is_file():
        raise RuntimeError('cover generator authentication unavailable')
    client = CoordinatorClient(config['coordinator_url'], config['node_token'])
    stopping = threading.Event()

    def register():
        client.post('nodes/register', {'node_id': config['node_id'],
            'capabilities': CAPABILITIES,
            'metrics': {'cpu_count': 1, 'gpu_count': 0, 'backend': 'cpu',
                        'purpose': 'youtube-cover-only'}})

    register()

    def refresh():
        while not stopping.wait(15):
            try:
                register()
            except Exception:
                # Existing job heartbeat owns the lease independently.
                pass

    thread = threading.Thread(target=refresh, daemon=True)
    thread.start()
    try:
        process_worker(config, 'prepare', 'cover-only')
    finally:
        stopping.set()


if __name__ == '__main__':
    main()
