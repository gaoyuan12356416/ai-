"""Explicit decoder admission and GPU upload for formats NVDEC cannot decode.

CPU decoding is selected before rendering from actual device capabilities.
Runtime CUDA/decode failures never trigger a second renderer or decoder.
Pixel conversion, resampling and composition remain CUDA operations.
"""
from __future__ import annotations

import ctypes
from fractions import Fraction
from functools import lru_cache
import re


def pixel_layout(name, width, height):
    import av
    return av.VideoFormat(name, width, height)


@lru_cache(maxsize=32)
def _caps(codec, chroma, bits):
    from .native_video import codec_runtime
    _, nvc, _ = codec_runtime()
    names={'h264':'H264','hevc':'HEVC','av1':'AV1','vp8':'VP8','vp9':'VP9',
           'mpeg1video':'MPEG1','mpeg2video':'MPEG2','mpeg4':'MPEG4','vc1':'VC1','mjpeg':'JPEG'}
    if codec not in names:
        return {'supported':False, 'reason':'codec_has_no_nvdec_entry'}
    return dict(nvc.GetDecoderCaps(gpuid=0,codec=getattr(nvc.cudaVideoCodec,names[codec]),
                    chromaformat=getattr(nvc.cudaVideoChromaFormat,'Monochrome' if chroma==400 else str(chroma)),bitdepth=bits))


