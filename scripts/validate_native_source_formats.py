"""Short synthetic decode-format matrix; never reads or mutates tasks."""
import argparse
import json
from pathlib import Path
import subprocess
import sys


def run(command):
    return subprocess.run(command,check=True,capture_output=True,text=True,timeout=90)


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--package',required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--generate-only',action='store_true')
    a=p.parse_args();a.output.mkdir(parents=True,exist_ok=True)
    cases={
      'h264422': ('mp4',['-c:v','libx264','-pix_fmt','yuv422p']),
      'av1': ('mp4',['-c:v','libaom-av1','-cpu-used','8','-pix_fmt','yuv420p']),
      'hevc12': ('mp4',['-c:v','libx265','-x265-params','pools=1:frame-threads=1:log-level=error','-pix_fmt','yuv420p12le']),
      'hevc44410': ('mp4',['-c:v','libx265','-x265-params','pools=1:frame-threads=1:log-level=error','-pix_fmt','yuv444p10le']),
      'rgb_ffv1': ('mkv',['-c:v','ffv1','-pix_fmt','bgr0']),
      'interlace': ('mp4',['-vf','tinterlace=interleave_top','-c:v','libx264','-x264-params','tff=1','-flags','+ilme+ildct','-pix_fmt','yuv420p']),
    }
    fixtures=[]
    for name,(suffix,options) in cases.items():
        source=a.output/(name+'.'+suffix)
        rate=60 if name=='interlace' else 30
        run(['ffmpeg','-nostdin','-v','error','-y','-f','lavfi','-i',f'testsrc2=s=192x320:r={rate}:d=0.2',
             '-threads','2',*options,str(source)])
        fixtures.append(source)
    parts=[]
    for index,size in enumerate(('192x320','320x192')):
        path=a.output/f'dynamic-part{index}.h264'
        run(['ffmpeg','-nostdin','-v','error','-y','-f','lavfi','-i',f'testsrc2=s={size}:r=30:d=0.2',
             '-threads','2','-c:v','libx264','-bf','0','-pix_fmt','yuv420p','-f','h264',str(path)])
        parts.append(path)
    joined=a.output/'dynamic-joined.h264'
    joined.write_bytes(b''.join(p.read_bytes() for p in parts))
    dynamic=a.output/'dynamic.mp4'
    run(['ffmpeg','-nostdin','-v','error','-y','-fflags','+genpts','-r','30','-i',str(joined),'-c:v','copy',str(dynamic)])
    fixtures.append(dynamic)
    if a.generate_only:
        print(json.dumps([str(p) for p in fixtures]));return
    results=[]
    verify=Path(__file__).with_name('verify_native_video.py')
    for source in fixtures:
        output=a.output/(source.stem+'-output.h264')
        try:
            result=run([sys.executable,str(verify),'--package',a.package,'--source',str(source),'--output',str(output)])
            record=json.loads(output.with_suffix('.validation.json').read_text())
            record['case']=source.stem
            record['passed']=record['frame_count']==record['decode_count']
        except Exception as exc:
            record=dict(case=source.stem,passed=False,error=str(exc),
                        stderr=getattr(exc,'stderr','')[-3000:])
        results.append(record);print(json.dumps(record),flush=True)
    (a.output/'formats.json').write_text(json.dumps(results,indent=2))
    if not all(row['passed'] for row in results): raise SystemExit(1)


if __name__=='__main__': main()
