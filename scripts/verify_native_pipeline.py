"""Read-only source fixture -> isolated native output, with no publish client."""
import argparse
import json
import os
from pathlib import Path
import resource
import subprocess
import sys
import time
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--fixture',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--seconds',type=float,default=6)
    args=parser.parse_args()
    if not 0<args.seconds<=60:
        raise ValueError('bounded_acceptance_sample_required')
    from features.fb_gpu.random_overlay import load_asset_set,selected_asset_paths
    from features.fb_gpu.render_process import run_render
    from features.random_gpu.native_pipeline import command
    frozen=json.loads((args.fixture/'recipe.json').read_text())
    source=args.fixture/'source.mp4'
    probe=lambda path:json.loads(subprocess.check_output(
        ['/usr/bin/ffprobe','-v','error','-show_streams','-show_format','-of','json',str(path)],timeout=60))
    raw=probe(source)
    info=dict(duration=args.seconds,video=next(s for s in raw['streams'] if s['codec_type']=='video'),
              has_audio=any(s['codec_type']=='audio' for s in raw['streams']),format=raw['format'])
    assets=selected_asset_paths(frozen['recipe'],load_asset_set(Path(frozen['asset_root']),frozen['recipe']['asset_set_sha256']))
    before=resource.getrusage(resource.RUSAGE_CHILDREN)
    started=time.monotonic()
    cmd=command(source,args.output,info,frozen['recipe'],assets)
    stop=threading.Event()
    gpu_memory=[]
    def monitor():
        while not stop.is_set():
            try:
                rows=subprocess.check_output(['nvidia-smi','--query-compute-apps=pid,used_memory',
                     '--format=csv,noheader,nounits'],timeout=3,text=True)
                for row in rows.splitlines():
                    pid,memory=[item.strip() for item in row.split(',')]
                    argv=(Path('/proc')/pid/'cmdline').read_bytes().split(b'\0')
                    if b'features.random_gpu.native_pipeline' in argv and str(args.output).encode() in argv:
                        gpu_memory.append(int(memory))
            except (OSError,ValueError,subprocess.SubprocessError):
                pass
            stop.wait(1)
    thread=threading.Thread(target=monitor,daemon=True)
    thread.start()
    try:
        run_render(cmd,timeout=600,stall_seconds=120,poll_seconds=.2)
    except subprocess.CalledProcessError as exc:
        print(exc.stderr, file=sys.stderr, flush=True)
        raise RuntimeError('native_pipeline_child_failed') from None
    finally:
        stop.set();thread.join(timeout=4)
    after=resource.getrusage(resource.RUSAGE_CHILDREN)
    result=json.loads(args.output.with_suffix(args.output.suffix+'.native.json').read_text())
    result.update(wall_seconds=time.monotonic()-started,
                  cpu_seconds=(after.ru_utime+after.ru_stime)-(before.ru_utime+before.ru_stime),
                  child_peak_rss_kib=after.ru_maxrss,
                  sampled_gpu_peak_mib=max(gpu_memory) if gpu_memory else None)
    out=probe(args.output)
    result['probe']={
        'duration':out['format']['duration'],
        'streams':[{key:stream.get(key) for key in
                    ('codec_type','codec_name','profile','width','height','pix_fmt','sample_rate','channels','duration','nb_frames','start_time')}
                   for stream in out['streams']]}
    subprocess.run(['/usr/bin/ffmpeg','-v','error','-xerror','-i',str(args.output),'-f','null','-'],check=True,timeout=180)
    result['full_decode_passed']=True
    args.output.with_suffix('.acceptance.json').write_text(json.dumps(result,indent=2))
    print(json.dumps(result,sort_keys=True),flush=True)


if __name__=='__main__':main()
