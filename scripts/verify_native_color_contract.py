"""Prove the NVENC RGB conversion used by MP4 delivery, on a real GPU.

PyNvVideoCodec 2.2.3's legacy `colorspace` option sets full-range conversion
without signalling videoSignalTypePresentFlag. See official NGC source:
src/PyNvVideoCodec/src/NvEncoderClInterface.cpp, SetupVuiConfig.
This test deliberately omits that option and checks decoded sample values.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--package',required=True)
    p.add_argument('--output',type=Path,required=True)
    a=p.parse_args()
    sys.path.insert(0,a.package)
    from features.random_gpu.native_video import codec_runtime,PackedFrame
    cp,nvc,ctx=codec_runtime()
    import numpy as np
    width,height=192,128
    count=width*height
    values=[((255,0,0),(81,90,240)),((0,255,0),(145,54,34)),
            ((0,0,255),(41,240,110)),((0,0,0),(16,128,128)),
            ((255,255,255),(235,128,128))]
    results=[]
    for rgb,expected in values:
        image=cp.empty((height,width,4),cp.uint8)
        for index,value in enumerate((*rgb,255)): image[:,:,index]=value
        encoder=nvc.CreateEncoder(width,height,'ABGR',False,cudacontext=ctx,cudastream=0,
            codec='h264',profile='high',preset='P5',rc='vbr',cq=21,bitrate=0,fps=30,
            gop=60,idrperiod=60,global_header=1)
        cp.cuda.get_current_stream().synchronize()
        encoded=encoder.Encode(PackedFrame(image))+encoder.EndEncode()
        bitstream=bytes(encoder.GetSequenceParams())+b''.join(packet['data'] for packet in encoded)
        raw=subprocess.check_output(['ffmpeg','-v','error','-threads','1','-i','pipe:0',
            '-frames:v','1','-c:v','rawvideo','-pix_fmt','yuv420p','-threads','1','-f','rawvideo','-'],
            input=bitstream,timeout=30)
        pixels=np.frombuffer(raw,np.uint8)
        actual=[float(pixels[:count].mean()),float(pixels[count:count+count//4].mean()),
                float(pixels[count+count//4:].mean())]
        results.append(dict(rgb=rgb,expected_yuv=expected,actual_yuv=actual,
                            passed=all(abs(x-y)<=1 for x,y in zip(actual,expected))))
        del encoder
    record=dict(codec_version=nvc.__version__,range='limited',matrix='BT.601',
                mp4_nclx=[2,2,5,0],results=results)
    a.output.write_text(json.dumps(record,indent=2))
    print(json.dumps(record),flush=True)
    if not all(row['passed'] for row in results): raise SystemExit(1)


if __name__=='__main__': main()
