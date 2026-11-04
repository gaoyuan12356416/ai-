"""CUDA-resident video rendering and timestamped NVENC access units.

This module does not select jobs, publish artifacts, encode audio or mux files.
The caller owns the deadline/cancellation callback and immutable asset inputs.
Only compressed bytes, timestamps and small metadata leave device memory.
"""
from __future__ import annotations

from contextlib import ExitStack
from fractions import Fraction
import json
import math
from pathlib import Path
import re

from .gpu_compute import cupy_runtime

IMPLEMENTATION = 'nvdec-cuda-lanczos-nvenc-native-v1'
NVC_VERSION = '2.2.3'
WIDTH, HEIGHT, FPS = 720, 1280, 30


def round_near_inf(value):
    """FFmpeg AV_ROUND_NEAR_INF, without binary floating-point ties."""
    value = Fraction(value)
    if value < 0:
        return -round_near_inf(-value)
    return (value.numerator * 2 + value.denominator) // (2 * value.denominator)


def cfr_frames(frames, time_base, start_time, duration, check):
    """Hold/drop frames on the existing fps=30:start_time=0 time grid.

    `frames` yields (native timestamp, owned GPU frame). One-frame look-ahead
    bounds memory and allows native VFR timestamps and an initial video delay.
    A caller must supply the shared container origin, not video start_time.
    """
    # The delivery -t bound is rescaled into the 1/30 encoder clock using
    # nearest rounding; even a positive sub-frame bound emits the first frame.
    count = max(1, round_near_inf(Fraction(str(duration)) * FPS))
    origin = Fraction(str(start_time))
    tick = Fraction(time_base) * FPS
    previous = None
    previous_timestamp = None
    emitted = 0
    for timestamp, frame in frames:
        check()
        at = round_near_inf(Fraction(timestamp) * tick - origin * FPS)
        if previous is not None:
            while emitted < min(count, at):
                check()
                yield emitted, previous
                emitted += 1
        previous = frame
        previous_timestamp = timestamp
        if emitted >= count:
            return
    if previous is None:
        raise ValueError('native_video_no_decoded_frames')
    # fps eof_action=round ends at the rounded decoder EOF timestamp. The
    # container duration may be longer because audio continues after video.
    frame_duration = getattr(previous, 'duration', None)
    if frame_duration is not None:
        end = Fraction(previous_timestamp)*Fraction(time_base) + frame_duration - origin
        count = min(count, max(0, round_near_inf(end*FPS)))
    while emitted < count:
        check()
        yield emitted, previous
        emitted += 1


class CudaView:
    """Normalize SDK CAI lists and retain the actual allocation owner."""
    def __init__(self, array, owner=None):
        self.owner = owner if owner is not None else array
        self.__cuda_array_interface__ = dict(array.__cuda_array_interface__)
        for key in ('shape', 'strides'):
            if self.__cuda_array_interface__.get(key) is not None:
                self.__cuda_array_interface__[key] = tuple(self.__cuda_array_interface__[key])
        # SDK 2.2.3 reports P016 strides in elements (including a final stride
        # of 1), although CAI requires bytes. CUDA/NVDEC's actual UV address is
        # width*height*2 bytes after Y. CuPy-owned uint16 views already use 2.
        strides = self.__cuda_array_interface__.get('strides')
        if self.__cuda_array_interface__.get('typestr') == '|u2' and strides and strides[-1] == 1:
            self.__cuda_array_interface__['strides'] = tuple(s*2 for s in strides)
        if self.__cuda_array_interface__.get('stream') == 0:
            self.__cuda_array_interface__['stream'] = 1


class PackedFrame:
    def __init__(self, array):
        self.view = CudaView(array)
        self.view.__cuda_array_interface__['strides'] = tuple(array.strides)

    def cuda(self):
        return self.view


