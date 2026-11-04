"""Two isolated one-frame geometry regressions, never production inputs."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import threading


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--package',required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    sys.path.insert(0,args.package)
    from features.random_gpu.native_video import codec_runtime,encode_video
    fixtures=[('wide_hevc','5120x192','mp4',['-c:v','libx265','-x265-params',
        'pools=1:frame-threads=1:log-level=error']),
        ('odd_vp9','193x321','webm',['-c:v','libvpx-vp9','-deadline','realtime','-cpu-used','8'])]
    results=[]
    cp,_,_=codec_runtime()
    layers={name:cp.zeros((16,16,4),cp.uint8) for name in ('border','opacity','corners','tint')}
    recipe=dict(rotation_millidegrees=0,scale_bp=10000,tint_opacity_bp=100)
    for name,size,ext,options in fixtures:
        source=args.output/(name+'.'+ext);output=args.output/(name+'-output.h264')
        subprocess.run(['ffmpeg','-nostdin','-v','error','-y','-f','lavfi','-i',
            f'testsrc=s={size}:r=30','-frames:v','1','-threads','2','-pix_fmt','yuv420p',
            *options,str(source)],check=True,timeout=60)
        probe=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams',
            '-show_format','-of','json',str(source)]))
        video=next(s for s in probe['streams'] if s['codec_type']=='video')
        stop=threading.Event();gpu=[]
        def monitor():
            while not stop.is_set():
                result=subprocess.run(['nvidia-smi','--query-compute-apps=pid,used_gpu_memory',
                    '--format=csv,noheader,nounits'],capture_output=True,text=True)
                for row in result.stdout.splitlines():
                    fields=row.split(',')
                    if len(fields)==2 and fields[0].strip()==str(os.getpid()):
                        gpu.append(int(fields[1]))
                stop.wait(.1)
        thread=threading.Thread(target=monitor,daemon=True);thread.start()
        try:
            record=encode_video(source,output,dict(video=video,format=probe['format'],duration=1/30),
                                recipe,lambda index:layers,lambda:None)
        finally:
            stop.set();thread.join()
        subprocess.run(['ffmpeg','-nostdin','-v','error','-xerror','-i',str(output),
                        '-f','null','-'],check=True,timeout=30)
        count=json.loads(subprocess.check_output(['ffprobe','-v','error','-count_frames',
            '-show_entries','stream=nb_read_frames','-of','json',str(output)]))
        decoded=int(count['streams'][0]['nb_read_frames'])
        expected='nvdec' if name=='wide_hevc' else 'cpu_decode'
        result=dict(case=name,source_width=video['width'],source_height=video['height'],
            source_decoder=record['source_decoder'],frame_count=record['frame_count'],
            decoded_frames=decoded,peak_gpu_mib=max(gpu,default=0),
            passed=decoded==record['frame_count']==1 and record['source_decoder']['backend']==expected)
        results.append(result);print(json.dumps(result),flush=True)
    (args.output/'geometry.json').write_text(json.dumps(results,indent=2))
    if not all(result['passed'] for result in results): raise SystemExit(1)


if __name__=='__main__': main()
