import json
import sys
sys.path.insert(0,sys.argv[1])
from features.random_gpu.native_video import codec_runtime
cp,nvc,ctx=codec_runtime()
dmx=nvc.CreateDemuxer(filename=sys.argv[2])
print(json.dumps({'methods':[x for x in dir(dmx) if not x.startswith('_')],
                  'width':dmx.Width(),'height':dmx.Height()}),flush=True)
for p in dmx:
    print(json.dumps({'fields':[x for x in dir(p) if not x.startswith('_')],
                     'pts':p.pts,'dts':p.dts,'bsl':p.bsl,
                     'data_type':str(type(p.bsl_data)),
                     'data_shape':str(getattr(p.bsl_data,'shape',None)),
                     'head':str(p.bsl_data)[:100]}),flush=True)
    break
