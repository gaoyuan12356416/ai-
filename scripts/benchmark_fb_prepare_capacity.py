"""Compare sequential and concurrent prepare-only work; never import publishing APIs."""
import concurrent.futures
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from features.fb_gpu.prepare_worker import WorkerConfig,PrepareProcessor


def main():
    pid=subprocess.check_output(['systemctl','show','fb-page-random-overlay-gpu.service','-p','MainPID','--value'],text=True).strip()
    env=dict(x.decode().split('=',1) for x in Path('/proc',pid,'environ').read_bytes().split(b'\0') if b'=' in x)
    os.environ.update(env)
    cfg=WorkerConfig.from_env(env)
    root=Path('/data/random-overlay-gpu/benchmarks/fb-capacity-20260918')
    root.mkdir(parents=True,exist_ok=True)
    selected=[]
    for path in sorted((cfg.work_root/'jobs').glob('*/manifest.json'),key=lambda p:p.stat().st_mtime,reverse=True):
        row=json.loads(path.read_text())
        if 120<=float(row['result']['probe']['duration'])<=180:
            selected.append(row)
        if len(selected)==2:break
    assert len(selected)==2
    report={}
    for mode,workers in [('sequential',1),('parallel',2)]:
        processor=PrepareProcessor(replace(cfg,work_root=root/mode,max_jobs=workers))
        def one(row):
            request=dict(row['request'],source_trim_tail_seconds=0,video_template='random_overlay',expected_profile=row['result']['profile'])
            start=time.monotonic();result=processor.prepare(request)
            return {'job_id':result['job_id'],'wall_seconds':round(time.monotonic()-start,3),'duration':result['probe']['duration'],'sha256':result['output_sha256'],'reference_sha256':row['result']['output_sha256'],'size':result['output_size'],'matches_reference':result['output_sha256']==row['result']['output_sha256']}
        start=time.monotonic()
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:items=list(pool.map(one,selected))
        report[mode]={'wall_seconds':round(time.monotonic()-start,3),'items':items}
        (root/'result.json').write_text(json.dumps(report,indent=2))
        print(json.dumps({mode:report[mode]}),flush=True)
    report['speedup']=round(report['sequential']['wall_seconds']/report['parallel']['wall_seconds'],3)
    report['all_hashes_match']=all(item['matches_reference'] for value in ('sequential','parallel') for item in report[value]['items'])
    (root/'result.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(report),flush=True)
    if not report['all_hashes_match']:raise SystemExit('Fixed-input output fingerprint changed')


if __name__=='__main__':main()
