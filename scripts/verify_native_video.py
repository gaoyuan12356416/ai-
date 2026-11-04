"""Short isolated native GPU smoke test; no production state or inputs."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--package', required=True)
    p.add_argument('--source', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    sys.path.insert(0, args.package)
    from features.random_gpu.native_video import codec_runtime, encode_video, decode_frames
    cp, _, _ = codec_runtime()
    if args.output.name == 'inspect.h264':
        for pts, frame in decode_frames(args.source,lambda:None):
            print(json.dumps({'pts':pts,'ctx':cp.cuda.driver.ctxGetCurrent(),
                'planes':[p.__cuda_array_interface__ for p in frame.cuda()]}),flush=True)
            return
    info = json.loads(subprocess.check_output(['ffprobe','-v','error', '-show_streams',
        '-show_format','-of','json',str(args.source)]))
    video = next(s for s in info['streams'] if s['codec_type']=='video')
    duration = min(6, float(info['format']['duration']))
    layers = {key:cp.zeros((16,16,4), dtype=cp.uint8)
              for key in ('border','opacity','corners','tint')}
    layers['tint'][:,:,0] = 180
    layers['tint'][:,:,3] = 255
    recipe = dict(rotation_millidegrees=1000,scale_bp=9900,tint_opacity_bp=300,
                  source_overlay=dict(version=1,opacity_bp=300,scale_bp=12000))
    deadline=time.monotonic()+120
    def check():
        if time.monotonic()>deadline: raise TimeoutError('native_gpu_probe_timeout')
    start = time.monotonic()
    record = encode_video(args.source,args.output,
        dict(video=video,format=info['format'],duration=duration), recipe,
        lambda index:layers, check)
    count = sum(1 for _ in decode_frames(args.output,check))
    result={key:value for key,value in record.items() if key != 'packets'}
    result.update(seconds=time.monotonic()-start, decode_count=count,
                  output_size=args.output.stat().st_size, pts=[p['pts'] for p in record['packets']])
    args.output.with_suffix('.validation.json').write_text(json.dumps(result,indent=2))
    print(json.dumps(result),flush=True)


if __name__=='__main__': main()
