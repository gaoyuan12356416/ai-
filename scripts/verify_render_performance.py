"""Isolated deterministic pixel/container parity and same-fixture A/B timing."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def kernels(out):
    import numpy as np
    from features.random_gpu.native_video import NativeRenderer, codec_runtime
    from features.random_gpu.native_resize import CachedWeightRenderer
    from features.random_gpu.cpu_mux import CpuMP4
    from features.random_gpu.gpu_mux import CudaMP4, Sample, Track
    cp,_,_=codec_runtime()
    rng=np.random.default_rng(20261105)
    recipe=dict(rotation_millidegrees=992,scale_bp=9924,tint_opacity_bp=959,
                source_overlay=dict(version=1,opacity_bp=397,scale_bp=14120))
    layers={n:cp.asarray(rng.integers(0,256,(32,32,4),dtype=np.uint8))
            for n in ('border','opacity','corners','tint')}
    class Frame:
        def __init__(self,planes):self.planes=planes
        def cuda(self):return self.planes
    results=[]
    for iw,ih in [(720,1280),(1080,1920),(1920,1080),(640,360),(3840,2160),(718,1282)]:
        for bits,legacy,chroma in [(8,0,'left'),(8,1,'center'),(10,0,'topleft')]:
            dtype=np.uint8 if bits==8 else np.uint16
            planes=[cp.asarray(rng.integers(0,1<<bits,shape,dtype=dtype))
                    for shape in ((ih,iw),(ih//2,iw))]
            if bits>8:planes=[p << (16-bits) for p in planes]
            frame=Frame(planes)
            video=dict(codec_name='h264' if bits==8 else 'hevc',pix_fmt='yuv420p' if bits==8 else 'yuv420p10le',
                       width=iw,height=ih,time_base='1/30',chroma_location=chroma,color_space='bt709',color_range='tv')
            old,new=NativeRenderer(recipe,video),CachedWeightRenderer(recipe,video)
            old.legacy_fit=new.legacy_fit=legacy
            before=old.render(frame,layers).get();after=new.render(frame,layers).get()
            diff=np.abs(before.astype(np.int16)-after.astype(np.int16))
            times={'old':[],'new':[]}
            for order in (('old','new'),('new','old'),('old','new')):
                for name in order:
                    renderer=old if name=='old' else new
                    start=cp.cuda.Event();end=cp.cuda.Event()
                    start.record()
                    for _ in range(3):renderer.render(frame,layers)
                    end.record();end.synchronize()
                    times[name].append(cp.cuda.get_elapsed_time(start,end)/3)
            row=dict(width=iw,height=ih,bits=bits,legacy=legacy,chroma=chroma,
                     different_channels=int(np.count_nonzero(diff)),max_error=int(diff.max()),
                     old_ms=float(np.median(times['old'])),new_ms=float(np.median(times['new'])))
            results.append(row);print(json.dumps(row),flush=True)
            (out/'kernels.json').write_text(json.dumps(results,indent=2))
            if row['different_channels']:raise AssertionError('pixel_mismatch')
            del old,new,planes,frame
    # Full container bytes, including negative PTS/DTS, B-frame offsets and
    # audio priming, must remain identical between array backends.
    video=Track('video',30,[Sample(b'V0',0,-2,1,True),Sample(b'V1',2,-1,1),Sample(b'V2',1,0,1)],
                bytes.fromhex('0142001fffe100016701000168'),width=720,height=1280,color=(2,2,5,False))
    audio=Track('audio',48000,[Sample(b'a',i,i,1024) for i in range(-1024,5120,1024)],b'\x11\x90')
    CudaMP4().mux([video,audio],out/'gpu-mux.mp4',.1)
    CpuMP4().mux([video,audio],out/'cpu-mux.mp4',.1)
    assert (out/'gpu-mux.mp4').read_bytes()==(out/'cpu-mux.mp4').read_bytes()
    print('container_bytes_identical',flush=True)


def pipeline(args,out):
    spec=json.loads(Path(args.spec).read_text())
    spec['info']['duration']=args.seconds
    spec['info']['format']['duration']=str(args.seconds)
    roots={'old':args.baseline_root,'new':str(Path(__file__).resolve().parents[1])}
    results=[]
    for run,name in enumerate(args.order.split(',')):
        folder=out/(str(run)+'-'+name);folder.mkdir()
        command=[sys.executable,'-m','features.random_gpu.native_pipeline','--spec',json.dumps(spec),str(folder/'output.mp4')]
        env=dict(os.environ,PYTHONPATH=roots[name],PYTHONUNBUFFERED='1')
        started=time.monotonic()
        with (folder/'process.log').open('w') as log:
            subprocess.run(command,cwd=roots[name],env=env,stdout=log,stderr=subprocess.STDOUT,check=True,timeout=600)
        report=json.loads((folder/'output.mp4.native.json').read_text())
        row=dict(variant=name,elapsed=time.monotonic()-started,report=report)
        results.append(row)
        (out/'pipeline.json').write_text(json.dumps(results,indent=2))
        print(json.dumps(dict(variant=name,elapsed=row['elapsed'],timings=report['timings'])),flush=True)
    # Decoder output hashes prove exact video and audio equality independently
    # of container metadata and encoder packetization.
    ffmpeg=spec['ffmpeg'];hashes=[]
    for run,row in enumerate(results):
        path=out/(str(run)+'-'+row['variant'])/'output.mp4'
        subprocess.run([ffmpeg,'-v','error','-err_detect','explode','-i',str(path),'-f','null','-'],capture_output=True,check=True,timeout=120)
        streams={}
        for stream in ('v:0','a:0'):
            data=subprocess.check_output([ffmpeg,'-v','error','-i',str(path),'-map','0:'+stream,'-f','framemd5','-'],timeout=120)
            streams[stream]=hashlib.sha256(data).hexdigest()
        hashes.append(streams)
    identical=all(h==hashes[0] for h in hashes)
    (out/'quality.json').write_text(json.dumps(dict(decoded_frames_identical=identical,hashes=hashes),indent=2))
    print(json.dumps(dict(decoded_frames_identical=identical)),flush=True)
    if not identical:raise AssertionError('decoded_media_mismatch')


def main():
    p=argparse.ArgumentParser();p.add_argument('--phase',choices=['kernels','pipeline'],required=True)
    p.add_argument('--output',required=True);p.add_argument('--spec');p.add_argument('--baseline-root')
    p.add_argument('--seconds',type=float,default=30);p.add_argument('--order',default='old,new,new,old')
    args=p.parse_args();out=Path(args.output);out.mkdir(parents=True,exist_ok=False)
    kernels(out) if args.phase=='kernels' else pipeline(args,out)


if __name__=='__main__':main()