def decoder_choice(video):
    fmt=pixel_layout(video['pix_fmt'],int(video['width']),int(video['height']))
    width,height=int(video['width']),int(video['height'])
    bits=max(c.bits for c in fmt.components)
    if fmt.is_rgb:
        chroma=444
    elif len(fmt.components)==1:
        chroma=400
    else:
        cw,ch=fmt.chroma_width(),fmt.chroma_height()
        chroma=420 if (cw,ch)==((width+1)//2,(height+1)//2) else 422 if (cw,ch)==((width+1)//2,height) else 444
    caps=_caps(video['codec_name'],chroma,bits)
    supported=bool(caps.get('supported'))
    geometry=(supported and caps['width_min']<=width<=caps['width_max']
              and caps['height_min']<=height<=caps['height_max']
              and ((width+15)//16)*((height+15)//16)<=caps['mb_num_max'])
    if video.get('field_order') not in (None,'','unknown','progressive'):
        # SDK 2.2.3 NvDecoder.cpp forces Adaptive for interlaced input and has
        # no Python Weave control. Preserve the established, un-deinterlaced
        # source rather than silently changing motion/field detail.
        return dict(backend='cpu_decode',reason='sdk_interlace_forces_adaptive',caps=caps)
    if not supported:
        return dict(backend='cpu_decode',reason='nvdec_format_unsupported',caps=caps)
    if not geometry:
        return dict(backend='cpu_decode',reason='nvdec_geometry_unsupported',caps=caps)
    if chroma==420 and (width%2 or height%2):
        # SDK 2.2.3's NV12 CAI plane shape is not qualified for odd extents.
        # Preserve odd edge samples through the existing generic CUDA planar
        # sampler after software decode; this is not a T4 codec limitation.
        return dict(backend='cpu_decode',reason='sdk_odd_420_surface_unqualified',caps=caps)
    return dict(backend='nvdec',reason='hardware_supported',caps=caps)


class _Component(ctypes.Structure):
    _fields_=[(key,ctypes.c_int) for key in ('plane','step','offset','shift','depth')]


class _Descriptor(ctypes.Structure):
    _fields_=[('name',ctypes.c_char_p),('nb_components',ctypes.c_uint8),
              ('log2_chroma_w',ctypes.c_uint8),('log2_chroma_h',ctypes.c_uint8),
              ('flags',ctypes.c_uint64),('comp',_Component*4),('alias',ctypes.c_char_p)]


@lru_cache(maxsize=64)
def descriptor(name):
    import av
    lib=ctypes.CDLL(av._core.__file__)
    lib.av_pix_fmt_desc_get.argtypes=[ctypes.c_int]
    lib.av_pix_fmt_desc_get.restype=ctypes.POINTER(_Descriptor)
    desc=lib.av_pix_fmt_desc_get(int(av.VideoFormat(name))).contents
    # Keep metadata only; the FFmpeg-owned static descriptor is never mutated.
    return dict(flags=desc.flags,components=[{key:getattr(comp,key) for key in
        ('plane','step','offset','shift','depth')} for comp in list(desc.comp)[:desc.nb_components]])


class UploadedFrame:
    def __init__(self,cp,frame,video,time_base,fallback_duration):
        self.timestamp=Fraction(frame.pts)*frame.time_base/time_base
        self.duration=frame.duration*frame.time_base if frame.duration else fallback_duration
        self.width,self.height=frame.width,frame.height
        self.format=frame.format.name
        self.video=dict(video)
        self.video['pix_fmt']=self.format
        ranges={1:'tv',2:'pc'}
        matrices={1:'bt709',4:'fcc',5:'bt470bg',6:'smpte170m',7:'smpte240m',9:'bt2020nc',10:'bt2020c'}
        if int(frame.color_range) in ranges: self.video['color_range']=ranges[int(frame.color_range)]
        if int(frame.colorspace) in matrices: self.video['color_space']=matrices[int(frame.colorspace)]
        desc=descriptor(self.format)
        # A CPU decoder must not return another hardware surface here.
        if desc['flags'] & (1<<3):
            raise ValueError('native_video_gpu_sampler_unimplemented_'+self.format)
        self.planes=[]
        for plane in frame.planes:
            height=max(1,plane.height)
            stride=plane.line_size or plane.buffer_size//height
            array=cp.empty((height,stride),dtype=cp.uint8)
            cp.cuda.runtime.memcpy(array.data.ptr,plane.buffer_ptr,array.nbytes,
                                   cp.cuda.runtime.memcpyHostToDevice)
            self.planes.append(array)
        self.rgb=bool(desc['flags'] & (1<<5))
        self.big_endian=bool(desc['flags'] & 1)
        self.float_data=bool(desc['flags'] & (1<<9))
        self.component_data=[]
        if desc['flags'] & (1<<8):
            # Bilinear Bayer demosaic uses only device neighbor gathers.
            pattern=re.search(r'bayer_([rgb]{4})',self.format).group(1)
            size=desc['components'][0]['step'];depth=size*8
            raw=self.planes[0][:,:self.width*size].copy()
            if size==2:
                raw=raw.view(cp.uint16)
                if self.big_endian: raw=raw.byteswap()
            raw=raw.astype(cp.float32)/float((1<<depth)-1)
            x,y=cp.arange(self.width),cp.arange(self.height)
            xm,xp=cp.maximum(x-1,0),cp.minimum(x+1,self.width-1)
            ym,yp=cp.maximum(y-1,0),cp.minimum(y+1,self.height-1)
            horizontal=(raw[:,xm]+raw[:,xp])*.5
            vertical=(raw[ym,:]+raw[yp,:])*.5
            diagonal=(raw[ym[:,None],xm[None,:]]+raw[ym[:,None],xp[None,:]]+
                      raw[yp[:,None],xm[None,:]]+raw[yp[:,None],xp[None,:]])*.25
            parity=(y[:,None]%2)*2+x[None,:]%2
            red_index=pattern.index('r');blue_index=pattern.index('b')
            red=cp.where(parity==red_index,raw,cp.where(parity==blue_index,diagonal,
                         cp.where(y[:,None]%2==red_index//2,horizontal,vertical)))
            blue=cp.where(parity==blue_index,raw,cp.where(parity==red_index,diagonal,
                          cp.where(y[:,None]%2==blue_index//2,horizontal,vertical)))
            green=cp.where((parity==red_index)|(parity==blue_index),(horizontal+vertical)*.5,raw)
            self._float_rgb(cp,cp.stack((red,green,blue),axis=2))
            return
        if desc['flags'] & (1<<1):
            # FFmpeg PAL8 palettes contain native-endian ARGB words. Indexing
            # and expansion happen on the GPU; no CPU palette conversion.
            colors=self.planes[1].ravel().view(cp.uint32)[:256]
            packed=colors[self.planes[0][:,:self.width]].view(cp.uint8).reshape(self.height,self.width,4)
            self.planes.append(packed)
            self.rgb,self.big_endian,self.float_data=True,False,False
            self.component_data=[(packed,packed.strides[0],4,o,0,8,self.width,self.height) for o in (2,1,0,3)]
            return
        if desc['flags'] & (1<<2):
            if self.format in ('monob','monow'):
                x=cp.arange(self.width)
                unpacked=(self.planes[0][:,x//8]>>(7-x%8))&1
                if self.format=='monow': unpacked=1-unpacked
                unpacked=(unpacked*255).astype(cp.uint8)
                self.planes.append(unpacked)
                self.component_data=[(unpacked,unpacked.strides[0],1,0,0,8,self.width,self.height)]
                self.video['color_range']='pc'
            else:
                channels=[]
                for layout in desc['components']:
                    x=cp.arange(self.width)*layout['step']+layout['offset']
                    bits=layout['depth'];mask=(1<<bits)-1
                    values=(self.planes[layout['plane']][:,x//8]>>(8-bits-x%8-layout['shift']))&mask
                    channels.append(values.astype(cp.float32)/mask)
                self._float_rgb(cp,cp.stack(channels,axis=2))
            return
        if self.format.startswith('gray') and self.video.get('color_range') not in ('pc','tv'):
            self.video['color_range']='pc'
        for component,layout in zip(frame.format.components,desc['components']):
            plane=self.planes[layout['plane']]
            self.component_data.append((plane,plane.strides[0],layout['step'],layout['offset'],
                layout['shift'],layout['depth'],component.width,component.height))

    def _float_rgb(self,cp,packed):
        self.planes.append(packed)
        self.rgb,self.big_endian,self.float_data=True,False,True
        self.component_data=[(packed,packed.strides[0],packed.strides[1],i*4,0,32,
                              self.width,self.height) for i in range(packed.shape[2])]


def cpu_decode_frames(source,video,check):
    import av
    from .native_video import codec_runtime
    cp,_,_=codec_runtime()
    with av.open(str(source)) as container:
        stream=container.streams.video[0]
        stream.codec_context.thread_count=2
        tb=Fraction(video['time_base'])
        fallback=1/Fraction(stream.average_rate or stream.base_rate or 30)
        for frame in container.decode(stream):
            check()
            if frame.pts is None:
                raise ValueError('native_video_source_timestamp_missing')
            yield Fraction(frame.pts)*frame.time_base/tb, UploadedFrame(cp,frame,video,tb,fallback)


def native_components(frame,video):
    """Describe NVDEC planar 4:4:4 buffers without a host-pixel conversion."""
    frame.height,frame.width=frame.planes[0].shape[:2]
    fmt=pixel_layout(video['pix_fmt'],frame.width,frame.height)
    desc=descriptor(video['pix_fmt'])
    frame.video=dict(video)
    frame.rgb=fmt.is_rgb
    frame.big_endian=False
    frame.float_data=False
    frame.component_data=[]
    for component,layout in zip(fmt.components,desc['components']):
        plane=frame.planes[layout['plane']]
        size=plane.dtype.itemsize
        frame.component_data.append((plane,plane.strides[0],size,0,
                                     16-frame.bits if size==2 else 0,
                                     frame.bits,component.width,component.height))
