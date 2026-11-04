"""Known-color CPU decoder buffers, uploaded and converted entirely on CUDA."""
import argparse
from fractions import Fraction
import json
from pathlib import Path
import sys


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--package',required=True)
    p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();sys.path.insert(0,a.package)
    import av
    import numpy as np
    from features.random_gpu.native_video import NativeRenderer,codec_runtime
    from features.random_gpu.native_video_compat import UploadedFrame
    cp,_,_=codec_runtime();w,h=192,320
    recipe=dict(rotation_millidegrees=0,scale_bp=10000,tint_opacity_bp=100)
    layers={name:cp.zeros((16,16,4),cp.uint8) for name in ('border','opacity','corners','tint')}
    cases=['pal8','rgb4','monob','monow','gbrpf32le','rgba64be','bayer_bggr8','bayer_rggb16le']
    results=[]
    for fmt in cases:
        frame=av.VideoFrame(w,h,fmt);frame.pts=0;frame.time_base=Fraction(1,30)
        expected=(191,64,25)
        for pi,plane in enumerate(frame.planes):
            array=np.zeros(plane.buffer_size,dtype=np.uint8)
            if fmt=='pal8':
                if pi==0: array[:]=1
                else: array[4:8]=[25,64,191,255]
            elif fmt=='rgb4': array[:]=0xcc;expected=(255,170,0)
            elif fmt in ('monob','monow'):
                array[:]=255;expected=(255,255,255) if fmt=='monob' else (0,0,0)
            elif fmt=='gbrpf32le':
                array.view(np.float32)[:]=[64/255,25/255,191/255][pi]
            elif fmt=='rgba64be':
                words=array.reshape(h,plane.line_size).view('>u2')
                words[:,:w*4]=np.tile(np.asarray([191,64,25,255],dtype='>u2')*257,w)
            elif fmt.startswith('bayer'):
                pattern=fmt.split('_')[1][:4];size=2 if '16' in fmt else 1
                raw=array.reshape(h,plane.line_size)
                if size==2: raw=raw.view(np.uint16)
                values={'r':191,'g':64,'b':25}
                for yy in range(2):
                    for xx in range(2): raw[yy::2,xx:w:2]=values[pattern[yy*2+xx]]*(257 if size==2 else 1)
            plane.update(array)
        video=dict(codec_name='rawvideo',pix_fmt=fmt,width=w,height=h,time_base='1/30',
                   r_frame_rate='30/1',avg_frame_rate='30/1',color_range='pc')
        uploaded=UploadedFrame(cp,frame,video,Fraction(1,30),Fraction(1,30))
        renderer=NativeRenderer(recipe,video)
        output=renderer.render(uploaded,layers)
        center=output[630:650,350:370,:3].get().astype(float).mean((0,1)).tolist()
        result=dict(format=fmt,expected_rgb=expected,actual_center_rgb=center,
                    passed=max(abs(x-y) for x,y in zip(center,expected))<=1)
        results.append(result);print(json.dumps(result),flush=True)
        del renderer,uploaded
    a.output.write_text(json.dumps(results,indent=2))
    if not all(r['passed'] for r in results): raise SystemExit(1)


if __name__=='__main__': main()
