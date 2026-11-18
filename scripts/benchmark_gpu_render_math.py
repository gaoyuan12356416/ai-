"""Run same-quality renderer A/B batches only in observed idle GPU windows.

Never signals production or changes queue admission. Abort only owned test
children when a production media process starts; completed cases checkpoint.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.benchmark_fb_efficiency import (ProductionNeedsResources,
    atomic_json,check_permit,guarded_commands,source_spec,probe)


def idle_window(args):
    deadline=time.monotonic()+args.wait_timeout
    consecutive=0
    while time.monotonic()<deadline:
        if time.time()>=args.deadline_epoch:
            raise TimeoutError('exclusive_window_ended')
        # A locally observed idle window is the only permit this runner grants.
        atomic_json(args.permit,dict(allowed=True,expires_at=min(time.time()+1800,args.deadline_epoch),
            basis='idle_gpu_observation',production_signals=0))
        try:
            check_permit(args.permit,args.work_root)
            p=subprocess.run(['nvidia-smi','--query-gpu=utilization.gpu,utilization.encoder',
                '--format=csv,noheader,nounits'],capture_output=True,text=True,timeout=5,check=True)
            busy=any(float(x.strip())>1 for line in p.stdout.splitlines() for x in line.split(','))
            consecutive=0 if busy else consecutive+1
            if consecutive>=args.idle_samples:
                print(json.dumps(dict(event='idle_window_open')),flush=True)
                return
        except (ProductionNeedsResources,subprocess.SubprocessError,ValueError):
            consecutive=0
        atomic_json(args.permit,dict(allowed=False,expires_at=0,basis='waiting_for_idle'))
        time.sleep(2)
    raise TimeoutError('no_idle_gpu_window')


def commands_in_idle(commands, outputs, args, runtime):
    env={k:v for k,v in os.environ.items() if k in ['PATH','LD_LIBRARY_PATH','CUDA_VISIBLE_DEVICES',
         'CUDA_MODULE_LOADING','CUPY_CACHE_DIR','HOME','LANG','SSL_CERT_FILE']}
    env.update(PYTHONPATH=str(runtime),OPENBLAS_NUM_THREADS='1',OMP_NUM_THREADS='1')
    args.runtime=runtime
    while True:
        idle_window(args)
        try:
            return guarded_commands(commands,outputs,args,env)
        except ProductionNeedsResources as exc:
            print(json.dumps(dict(event='yielded_to_production',reason=str(exc))),flush=True)
        finally:
            atomic_json(args.permit,dict(allowed=False,expires_at=0,basis='case_finished_or_yielded'))


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--baseline-root',type=Path,required=True)
    p.add_argument('--work-root',type=Path,required=True)
    p.add_argument('--source-job',type=Path,action='append',required=True)
    p.add_argument('--asset-cache',required=True)
    p.add_argument('--phase',choices=['kernels','quality','throughput','all'],default='all')
    p.add_argument('--wait-timeout',type=float,default=1800)
    p.add_argument('--idle-samples',type=int,default=10)
    p.add_argument('--deadline-epoch',type=float,default=float('inf'))
    p.add_argument('--duration',type=float,default=120)
    p.add_argument('--concurrency',type=int,default=16)
    p.add_argument('--order',default='baseline,candidate,candidate,baseline')
    p.add_argument('--ffmpeg',default='/usr/bin/ffmpeg')
    p.add_argument('--ffprobe',default='/usr/bin/ffprobe')
    args=p.parse_args()
    args.work_root=args.work_root.resolve();args.work_root.mkdir(parents=True,exist_ok=True)
    args.permit=args.work_root/'permit.json';args.case_timeout=1200
    runtimes={'baseline':args.baseline_root.resolve(),'candidate':ROOT}
    if args.phase in ('kernels','all'):
        dest=args.work_root/'kernels';dest.mkdir(exist_ok=True)
        command=[sys.executable,str(ROOT/'scripts/verify_gpu_render_math.py'),
            '--baseline-root',str(args.baseline_root),'--output',str(dest)]
        commands_in_idle([command],[dest/'control'],args,ROOT)
    if args.phase=='kernels':return
    specs=[source_spec(job,args) for job in args.source_job]
    report_path=args.work_root/'batches.json'
    report=json.loads(report_path.read_text()) if report_path.exists() else dict(cases=[],production_signals=0,publication_writes=0)
    completed={r['name'] for r in report['cases']}
    cases=[]
    if args.phase in ('quality','all'):
        for codec in ('h264','hevc'):
            for i,spec in enumerate(specs):
                for variant in ('baseline','candidate'):
                    short=json.loads(json.dumps(spec))
                    short['info']['duration']=30
                    short['info']['format']['duration']='30'
                    if codec=='hevc':
                        short['encoder_options']=dict(codec='hevc',profile='main',preset='P5',rc='vbr',cq=21,bitrate=0,fps=30,gop=60,idrperiod=60)
                    cases.append((f'quality-{codec}-{i}-{variant}',variant,[short]))
    if args.phase in ('throughput','all'):
        for run,variant in enumerate(args.order.split(',')):
            cases.append((f'batch-{run}-{variant}',variant,[specs[i%len(specs)] for i in range(args.concurrency)]))
    for name,variant,batch in cases:
        if name in completed:continue
        dest=args.work_root/name;dest.mkdir(exist_ok=True)
        outputs=[dest/f'{i}.mp4' for i in range(len(batch))]
        commands=[[sys.executable,'-m','features.random_gpu.native_pipeline','--spec',
                   json.dumps(spec,separators=(',',':')),str(output)] for spec,output in zip(batch,outputs)]
        print(json.dumps(dict(event='case_waiting',case=name)),flush=True)
        result=commands_in_idle(commands,outputs,args,runtimes[variant])
        durations=[probe(args.ffprobe,output)['duration'] for output in outputs]
        row=dict(name=name,variant=variant,concurrency=len(batch),**result,
            video_seconds=sum(durations),output_bytes=sum(output.stat().st_size for output in outputs),
            video_seconds_per_wall_second=sum(durations)/result['wall_seconds'],
            outputs=[str(output) for output in outputs])
        report['cases'].append(row);atomic_json(report_path,report)
        print(json.dumps({k:v for k,v in row.items() if k not in ['gpu_samples','outputs']}),flush=True)
    if args.phase in ('quality','all'):
        quality=[]
        for codec in ('h264','hevc'):
            for i in range(len(specs)):
                hashes={}
                for variant in ('baseline','candidate'):
                    output=args.work_root/f'quality-{codec}-{i}-{variant}'/'0.mp4'
                    hashes[variant]={}
                    for stream in ('v:0','a:0'):
                        raw=subprocess.check_output([args.ffmpeg,'-v','error','-err_detect','explode',
                            '-threads','1','-i',str(output),'-map','0:'+stream,'-f','framemd5','-'],timeout=180)
                        hashes[variant][stream]=hashlib.sha256(raw).hexdigest()
                row=dict(codec=codec,source_index=i,hashes=hashes,
                    decoded_identical=hashes['baseline']==hashes['candidate'])
                quality.append(row);atomic_json(args.work_root/'decoded-quality.json',quality)
                if not row['decoded_identical']:raise AssertionError('decoded_quality_mismatch')
        print(json.dumps(dict(event='decoded_quality_complete',cases=len(quality),identical=True)),flush=True)


if __name__=='__main__':
    main()
