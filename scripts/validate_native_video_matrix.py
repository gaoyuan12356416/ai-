"""Bounded synthetic timestamp/decoder matrix on an isolated CUDA runtime."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time


def run(args):
    return subprocess.check_output(args,stderr=subprocess.PIPE,timeout=60)


def hashes(path, fps=False):
    cmd=['ffmpeg','-nostdin','-v','error','-threads','2','-i',str(path),'-map','0:v:0','-an']
    if fps: cmd+=['-vf','fps=30:start_time=0']
    cmd+=['-threads','1','-fps_mode','passthrough','-f','framehash','-']
    return [line.split(',')[-1].strip() for line in run(cmd).decode().splitlines() if line and not line.startswith('#')]


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--package',required=True)
    p.add_argument('--output',type=Path,required=True)
    a=p.parse_args()
    a.output.mkdir(parents=True,exist_ok=True)
    sys.path.insert(0,a.package)
    from features.random_gpu.native_video import decode_frames,cfr_frames
    cases={
        'cfr20': ['-f','lavfi','-i','testsrc2=s=96x160:r=20:d=0.45'],
        'lowfps7': ['-f','lavfi','-i','testsrc2=s=96x160:r=7:d=0.45'],
        'vfr': ['-f','lavfi','-i','testsrc2=s=96x160:r=30:d=0.4','-vf',r'select=not(eq(mod(n\,4)\,1)),setpts=PTS','-fps_mode','vfr'],
        'delayed': ['-f','lavfi','-i','testsrc2=s=96x160:r=30:d=0.4','-f','lavfi','-i','sine=d=0.7',
                    '-vf','setpts=PTS+0.3/TB','-fps_mode','passthrough','-c:a','aac'],
        'audio_tail': ['-f','lavfi','-i','testsrc2=s=96x160:r=30:d=0.3','-f','lavfi','-i','sine=d=0.7','-c:a','aac'],
        'full_range': ['-f','lavfi','-i','testsrc2=s=96x160:r=30:d=0.3','-vf','scale=out_range=full','-color_range','pc'],
        'sar': ['-f','lavfi','-i','testsrc2=s=160x96:r=30:d=0.3','-vf','setsar=2/1'],
    }
    results=[]
    for name,inputs in cases.items():
        source=a.output/(name+'.mp4')
        run(['ffmpeg','-nostdin','-v','error','-y',*inputs,'-threads','2',
             '-c:v','libx264','-preset','ultrafast','-pix_fmt','yuv420p',str(source)])
        probe=json.loads(run(['ffprobe','-v','error','-select_streams','v:0','-show_streams','-show_format','-show_frames',
                             '-show_entries','frame=best_effort_timestamp','-of','json',str(source)]))
        video=probe['streams'][0]
        original=hashes(source)
        target=hashes(source,True)
        source_pts=[frame['best_effort_timestamp'] for frame in probe['frames']]
        mapping=dict(zip(original,source_pts))
        expected=[mapping[h] for h in target]
        started=time.monotonic()
        def check():
            if time.monotonic()-started>45: raise TimeoutError(name)
        result=dict(case=name,source_frames=len(original),expected_frames=len(expected))
        try:
            actual=[frame.timestamp for _,frame in cfr_frames(decode_frames(source,check),video['time_base'],
                    probe['format'].get('start_time','0'),probe['format']['duration'],check)]
            result.update(actual_frames=len(actual),matches=actual==expected,expected_pts=expected,actual_pts=actual)
        except Exception as exc:
            result.update(error=str(exc),matches=False)
        results.append(result)
        print(json.dumps(result),flush=True)
    (a.output/'timeline-matrix.json').write_text(json.dumps(results,indent=2))
    if not all(r['matches'] for r in results): raise SystemExit(1)


if __name__=='__main__': main()
