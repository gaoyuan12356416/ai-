"""Isolated NVENC API/quality probe; writes only beneath the supplied directory."""
import argparse
import hashlib
import json
from pathlib import Path
import sys


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--package', required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--mode', default='api')
    args = p.parse_args()
    sys.path.insert(0, args.package)
    from features.random_gpu.gpu_compute import cupy_runtime
    cp = cupy_runtime()
    cp.zeros(1)
    import PyNvVideoCodec as nvc
    ctx = cp.cuda.driver.ctxGetCurrent()
    args.output.mkdir(parents=True, exist_ok=True)
    class View:
        def __init__(self, array):
            self.array = array
            self.__cuda_array_interface__ = dict(array.__cuda_array_interface__)
            self.__cuda_array_interface__['strides'] = tuple(array.strides)
    class Frame:
        def __init__(self, array): self.view = View(array)
        def cuda(self): return self.view
    print(json.dumps({'version':nvc.__version__, 'api':[x for x in dir(nvc) if 'Encode' in x or 'Packet' in x or 'Mux' in x]}),flush=True)
    params = dict(codec='h264', preset='P5', profile='high', rc='vbr', bitrate=0,
                  fps=30, gop=60, cudacontext=ctx, cudastream=0)
    if args.mode == 'api':
        enc = nvc.CreateEncoder(192,320,'ABGR',False,**params)
        print(json.dumps({'methods':[x for x in dir(enc) if not x.startswith('_')],
                         'encode_doc':enc.Encode.__doc__,
                         'reconfigure_fields':dir(enc.GetEncodeReconfigureParams()),
                         'reconfigure_doc':enc.Reconfigure.__doc__,
                         'pic_fields':dir(nvc.NV_ENC_PIC_PARAMS()) if hasattr(nvc,'NV_ENC_PIC_PARAMS') else []}),flush=True)
        return
    base = cp.arange(192*320*4,dtype=cp.uint32).reshape(320,192,4)
    for option in ('cq','targetquality','reconfigure'):
        for level in (21,40):
            kw = dict(params)
            kw[option] = level
            enc = nvc.CreateEncoder(192,320,'ABGR',False,**kw)
            if option == 'reconfigure':
                reconf = enc.GetEncodeReconfigureParams()
                reconf.targetQuality = level
                enc.Reconfigure(reconf)
            output = bytearray()
            timestamps = []
            for i in range(6):
                a = ((base*1103515245+12345+i*3019) >> 13).astype(cp.uint8)
                a[:,:,3] = 255
                cp.cuda.get_current_stream().synchronize()
                pic = nvc.NV_ENC_PIC_PARAMS()
                pic.inputTimeStamp = i
                pic.frameIdx = i
                pic.inputDuration = 1
                for packet in enc.Encode(Frame(a), pic):
                    output.extend(packet['data'])
                    timestamps.append(packet['timestamp'])
            for packet in enc.EndEncode():
                output.extend(packet['data'])
                timestamps.append(packet['timestamp'])
            target = args.output / ('encode-'+option+str(level)+'.h264')
            target.write_bytes(output)
            print(json.dumps({'option':option,'level':level,'size':len(output),
                              'sha256':hashlib.sha256(output).hexdigest(),
                              'target_quality':enc.GetEncodeReconfigureParams().targetQuality,
                              'timestamps':timestamps, 'bframes':enc.GetNumBFrames(),
                              'sequence_size':len(enc.GetSequenceParams())}),flush=True)
            del enc


if __name__ == '__main__': main()
