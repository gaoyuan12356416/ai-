"""Read-only provider timing and durable browsing cache warmup; no media imports."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--cache',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    args=p.parse_args()
    pid=subprocess.check_output(['systemctl','show','ai-video-editing.service','-p','MainPID','--value']).decode().strip()
    for pair in Path('/proc/'+pid+'/environ').read_bytes().split(b'\0'):
        if b'=' in pair:
            k,v=pair.split(b'=',1);os.environ[k.decode()]=v.decode()
    from features.ai_video_editing.http_api import platform
    from features.ai_video_editing.catalog import Catalog
    catalog=Catalog(platform,browse_cache_path=args.cache)
    results=[]
    with closing(sqlite3.connect('file:/mnt/data-disk/ai-video-editing/editor.sqlite3?mode=ro',uri=True)) as db:
        keys=dict(db.execute("SELECT product,source_key FROM catalog_imports WHERE status='ready' GROUP BY product,source_key ORDER BY max(updated)"))
    calls=[]
    for product in ['dramawave','moboreels']:
        calls.append((product,'dramas',lambda refresh,p=product:catalog.search(p,refresh=refresh)))
        if product in keys:
            calls.append((product,'episodes',lambda refresh,p=product:catalog.episodes(p,keys[p],refresh=refresh)))
    for product,kind,call in calls:
        row={'product':product,'kind':kind}
        try:
            before=time.monotonic();data=call(True);row.update(refresh_ms=round((time.monotonic()-before)*1000,2),items=len(data['items']))
            samples=[]
            for _ in range(10):
                before=time.monotonic();call(False);samples.append(round((time.monotonic()-before)*1000,3))
            row['cached_ms']=samples
        except Exception as exc:
            row['error']=getattr(exc,'message',type(exc).__name__)
        results.append(row)
        args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(results,indent=2))
        print(json.dumps(row),flush=True)
    catalog.browse.close()
    restarted=Catalog(platform,browse_cache_path=args.cache)
    for product in ['dramawave','moboreels']:
        before=time.monotonic();data=restarted.search(product)
        results.append({'product':product,'kind':'restart_drama_cache','ms':round((time.monotonic()-before)*1000,3),'state':data['cache']['state']})
    restarted.browse.close();args.output.write_text(json.dumps(results,indent=2))


if __name__=='__main__':main()
