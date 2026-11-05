"""Compare uncompressed template output from the same frozen real source."""
import argparse
import json
from pathlib import Path
import sys

import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from features.random_gpu.native_video import NativeRenderer,CachedAssets,decode_frames
from features.random_gpu.native_resize import CachedWeightRenderer


def main():
    p=argparse.ArgumentParser();p.add_argument('--spec',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);p.add_argument('--frames',type=int,default=900)
    p.add_argument('--stride',type=int,default=30)
    a=p.parse_args();spec=json.loads(a.spec.read_text())
    old=NativeRenderer(spec['recipe'],spec['info']['video'])
    new=CachedWeightRenderer(spec['recipe'],spec['info']['video'])
    assets=CachedAssets(spec['assets'],spec['cache_root'],lambda:None);count=0
    try:
        for i,(_,frame) in enumerate(decode_frames(spec['source'],lambda:None)):
            if i>=a.frames:break
            if i%a.stride:continue
            layers=assets(i)
            before=old.render(frame,layers).get();after=new.render(frame,layers).get()
            if not np.array_equal(before,after):raise AssertionError(('real_pixel_mismatch',i))
            count+=1
    finally:assets.close()
    if not count:raise AssertionError('no_frames_checked')
    result=dict(real_frames_checked=count,different_channels=0,recipe_identical=True)
    a.output.write_text(json.dumps(result,indent=2));print(json.dumps(result))


if __name__=='__main__':main()
