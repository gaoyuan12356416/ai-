#!/usr/bin/env python3
"""Run optimized tag selection against an explicitly pinned existing runtime."""
import os
import json
from pathlib import Path
import sys
import threading

base = Path(os.environ['X_TAG_SELECTION_BASE']).resolve(strict=True)
sys.path.insert(0, str(base))

from features.x_auto_posts import service
from x_tag_selection_io import install

if __name__ == '__main__':
    pools, cache = install(service)
    def report():
        while not stop.wait(60):
            values = list(pools.values())
            print(json.dumps({'selection_io': {
                'connections_created': sum(p.created for p in values),
                'connections_reused': sum(p.reused for p in values),
                'connections_discarded': sum(p.discarded for p in values),
                'candidate_reads': cache.loads, 'candidate_reuses': cache.hits,
            }}), flush=True)
    stop = threading.Event()
    threading.Thread(target=report, daemon=True, name='selection-io-stats').start()
    service.serve()
