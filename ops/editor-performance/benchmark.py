"""Cold-import comparison in new isolated databases; production is read-only."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
import statistics
import subprocess
import sys
import threading
import time
import urllib.request

REPO=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(REPO))


def p95(values):return round(sorted(values)[min(len(values)-1,int(len(values)*.95))],2) if values else None


def main():
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);args=p.parse_args()
    if args.root.exists():raise RuntimeError('Benchmark root must be new')
    args.root.mkdir(parents=True,mode=0o700)
    pid=subprocess.check_output(['systemctl','show','ai-video-editing.service','-p','MainPID','--value']).decode().strip()
    for pair in Path('/proc/'+pid+'/environ').read_bytes().split(b'\0'):
        if b'=' in pair:
            key,value=pair.split(b'=',1);os.environ[key.decode()]=value.decode()
    # Never inherit the production data root for fixtures.
    os.environ['AI_VIDEO_EDITING_ROOT']=str(args.root/'unused')
    from features.ai_video_editing.runtime import Runtime,public_asset,packed
    from features.ai_video_editing import catalog_imports,studio,selections
    from features.ai_video_editing.catalog import Catalog
    from features.ai_video_editing.http_api import Handler,platform
    with closing(sqlite3.connect('file:/mnt/data-disk/ai-video-editing/editor.sqlite3?mode=ro',uri=True)) as db:
        db.row_factory=sqlite3.Row
        original=db.execute('SELECT * FROM catalog_imports WHERE asset_id=?',('aa_b3419840640d4c56880675a5b7f4b5a4',)).fetchone()
        originals=db.execute("SELECT c.episode,a.sha256 FROM catalog_imports c JOIN assets a ON a.id=c.asset_id WHERE c.owner=? AND c.source_key=? AND c.episode BETWEEN 1 AND 5 ORDER BY c.episode",(original['owner'],original['source_key'])).fetchall()
        assert len(originals)==5
        expected={r['episode']:r['sha256'] for r in originals}
    results=[]
    for concurrency in (1,2,3):
        rt=Runtime(args.root/str(concurrency));studio.setup(rt)
        actor={'tenant_key':'isolated-editor-benchmark','user_id':'benchmark-'+str(concurrency)}
        catalog=Catalog(platform)
        refs=[{'kind':'catalog','product':original['product'],'key':original['source_key'],'episode':i} for i in range(1,6)]
        started=time.monotonic();receipt=selections.submit(rt,actor,None,{'items':refs,'operation_id':'cold-five'})
        confirm_ms=(time.monotonic()-started)*1000
        class SampleHandler(Handler):
            def log_message(self,*a):pass
            def _session(self):return actor
            def _require_cookie_module(self,*a):return True
            def _require_same_origin_json(self):return True
        times=[];errors=[];stop=threading.Event()
        with ThreadingHTTPServer(('127.0.0.1',0),SampleHandler) as http:
            http.runtime=rt;http.catalog=catalog;http.worker_token='isolated'
            server=threading.Thread(target=http.serve_forever,daemon=True);server.start()
            url='http://127.0.0.1:%s/api/ai-video-editing/assets?limit=24'%http.server_port
            def probe():
                while not stop.is_set():
                    began=time.monotonic()
                    try:
                        with urllib.request.urlopen(url,timeout=5) as response:response.read()
                        times.append((time.monotonic()-began)*1000)
                    except Exception as exc:errors.append(type(exc).__name__)
                    stop.wait(.25)
            monitor=threading.Thread(target=probe,daemon=True);monitor.start()
            def consume():
                while job:=catalog_imports.claim(rt):catalog_imports.process(rt,catalog,job)
            began=time.monotonic()
            try:
                with ThreadPoolExecutor(max_workers=concurrency) as pool:
                    list(pool.map(lambda _:consume(),range(concurrency)))
            finally:
                elapsed=time.monotonic()-began;stop.set();monitor.join(6);http.shutdown();server.join(2)
        with rt.db() as db:
            rows=[dict(r) for r in db.execute('SELECT episode,status,asset_id,bytes,timings,error FROM catalog_imports ORDER BY episode')]
        for row in rows:
            if row['status']=='ready':assert rt.asset(row['asset_id'],actor)['sha256']==expected[row['episode']],'Cold sample content drifted'
        result={'workers':concurrency,'elapsed_seconds':round(elapsed,3),'confirm_ms':round(confirm_ms,2),
                'http_p95_ms':p95(times),'http_samples':len(times),'http_errors':errors,'items':rows,'load':os.getloadavg()}
        results.append(result)
        (args.root/'results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
        print(json.dumps({k:v for k,v in result.items() if k!='items'}),flush=True)
    good=[r for r in results if not r['http_errors'] and r['http_p95_ms']<=500 and all(i['status']=='ready' for i in r['items'])]
    assert good,'No stable benchmark configuration'
    fastest=min(r['elapsed_seconds'] for r in good)
    chosen=min(r['workers'] for r in good if r['elapsed_seconds']<=fastest/.9)
    (args.root/'recommended.json').write_text(json.dumps({'workers':chosen,'rule':'lowest concurrency within 10 percent of fastest stable result'}))
    print(json.dumps({'recommended_workers':chosen}),flush=True)


if __name__=='__main__':main()