class OwnedDecodeFrame:
    """NVDEC output leases end on the next Decode; retain a device copy."""
    def __init__(self, cp, frame, duration=None, bits=8):
        self.timestamp = frame.timestamp
        self.duration = duration
        self.bits = bits
        self.planes = [cp.asarray(CudaView(p, frame)).copy() for p in frame.cuda()]
        self.views = [CudaView(p) for p in self.planes]
        cp.cuda.get_current_stream().synchronize()

    def cuda(self):
        return self.views


def codec_runtime():
    cp = cupy_runtime()
    cp.cuda.Device(0).use()
    cp.empty(1)
    import PyNvVideoCodec as nvc
    # Older releases silently ignore CQ or discard encoded timestamps.
    if nvc.__version__ != NVC_VERSION:
        raise RuntimeError('native_video_requires_pynvvideocodec_' + NVC_VERSION)
    return cp, nvc, cp.cuda.driver.ctxGetCurrent()


def bitrate_number(value):
    match=re.fullmatch(r'([0-9]+(?:\.[0-9]+)?)([kKmM]?)',str(value))
    if not match:
        raise ValueError('native_video_invalid_bitrate')
    return int(float(match[1])*{'':1,'k':1000,'m':1000000}[match[2].lower()])


def create_encoder(nvc,width,height,context,stream,options):
    """Preserve both CQ and a nonzero budget despite the SDK CQ shortcut."""
    construction=dict(options)
    bounded_cq=('cq' in options and any(bitrate_number(options.get(k,0))>0
                                      for k in ('bitrate','maxbitrate')))
    if bounded_cq:
        construction.pop('cq')
    encoder=nvc.CreateEncoder(width,height,'ABGR',False,cudacontext=context,
                             cudastream=stream,**construction)
    config=encoder.GetEncodeReconfigureParams()
    initial={key:int(getattr(config,key)) for key in ('averageBitrate','maxBitRate',
             'vbvBufferSize','vbvInitialDelay','targetQuality')}
    for option,field in (('bitrate','averageBitrate'),('maxbitrate','maxBitRate'),
                         ('vbvbufsize','vbvBufferSize'),('vbvinit','vbvInitialDelay')):
        if option in construction and initial[field]!=bitrate_number(construction[option]):
            raise RuntimeError('native_video_encoder_rejected_'+option)
    applied=None
    if bounded_cq:
        # SetupNvEncRcParams deliberately zeroes both bitrates when `cq` is
        # supplied to CreateEncoder. Reconfigure sends all preserved fields
        # plus CQ to NVENC. Its getter remains a constructor-cache snapshot.
        config.targetQuality=int(options['cq'])
        if not encoder.Reconfigure(config):
            raise RuntimeError('native_video_encoder_cq_reconfigure_failed')
        actual_cq=int(options['cq'])
        applied=dict(initial,targetQuality=actual_cq)
    else:
        actual_cq=initial['targetQuality']
        if 'cq' in options and actual_cq!=float(options['cq']):
            raise RuntimeError('native_video_encoder_did_not_accept_cq')
    return encoder,dict(initial=initial,applied_reconfigure=applied,
                        target_quality=actual_cq,getter_is_constructor_snapshot=True)


def decode_frames(source, check, *, caps=None):
    """Yield NVDEC native GPU surfaces in display order with exact PTS."""
    cp, nvc, context = codec_runtime()
    demuxer = nvc.CreateDemuxer(filename=str(source))
    # Keep the existing dynamic-resolution allowance for ordinary material,
    # without allocating an 8K decoder for every 720p task. A wider initial
    # stream gets its actual initial extent, subject to queried device limits.
    maxwidth, maxheight = max(4096,demuxer.Width()), max(4096,demuxer.Height())
    if caps:
        maxwidth, maxheight = min(maxwidth,caps['width_max']), min(maxheight,caps['height_max'])
    decoder = nvc.CreateDecoder(gpuid=0, codec=demuxer.GetNvCodecId(),
        cudacontext=context, cudastream=cp.cuda.get_current_stream().ptr,
        usedevicememory=True, maxwidth=maxwidth, maxheight=maxheight,
        outputColorType=nvc.OutputColorType.NATIVE)
    durations = {}
    tb = Fraction(demuxer.GetTimebaseNum(),demuxer.GetTimebaseDen())
    fallback_duration = Fraction(demuxer.RFrameRateDen(),demuxer.RFrameRateNum())
    for packet in demuxer:
        check()
        if packet.bsl:
            durations[packet.pts] = packet.duration*tb if packet.duration>0 else fallback_duration
        for frame in decoder.Decode(packet):
            check()
            duration = durations.pop(frame.timestamp, fallback_duration)
            yield frame.timestamp, OwnedDecodeFrame(cp, frame, duration, demuxer.BitDepth())


