"""One small real cold GET, followed by actual conditional-GET cache hits."""
from concurrent.futures import ThreadPoolExecutor
import argparse
import glob
import hashlib
import json
import os
from pathlib import Path
import statistics
import time

import requests

from features.fb_gpu.source_cache import SourceCache


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--jobs-root',type=Path,required=True)
    p.add_argument('--work-root',type=Path,required=True)
    p.add_argument('--max-body-mib',type=int,default=16)
    a=p.parse_args()
    a.work_root.mkdir(mode=0o700,parents=True,exist_ok=True)
    choices=sorted(a.jobs_root.glob('*/source.mp4'),key=lambda x:x.stat().st_size)
    selected=None
    for path in choices:
        if not 0<path.stat().st_size<=a.max_body_mib*1024**2:
            continue
        metadata=path.parent/'recipe.json'
        if metadata.is_file():
            selected=json.loads(metadata.read_text())
            break
    if selected is None:
        raise RuntimeError('no_small_existing_source_for_bounded_real_network_test')
    cache=SourceCache(a.work_root/'cache',reserve_bytes=0)
    url=selected['request']['source_url']
    def fetch(i):
        return cache.fetch(url,a.work_root/('job-%d.mp4'%i),
            session_factory=requests.Session,max_source_bytes=a.max_body_mib*1024**2)
    cold=fetch(0)
    warm=fetch(1)
    started=time.monotonic()
    with ThreadPoolExecutor(max_workers=8) as pool:
        parallel=list(pool.map(fetch,range(2,10)))
    result=dict(cold=cold,warm=warm,parallel=parallel,
        parallel_wall_seconds=time.monotonic()-started,
        body_bytes_total=sum(x['network_bytes'] for x in [cold,warm]+parallel),
        uncached_body_bytes_for_same_requests=cold['size']*10,
        origin_unchanged_vs_frozen=cold['sha256']==selected['source_sha256'],
        source_url_recorded=False,publication_writes=0)
    (a.work_root/'results.json').write_text(json.dumps(result,indent=2))
    print(json.dumps(result))


if __name__=='__main__':
    main()
