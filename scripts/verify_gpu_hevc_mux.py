"""Tiny native HEVC AU -> CUDA hvcC/hvc1 MP4 -> strict decoded equivalence."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--directory',required=True,type=Path)
    args=parser.parse_args();args.directory.mkdir(parents=True,exist_ok=True)
    from features.random_gpu.native_video import codec_runtime,PackedFrame
    from features.random_gpu.gpu_mux import CudaMP4,native_video_track
    cp,nvc,ctx=codec_runtime()
    encoder=nvc.CreateEncoder(192,320,'ABGR',False,cudacontext=ctx,
        cudastream=cp.cuda.get_current_stream().ptr,codec='hevc',profile='main',
        preset='P6',rc='vbr',bitrate=1000000,fps=30,gop=60,global_header=1)
    sequence=bytes(encoder.GetSequenceParams())
    elemental=args.directory/'native.hevc';packets=[];offset=len(sequence)
    with elemental.open('wb') as handle:
        handle.write(sequence)
        def save(items):
            nonlocal offset
            for item in items:
                data=item['data'];handle.write(data)
                packets.append(dict(offset=offset,size=len(data),pts=int(item['timestamp']),duration=1))
                offset+=len(data)
        grid=cp.arange(192*320*4,dtype=cp.uint32).reshape(320,192,4)
        for i in range(12):
            pixels=((grid*13+i*27)%256).astype(cp.uint8);pixels[:,:,3]=255
            params=nvc.NV_ENC_PIC_PARAMS();params.inputTimeStamp=i;params.frameIdx=i;params.inputDuration=1
            save(encoder.Encode(PackedFrame(pixels),params))
        save(encoder.EndEncode())
    delay=max(i-p['pts'] for i,p in enumerate(packets))
    for i,packet in enumerate(packets):packet['dts']=i-delay
    record=dict(codec='hevc',sequence_header_hex=sequence.hex(),packets=packets)
    spool=args.directory/'native.spool';spool.unlink(missing_ok=True)
    track=native_video_track(elemental,record,spool,width=192,height=320)
    output=args.directory/'native.mp4'
    result=CudaMP4().mux([track],output,'0.4')
    def decoded(path):
        raw=subprocess.check_output(['/usr/bin/ffmpeg','-v','error','-xerror','-i',str(path),
              '-an','-pix_fmt','yuv420p','-f','rawvideo','-'],timeout=30)
        return dict(bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest())
    result.update(codec='hevc',frames=len(packets),hvcc=track.config.hex(),
                  before=decoded(elemental),after=decoded(output))
    result['passed']=result['before']==result['after'] and result['frames']==12
    (args.directory/'result.json').write_text(json.dumps(result,indent=2))
    print(json.dumps(result));return 0 if result['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