class CachedAsset:
    """Demux verified raw RGBA NUT packets, upload once when cache fits.

    No codec, colorspace or resize runs on CPU. Static layers stay resident;
    long animated layers stream through two GPU surfaces without allocating an
    entire asset per concurrent worker. The demuxer's packet memory is copied
    directly to CUDA, avoiding a Python bytes/NumPy image copy.
    """
    def __init__(self, path, check, cache_bytes=32*1024*1024):
        self.path, self.check = Path(path), check
        self.cp, self.nvc, _ = codec_runtime()
        self.cache = [] if self.path.stat().st_size <= cache_bytes else None
        self.stream = self._frames()
        self.current = self.next = None
        self.last_index = -1

    def _frames(self):
        shift = Fraction(0)
        while True:
            first, end, saw = None, None, False
            if self.cache:
                for pts, duration, frame in self.cache:
                    self.check()
                    first = pts if first is None else first
                    end, saw = pts+duration, True
                    yield pts+shift, frame
            else:
                demuxer = self.nvc.CreateDemuxer(filename=str(self.path))
                width, height = demuxer.Width(), demuxer.Height()
                tb = Fraction(demuxer.GetTimebaseNum(),demuxer.GetTimebaseDen())
                rate = Fraction(demuxer.RFrameRateNum(),demuxer.RFrameRateDen())
                for packet in demuxer:
                    self.check()
                    if not packet.bsl:
                        continue
                    size = width*height*4
                    if packet.bsl != size:
                        raise ValueError('native_asset_requires_verified_raw_rgba_packet')
                    pts = packet.pts * tb
                    duration = packet.duration*tb if packet.duration > 0 else 1/rate
                    first = pts if first is None else first
                    end, saw = pts+duration, True
                    frame = self.cp.empty((height,width,4),dtype=self.cp.uint8)
                    self.cp.cuda.runtime.memcpy(frame.data.ptr, int(packet.bsl_data), size,
                                                self.cp.cuda.runtime.memcpyHostToDevice)
                    if self.cache is not None:
                        self.cache.append((pts,duration,frame))
                    yield pts+shift, frame
            if not saw or end <= first:
                raise ValueError('native_asset_empty_or_invalid_clock')
            shift += end-first

    def frame_at(self, index):
        if index < self.last_index:
            raise ValueError('native_asset_clock_must_be_monotonic')
        self.last_index = index
        if self.next is None:
            self.next = next(self.stream)
        if self.current is None:
            self.current = self.next[1]
        while round_near_inf(self.next[0]*FPS) <= index:
            self.current = self.next[1]
            self.next = next(self.stream)
        return self.current

    def close(self):
        self.stream.close()
        self.current = self.next = None
        if self.cache is not None:
            self.cache.clear()


