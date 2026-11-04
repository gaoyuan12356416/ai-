"""Compare an immutable real fixture against an exact deployed-code baseline."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import resource
import threading
from types import SimpleNamespace


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--mode',choices=('baseline','native'),required=True)
    p.add_argument('--release',required=True)
    p.add_argument('--code',required=True)
    p.add_argument('--package',required=True)
    p.add_argument('--fixture',type=Path,required=True)
    p.add_argument('--source',type=Path)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--seconds',type=float,default=6)
    p.add_argument('--pixels',action='store_true')
    a=p.parse_args()
    if not 0<a.seconds<=30: raise ValueError('isolated_sample_must_be_at_most_30s')
    sys.path.insert(0,a.release)
    config=json.loads((a.fixture/'recipe.json').read_text())
    source=a.source or a.fixture/'source.mp4'
    from features.fb_gpu.random_overlay import load_asset_set,selected_asset_paths
    assets=selected_asset_paths(config['recipe'],load_asset_set(Path(config['asset_root']),config['recipe']['asset_set_sha256']))
    probe=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams','-show_format','-of','json',str(source)],timeout=60))
    video=next(s for s in probe['streams'] if s['codec_type']=='video')
    audio=next((s for s in probe['streams'] if s['codec_type']=='audio'),None)
    info=dict(video=video,audio=audio,has_audio=audio is not None,format=probe['format'],duration=a.seconds)
    os.environ['RANDOM_GPU_PIPELINE_V2']='1'
    os.environ['RANDOM_GPU_ASSET_CACHE_ROOT']='/data/random-overlay-gpu/asset-cache-v1'
    started=time.monotonic()
    cpu_started=time.process_time()
    usage_started=resource.getrusage(resource.RUSAGE_CHILDREN)
    if a.mode=='baseline':
        from features.fb_gpu.prepare_worker import build_command
        command=build_command(SimpleNamespace(ffmpeg='/usr/bin/ffmpeg',video_encoder='h264_nvenc',compositor_backend='opencl_fused_contain_v1'),source,a.output,info,config['recipe'],assets)
        if a.pixels:
            command=command[:command.index('-filter_complex')+2]+['-map','[v]',
                '-frames:v','1','-c:v','rawvideo','-pix_fmt','rgba','-threads','1','-f','rawvideo',str(a.output)]
        a.output.with_suffix('.command.json').write_text(json.dumps(command))
        proc=subprocess.Popen(command)
        stop=threading.Event()
        peaks={'gpu_mib':0,'samples':0}
        def monitor():
            while not stop.wait(1):
                try:
                    rows=subprocess.check_output(['nvidia-smi','--query-compute-apps=pid,used_memory','--format=csv,noheader,nounits'],timeout=5).decode().splitlines()
                    for row in rows:
                        pid,mib=[v.strip() for v in row.split(',')]
                        if int(pid)==proc.pid: peaks['gpu_mib']=max(peaks['gpu_mib'],int(mib))
                    peaks['samples']+=1
                except Exception: pass
        watcher=threading.Thread(target=monitor,daemon=True);watcher.start()
        try:
            result=proc.wait(timeout=300)
            if result: raise subprocess.CalledProcessError(result,command)
        finally:
            if proc.poll() is None: proc.terminate();proc.wait(timeout=10)
            stop.set();watcher.join(timeout=6)
        usage=resource.getrusage(resource.RUSAGE_CHILDREN)
        result={'mode':a.mode,'seconds':time.monotonic()-started,'size':a.output.stat().st_size,
                'cpu_seconds':time.process_time()-cpu_started+usage.ru_utime+usage.ru_stime-usage_started.ru_utime-usage_started.ru_stime,
                'child_peak_rss_kib':usage.ru_maxrss,'gpu_peak_mib':peaks['gpu_mib'],'gpu_samples':peaks['samples']}
        a.output.with_suffix('.performance.json').write_text(json.dumps(result,indent=2))
        print(json.dumps(result),flush=True)
    else:
        import features.random_gpu
        features.random_gpu.__path__ = [str(Path(a.code)/'features/random_gpu'), *features.random_gpu.__path__]
        sys.path.insert(0,a.package)
        from features.random_gpu.native_video import CachedAssets,encode_video,decode_frames
        def check():
            if time.monotonic()-started>180: raise TimeoutError('native_probe_deadline')
        layers=CachedAssets(assets,os.environ['RANDOM_GPU_ASSET_CACHE_ROOT'],check)
        if a.pixels:
            from features.random_gpu.native_video import NativeRenderer,cfr_frames
            from features.random_gpu.native_video_compat import decoder_choice,cpu_decode_frames
            renderer=NativeRenderer(config['recipe'],video)
            choice=decoder_choice(video)
            decoded=decode_frames(source,check) if choice['backend']=='nvdec' else cpu_decode_frames(source,video,check)
            frames=cfr_frames(decoded,video['time_base'],probe['format'].get('start_time','0'),a.seconds,check)
            index,frame=next(frames)
            pixels=renderer.render(frame,layers(index))
            a.output.write_bytes(pixels.get().tobytes())
            layers.close()
            print(json.dumps({'mode':a.mode,'pixels':True,'size':a.output.stat().st_size}),flush=True)
            return
        try:
            record=encode_video(source,a.output,info,config['recipe'],layers,check)
        finally:
            layers.close()
        encoded_seconds=time.monotonic()-started
        count=sum(1 for _ in decode_frames(a.output,check))
        result={k:v for k,v in record.items() if k!='packets'}
        result.update(seconds=encoded_seconds,decode_count=count,size=a.output.stat().st_size)
        a.output.with_suffix('.validation.json').write_text(json.dumps(result,indent=2))
        print(json.dumps(result),flush=True)


if __name__=='__main__': main()
