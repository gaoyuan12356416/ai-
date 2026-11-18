"""Cache Lanczos geometry and fuse chroma work without changing arithmetic order."""
import math
from pathlib import Path

from .native_video import NativeRenderer


class CachedWeightRenderer(NativeRenderer):
    resize_implementation = 'cuda_lanczos_cached_uv_v2'

    def __init__(self, recipe, video):
        super().__init__(recipe, video)
        source = Path(__file__).with_name('native_video.cu').read_text()
        start = source.index('extern "C" __global__ void fit_nv12(')
        end = source.index('__device__ float4 pixel_rgba', start)
        fit = source[start:end]
        # Reuse the authoritative color/geometry code verbatim, replacing only
        # coefficient evaluation. Component-format compatibility stays intact.
        fit = fit.replace('void fit_nv12(', 'void fit_nv12_cached(', 1)
        signature = 'int bits, int legacy_fit) {'
        if fit.count(signature) != 1 or fit.count('sample_plane(') != 3:
            raise ValueError('cached_fit_source_contract_changed')
        fit = fit.replace(signature, 'int bits, int legacy_fit, const int* mx, const int* my, '
                          'const float* wx, const float* wy, const float* normalizers, int taps) {', 1)
        old_uv = ('float u = sample_plane(uvp, uvpitch, 2, iw / 2, ih / 2, cx, cy, crx, cry, bits);\n'
                  '    float v = sample_plane(uvp + (bits>8?2:1), uvpitch, 2, iw / 2, ih / 2, cx, cy, crx, cry, bits);')
        if fit.count(old_uv) != 1:
            raise ValueError('cached_uv_source_contract_changed')
        fit = fit.replace(old_uv, 'float2 chroma = sample_uv_precomputed(uvp, uvpitch, iw / 2, ih / 2, '
                          'cx, cy, crx, cry, bits, x, y, mx, my, wx, wy, normalizers, taps);\n'
                          '    float u = chroma.x, v = chroma.y;', 1)
        macro = ('#define sample_plane(p,pitch,step,w,h,lx,ly,rx,ry,bits) '
                 'sample_precomputed(p,pitch,step,w,h,lx,ly,rx,ry,bits,x,y,step==1?0:1,mx,my,wx,wy,normalizers,taps)\n')
        compiled = source[:start]+Path(__file__).with_suffix('.cu').read_text()+'\n'+macro+fit+'\n#undef sample_plane\n'
        self.weight_module = self.cp.RawModule(code=compiled, options=('--std=c++11',),
            name_expressions=('fit_weights', 'fit_normalizers', 'fit_nv12_cached'))
        self.build_weights = self.weight_module.get_function('fit_weights')
        self.build_normalizers = self.weight_module.get_function('fit_normalizers')
        self.cached_fit = self.weight_module.get_function('fit_nv12_cached')
        self.plans = {}
        self.shape = None
        self.fit = self._fit_cached

    def _fit_cached(self, grid, block, args):
        cp = self.cp
        iw, ih, cover = int(args[4]), int(args[5]), int(args[7])
        key = (iw, ih, cover, float(args[10]), float(args[11]), int(args[14]))
        shape = (iw, ih)
        if shape != self.shape:
            self.plans.clear()
            self.shape = shape
        if key not in self.plans:
            taps = math.ceil(6*max(iw/720, ih/1280, 1))+8
            mx, my = cp.empty((2,720,2), cp.int32), cp.empty((2,1280,2), cp.int32)
            wx, wy = cp.empty((2,720,taps), cp.float32), cp.empty((2,1280,taps), cp.float32)
            normalizers = cp.empty((2,1280,720), cp.float32)
            for axis, n, meta, weights in ((0,720,mx,wx),(1,1280,my,wy)):
                self.build_weights(((n+127)//128,2),(128,),
                    (cp.int32(iw),cp.int32(ih),cp.int32(cover),args[14],args[10],args[11],
                     cp.int32(axis),cp.int32(taps),meta,weights))
            self.build_normalizers(((720+15)//16,(1280+15)//16,2),(16,16),
                                   (mx,my,wx,wy,cp.int32(taps),normalizers))
            self.plans[key] = (mx,my,wx,wy,normalizers,cp.int32(taps))
        self.cached_fit(grid, block, (*args,*self.plans[key]))