class CachedAssets:
    def __init__(self, original_paths, cache_root, check, cache_bytes=128*1024*1024):
        from .asset_cache import cached_path
        names = {'border':'border','opacity':'opacity_video','corners':'corners','tint':'tint'}
        self.layers = {name:CachedAsset(cached_path(original_paths[key], cache_root),
                                       check, cache_bytes//4)
                       for name,key in names.items()}

    def __call__(self, index):
        return {key:layer.frame_at(index) for key,layer in self.layers.items()}

    def close(self):
        for layer in self.layers.values():
            layer.close()


class NativeRenderer:
    def __init__(self, recipe, video):
        from .compositor import kernel_source, validate_source_overlay
        kernel_source(recipe)  # Reuse frozen recipe validation.
        self.overlay = validate_source_overlay(recipe)
        self.recipe, self.video = recipe, video
        from .gpu_pipeline import qualification,ENV
        # Match the former metadata admission's CPU-fit/OpenCL-fit distinction.
        # Do not resurrect its CPU keyframe decode of the complete source just
        # to choose chroma siting. Dynamic frames use their actual dimensions
        # while retaining this initial sampling style; provenance records it.
        self.legacy_fit=qualification({'video':video},{ENV:'1'})!='qualified'
        self.cp, self.nvc, self.context = codec_runtime()
        self.stream = self.cp.cuda.get_current_stream()
        source = Path(__file__).with_suffix('.cu').read_text(encoding='utf-8')
        self.module = self.cp.RawModule(code=source, options=('--std=c++11',),
                                      name_expressions=('fit_nv12', 'compose_rgba','fit_components'))
        self.fit = self.module.get_function('fit_nv12')
        self.compose = self.module.get_function('compose_rgba')
        self.components = self.module.get_function('fit_components')
        self.back = self.cp.empty((HEIGHT, WIDTH, 4), dtype=self.cp.uint8)
        self.main = self.cp.empty_like(self.back)
        self.output = self.cp.empty_like(self.back)

    def render(self, frame, assets):
        """assets maps border/opacity/corners/tint to owned RGBA device arrays."""
        cp = self.cp
        if hasattr(frame,'component_data'):
            self._fit_component_frame(frame)
            return self._compose_frame(self.main, assets)
        planes = frame.cuda()
        if len(planes) != 2:
            from .native_video_compat import native_components
            native_components(frame,self.video)
            self._fit_component_frame(frame)
            return self._compose_frame(self.main,assets)
        y = cp.asarray(CudaView(planes[0], frame))
        uv = cp.asarray(CudaView(planes[1], frame))
        bits=8
        if y.dtype == cp.uint16 and uv.dtype == cp.uint16:
            match=re.search(r'p(10|12|16)(?:le|be)',self.video.get('pix_fmt',''))
            if not match:
                raise ValueError('native_video_high_bit_depth_not_identified')
            bits=int(match.group(1))
        elif y.dtype != cp.uint8 or uv.dtype != cp.uint8:
            raise ValueError('native_video_requires_nv12_or_p016_surface')
        ih, iw = y.shape[:2]
        if iw % 2 or ih % 2:
            raise ValueError('native_video_requires_even_geometry')
        chroma = self.video.get('chroma_location')
        args = (y, uv, cp.int32(y.strides[0]), cp.int32(uv.strides[0]),
                cp.int32(iw), cp.int32(ih))
        color = (cp.int32(self.video.get('color_range') == 'pc' or
                          self.video.get('pix_fmt') == 'yuvj420p'),
                 cp.int32({'bt709':1,'bt2020nc':2,'bt2020c':2,'smpte240m':3,'fcc':4}.get(self.video.get('color_space'),0)),
                 cp.float32(.5 if chroma == 'center' else 0),
                 cp.float32(0 if chroma == 'topleft' else .5), cp.float32(1),cp.int32(bits),cp.int32(self.legacy_fit))
        grid, block = ((WIDTH+15)//16, (HEIGHT+15)//16), (16,16)
        self.fit(grid, block, (*args, self.back, cp.int32(1), *color))
        if iw * HEIGHT == ih * WIDTH:
            main = self.back
        else:
            self.fit(grid, block, (*args, self.main, cp.int32(0), *color))
            main = self.main
        return self._compose_frame(main,assets)

    def _fit_component_frame(self,frame):
        cp=self.cp
        pointers=cp.asarray([c[0].data.ptr for c in frame.component_data],dtype=cp.uint64)
        metadata=cp.asarray([int(v) for c in frame.component_data for v in c[1:]],dtype=cp.int32)
        video=frame.video
        args=(pointers,metadata,cp.int32(len(frame.component_data)),cp.int32(frame.width),cp.int32(frame.height),
              cp.int32(frame.rgb),cp.int32(frame.big_endian),cp.int32(frame.float_data))
        color=(cp.int32(video.get('color_range')=='pc' or video.get('pix_fmt','').startswith('yuvj')),
               cp.int32({'bt709':1,'bt2020nc':2,'bt2020c':2,'smpte240m':3,'fcc':4}.get(video.get('color_space'),0)),
               cp.int32(video.get('chroma_location')=='center'),cp.int32(video.get('chroma_location')=='topleft'),cp.int32(self.legacy_fit))
        grid,block=((WIDTH+15)//16,(HEIGHT+15)//16),(16,16)
        for target,cover in ((self.back,1),(self.main,0)):
            self.components(grid,block,(*args,target,cp.int32(cover),*color))
        self.stream.synchronize()

    def _compose_frame(self, main, assets):
        cp=self.cp
        grid,block=((WIDTH+15)//16,(HEIGHT+15)//16),(16,16)
        layers = []
        for name in ('border', 'opacity', 'corners', 'tint'):
            array = assets[name]
            if (array.dtype != cp.uint8 or array.ndim != 3 or array.shape[2] != 4
                    or not array.flags.c_contiguous):
                raise ValueError('native_video_requires_contiguous_rgba_asset')
            layers.extend((array, cp.int32(array.shape[1]), cp.int32(array.shape[0])))
        scale = self.recipe['scale_bp'] / 10000
        self.compose(grid, block, (self.back, main, *layers, self.output,
            cp.int32(math.floor(WIDTH*scale/2)*2), cp.int32(math.floor(HEIGHT*scale/2)*2),
            cp.float32(self.recipe['rotation_millidegrees']*math.pi/180000),
            cp.float32(self.recipe['tint_opacity_bp']/10000),
            cp.float32(self.overlay['opacity_bp']/10000 if self.overlay else 0),
            cp.float32(self.overlay['scale_bp']/10000 if self.overlay else 1)))
        # Decoder surfaces may be recycled on the next Decode call. Wait on the
        # GPU kernel, without ever copying raw frames to host memory.
        self.stream.synchronize()
        return self.output


def encode_video(source, output, info, recipe, assets_at, check, *, duration=None,
                 encoder_options=None):
    """Write Annex B H264/HEVC and an AU sidecar for the independent muxer.

    `assets_at(frame_index)` must return four GPU RGBA arrays. Compressed AU
    offsets/sizes refer to output. PTS/DTS/duration use the exact 1/30 timebase.
    No file is promoted or task state mutated by this function.
    """
    renderer = NativeRenderer(recipe, info['video'])
    nvc = renderer.nvc
    options = (dict(codec='h264', profile='high', preset='P5', rc='vbr', cq=21,
                   bitrate=0, fps=FPS, gop=60, idrperiod=60)
               if encoder_options is None else dict(encoder_options))
    if options.get('codec') not in ('h264','hevc') or int(options.get('fps', 0)) != FPS:
        raise ValueError('native_video_requires_h264_or_hevc_30fps_contract')
    # PyNvVideoCodec 2.2.3 SetupVuiConfig's legacy `colorspace` option also
    # sets full-range RGB conversion without signalling it in SPS. H264's
    # verified default is limited BT.601; HEVC needs an explicit context below.
    if 'colorspace' in options:
        raise ValueError('native_video_colorspace_option_changes_range')
    # Match FFmpeg's MP4 global-header mode. Otherwise SPS/SEI byte overhead
    # changes NVENC's frame bit budget and cascades into different quality.
    options.setdefault('global_header', 1)
    with ExitStack() as resources:
        construction = dict(options)
        if options['codec'] == 'hevc':
            # The HEVC default converts ABGR with BT.709 even without a VUI.
            # The official SDK context supplies the existing limited BT.601
            # conversion and tags. It describes output, never input color.
            from .color_contract import OutputColorContract
            color = resources.enter_context(OutputColorContract(nvc, Path(output).parent))
            construction.update(color.encoder_options)
        return _encode_video(renderer, source, output, info, assets_at, check,
                             duration, options, construction)


def _encode_video(renderer, source, output, info, assets_at, check, duration,
                  options, construction):
    nvc = renderer.nvc
    encoder,encoder_configuration=create_encoder(nvc,WIDTH,HEIGHT,renderer.context,
                                                renderer.stream.ptr,construction)
    try:
        return _encode_frames(encoder, encoder_configuration, renderer, source,
                              output, info, assets_at, check, duration, options)
    finally:
        # Release the encoder before its owning output-color context closes,
        # including cancellation and failures during rendering or EndEncode.
        del encoder


def _encode_frames(encoder, encoder_configuration, renderer, source, output,
                   info, assets_at, check, duration, options):
    nvc = renderer.nvc
    actual_cq=encoder_configuration['target_quality']
    sequence = bytes(encoder.GetSequenceParams())
    packets, offset, submitted = [], len(sequence), 0
    total = info.get('duration') if duration is None else duration
    from .native_video_compat import decoder_choice,cpu_decode_frames
    choice=decoder_choice(info['video'])
    decoded=(decode_frames(source,check,caps=choice['caps']) if choice['backend']=='nvdec' else
             cpu_decode_frames(source,info['video'],check))
    frames = cfr_frames(decoded, info['video']['time_base'],
                        (info.get('format') or {}).get('start_time', '0'), total, check)
    with Path(output).open('wb') as handle:
        # A standalone validation stream needs a header. It is outside the AU
        # offsets: the MP4 muxer stores the same header only in avcC.
        handle.write(sequence)
        def write(encoded):
            nonlocal offset
            for packet in encoded:
                check()
                data = packet['data']
                handle.write(data)
                packets.append({'offset':offset, 'size':len(data),
                    'pts':int(packet['timestamp']), 'duration':1,
                    'picture_type':int(packet['picture_type'])})
                offset += len(data)
        for index, frame in frames:
            check()
            image = renderer.render(frame, assets_at(index))
            params = nvc.NV_ENC_PIC_PARAMS()
            params.inputTimeStamp, params.frameIdx, params.inputDuration = index, index, 1
            write(encoder.Encode(PackedFrame(image), params))
            submitted += 1
        write(encoder.EndEncode())
    if sorted(p['pts'] for p in packets) != list(range(submitted)):
        raise ValueError('native_video_encoded_timeline_incomplete')
    delay = max((index - packet['pts'] for index, packet in enumerate(packets)), default=0)
    for index, packet in enumerate(packets):
        packet['dts'] = index - delay
    record = {'implementation':IMPLEMENTATION, 'codec_version':NVC_VERSION,
        'codec':options['codec'],'source_decoder':choice,
        'source_fit':('legacy_swscale_chroma_coordinates' if renderer.legacy_fit
                      else 'qualified_opencl_chroma_coordinates'),
        'fit_selection':'source_metadata_no_cpu_keyframe_scan',
        'time_base':'1/30', 'frame_count':submitted, 'decode_delay':delay,
        'encoder_bframes':encoder.GetNumBFrames(), 'target_quality':actual_cq,
        'encoder_options':options,
        'output_color_contract':('fixed_bt601_limited_30fps_context' if options['codec']=='hevc'
                                 else 'verified_h264_limited_bt601_default'),
        'encoder_configuration':encoder_configuration,
        'sequence_header_hex':sequence.hex(), 'packets':packets}
    Path(output).with_suffix('.packets.json').write_text(json.dumps(record), encoding='utf-8')
    return record
