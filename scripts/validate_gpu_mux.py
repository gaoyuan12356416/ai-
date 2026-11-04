"""Compare CUDA MP4 mux against an existing file without re-encoding packets."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from features.random_gpu.gpu_mux import CudaMP4,Sample,Track


def tracks_from_mp4(path):
    import av
    result=[]
    with av.open(str(path)) as container:
        streams=[s for s in container.streams if s.type in ('video','audio')]
        streams.sort(key=lambda s:s.type!='video')
        for stream in streams:
            # Reopen from the demuxer's initial position. seek(0) can skip a
            # negative AAC priming packet in a very short source.
            packets=[]
            with av.open(str(path)) as fresh:
                for p in fresh.demux(fresh.streams[stream.index]):
                    if not p.size:continue
                    if p.pts is None or p.dts is None:raise ValueError('packet_missing_timestamps')
                    unit=p.time_base.numerator
                    packets.append(Sample(bytes(p),p.pts*unit,p.dts*unit,p.duration*unit,p.is_keyframe))
            cc=stream.codec_context
            result.append(Track(stream.type,stream.time_base.denominator,packets,cc.extradata,
                width=cc.width if stream.type=='video' else 0,
                height=cc.height if stream.type=='video' else 0,
                channels=cc.channels if stream.type=='audio' else 2,
                sample_rate=cc.sample_rate if stream.type=='audio' else 48000,
                codec=cc.name if stream.type=='video' else 'h264'))
    return result


def summary(tracks):
    result=[]
    for t in tracks:
        result.append(dict(kind=t.kind,config=t.config.hex(),timescale=t.timescale,
            packets=[dict(sha256=hashlib.sha256(s.data).hexdigest(),pts=s.pts,dts=s.dts,
                          duration=s.duration,key=s.key) for s in t.samples]))
    return result


def decode_hash(path, media):
    args=['/usr/bin/ffmpeg','-v','error','-i',str(path)]
    args+=['-map','0:v:0','-an','-pix_fmt','yuv420p','-f','rawvideo','-'] if media=='video' else ['-map','0:a:0','-vn','-f','f32le','-']
    p=subprocess.Popen(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    digest=hashlib.sha256();size=0
    while True:
        data=p.stdout.read(1024*1024)
        if not data:break
        digest.update(data);size+=len(data)
    stderr=p.stderr.read();code=p.wait()
    if code:raise RuntimeError(stderr.decode(errors='replace'))
    return dict(sha256=digest.hexdigest(),bytes=size)


def main():
    p=argparse.ArgumentParser();p.add_argument('source');p.add_argument('target');p.add_argument('--duration',required=True);p.add_argument('--report',required=True);a=p.parse_args()
    before=tracks_from_mp4(a.source)
    start=time.monotonic();cpu=time.process_time()
    report=CudaMP4().mux(before,a.target,a.duration)
    report.update(wall_seconds=time.monotonic()-start,cpu_seconds=time.process_time()-cpu)
    after=tracks_from_mp4(a.target)
    report['packet_metadata_equal']=summary(before)==summary(after)
    report['decoded']={t.kind:{'before':decode_hash(a.source,t.kind),'after':decode_hash(a.target,t.kind)} for t in before}
    report['decode_equal']=all(x['before']==x['after'] for x in report['decoded'].values())
    report['status']='passed' if report['packet_metadata_equal'] and report['decode_equal'] else 'failed'
    Path(a.report).write_text(json.dumps(report,indent=2)+'\n')
    if not report['packet_metadata_equal']:
        Path(a.report+'.packet-diff.json').write_text(json.dumps({'before':summary(before),'after':summary(after)},indent=2))
    print(json.dumps(report));sys.exit(0 if report['status']=='passed' else 1)


if __name__=='__main__':main()
