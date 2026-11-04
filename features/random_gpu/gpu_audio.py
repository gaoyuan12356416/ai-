"""Explicit CUDA DSP, a native-codec bridge and offline GPU codec adapters.

The CUDA operators are not an AAC decoder or encoder. prepare_audio_track
explicitly retains CPU compressed decode and native AAC encode while moving
PCM arithmetic to CUDA. Experimental torch-aac adapters remain unqualified
for production. There is no silent CPU fallback for CUDA DSP.
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
import math
from pathlib import Path
import threading
import tempfile
import logging
import subprocess
from contextlib import contextmanager, ExitStack
from typing import Iterator


class AudioQualificationError(ValueError):
    pass


@dataclass(frozen=True)
class AudioTimeline:
    input_rate: int
    output_rate: int
    input_samples: int
    output_samples: int
    start_output_sample: int

    @classmethod
    def create(cls, *, input_rate, input_samples, duration_seconds,
               start_seconds=0, output_rate=48000):
        if isinstance(input_rate, bool) or not isinstance(input_rate, int) or input_rate <= 0:
            raise AudioQualificationError("invalid_input_rate")
        if isinstance(output_rate, bool) or not isinstance(output_rate, int) or output_rate <= 0:
            raise AudioQualificationError("invalid_output_rate")
        if isinstance(input_samples, bool) or not isinstance(input_samples, int) or input_samples < 0:
            raise AudioQualificationError("invalid_input_samples")
        try:
            duration = Fraction(str(duration_seconds))
            start = Fraction(str(start_seconds))
        except (ValueError, ZeroDivisionError):
            raise AudioQualificationError("invalid_audio_timeline") from None
        if duration <= 0:
            raise AudioQualificationError("invalid_audio_duration")
        # FFmpeg's output-duration boundary rounds to the nearest sample.
        # Use rational arithmetic and ties away from zero (not Python's
        # bankers' rounding), including for negative source timestamps.
        def nearest(value):
            sign = -1 if value < 0 else 1
            value = abs(value)
            return sign * ((value.numerator * 2 + value.denominator) //
                           (value.denominator * 2))
        count = nearest(duration * output_rate)
        if count == 0:
            raise AudioQualificationError("audio_duration_below_one_sample")
        origin = nearest(start * output_rate)
        return cls(input_rate, output_rate, input_samples, count, origin)


@dataclass(frozen=True)
class DecodedAudio:
    """CPU compressed-decoder output, before any resampling/channel arithmetic."""
    path: Path
    rate: int
    channels: int
    samples: int
    dtype: str
    start_seconds: Fraction

    def array(self):
        import numpy as np
        if not self.samples:
            return np.empty((0, self.channels), dtype=self.dtype)
        return np.memmap(self.path, dtype=self.dtype, mode="r", shape=(self.samples,self.channels))


def decode_contiguous_audio(source, output, *, stop_seconds=None, check=None) -> DecodedAudio:
    """Decode with PyAV, preserving native PCM dtype and its real source clock.

    This is explicitly CPU decoding, not claimed as GPU acceleration. PCM
    numeric conversion, resampling, rematrixing and padding belong to CUDA.
    A discontinuous/rate-changing stream is not silently flattened; it needs
    separate qualification against FFmpeg's async timestamp compensation.
    """
    import av
    output = Path(output)
    sample_count = 0
    rate = channels = dtype = origin = None
    with av.open(str(source)) as container, output.open("xb") as handle:
        if not container.streams.audio:
            return DecodedAudio(output,48000,2,0,"float32",Fraction(0))
        stream = container.streams.audio[0]
        for frame in container.decode(stream):
            if check is not None:
                check()
            if frame.pts is None or frame.time_base is None:
                raise AudioQualificationError("missing_audio_timestamps")
            current = Fraction(frame.pts) * frame.time_base
            array = frame.to_ndarray()
            count_channels = len(frame.layout.channels)
            if count_channels not in (1,2):
                raise AudioQualificationError("channel_layout_requires_explicit_matrix")
            array = array.T if frame.format.is_planar else array.reshape(-1,count_channels)
            if rate is None:
                rate,channels,dtype,origin = frame.sample_rate,count_channels,array.dtype.str,current
            if (frame.sample_rate,count_channels,array.dtype.str) != (rate,channels,dtype):
                raise AudioQualificationError("changing_audio_format")
            # Container timestamps can have a coarser tick than one audio
            # sample (e.g. Matroska milliseconds). Permit that representation
            # rounding, but do not conceal a real timestamp hole.
            tolerance = max(Fraction(1,rate), Fraction(frame.time_base))
            if abs(current-origin-Fraction(sample_count,rate)) > tolerance:
                raise AudioQualificationError("discontinuous_audio_timestamps")
            handle.write(array.tobytes(order="C"))
            sample_count += len(array)
            if stop_seconds is not None and current+Fraction(frame.samples,frame.sample_rate) >= stop_seconds:
                break
    if rate is None:
        raise AudioQualificationError("audio_stream_has_no_samples")
    return DecodedAudio(output,rate,channels,sample_count,dtype,origin)


@dataclass(frozen=True)
class PreparedAudio:
    """Encoded AAC access units for the separate GPU MP4 muxer."""
    track: object
    spool: Path
    report: dict


_CPU_DSP_COMPATIBILITY_REASONS = frozenset({
    "channel_layout_requires_explicit_matrix", "changing_audio_format",
    "missing_audio_timestamps", "discontinuous_audio_timestamps",
})


def _decode_cpu_dsp_compatibility(source, output, duration, output_samples, *,
                                  origin, explicit_origin, check, ffmpeg):
    """Existing FFmpeg semantics for explicitly unqualified source formats.

    This is selected before any CUDA work, only for known source-clock/layout
    cases. It cannot catch or disguise a failed CUDA operation.
    """
    command=[str(ffmpeg),"-nostdin","-v","error","-n","-threads","2","-filter_threads","2"]
    if explicit_origin:
        command += ["-copyts","-itsoffset",format(float(-origin),".9f")]
    command += ["-i",str(source),"-map","0:a:0","-vn","-af",
                "aresample=48000:async=1:first_pts=0,apad","-ar","48000","-ac","2",
                "-t",format(float(duration),".9f"),"-c:a","pcm_f32le","-f","f32le",str(output)]
    with tempfile.TemporaryFile(dir=Path(output).parent) as error_file:
        process=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=error_file)
        last_progress_bytes = 0
        try:
            while process.poll() is None:
                # This callback marks actual progress for the parent's
                # watchdog, not a periodic "still alive" heartbeat.
                try:progress_bytes=Path(output).stat().st_size
                except FileNotFoundError:progress_bytes=0
                if progress_bytes > last_progress_bytes:
                    last_progress_bytes = progress_bytes
                    if check is not None:check()
                try:process.wait(timeout=.2)
                except subprocess.TimeoutExpired:pass
            if process.returncode:
                error_file.seek(0)
                detail=error_file.read()[-2000:].decode("utf-8",errors="replace")
                raise AudioQualificationError("cpu_audio_compatibility_failed: "+detail)
        finally:
            if process.poll() is None:
                process.terminate()
                try:process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill();process.wait(timeout=5)
    if Path(output).stat().st_size != output_samples*2*4:
        raise AudioQualificationError("cpu_audio_compatibility_duration_mismatch")
    return DecodedAudio(Path(output),48000,2,output_samples,"<f4",origin)


def prepare_audio_track(source, output_dir, duration_seconds, check=None,
                        timeline_origin_seconds=None, *, audio_bitrate=192000,
                        ffmpeg="/usr/bin/ffmpeg") -> PreparedAudio:
    """CPU compressed decode → CUDA DSP → qualified native CPU AAC encoder.

    This is the incremental integration path. It deliberately keeps the
    established native AAC codec while the experimental CUDA codec is not
    quality-qualified. No CPU muxer is used: raw AAC access units and original
    encoder PTS/DTS/duration go directly to the caller's GPU MP4 muxer.

    ``check`` is called during decode, each DSP block and each encoded packet;
    it may raise for cancellation. Caller owns the returned spool's lifetime.
    ``timeline_origin_seconds`` is the source clock corresponding to output
    zero; default matches FFmpeg's container-start normalization.
    """
    import av
    import numpy as np
    from .gpu_mux import PacketFile, Sample, Track
    directory = Path(output_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    if check is not None:
        check()
    if timeline_origin_seconds is None:
        with av.open(str(source)) as container:
            origin = Fraction(container.start_time, av.time_base) if container.start_time is not None else Fraction(0)
    else:
        origin = Fraction(str(timeline_origin_seconds))
    duration = Fraction(str(duration_seconds))
    expected = AudioTimeline.create(input_rate=48000,input_samples=0,duration_seconds=duration)
    if isinstance(audio_bitrate,bool) or not isinstance(audio_bitrate,int) or audio_bitrate<=0:
        raise AudioQualificationError("invalid_audio_bitrate")
    # Preserve enough source after the requested end for the FIR's right halo.
    # The decoder still stops within a bounded frame after that point.
    decode_stop = origin + duration + Fraction(1, 20)
    spool = None
    try:
        with tempfile.TemporaryDirectory(prefix="cuda-audio-", dir=directory) as scratch, ExitStack() as resources:
            compatibility_reason = None
            try:
                decoded = decode_contiguous_audio(source,Path(scratch)/"native.pcm",
                                                  stop_seconds=decode_stop,check=check)
            except AudioQualificationError as exc:
                reason = str(exc)
                if reason not in _CPU_DSP_COMPATIBILITY_REASONS:
                    raise
                compatibility_reason = reason
                logging.getLogger(__name__).warning("Audio source uses explicit CPU DSP compatibility: %s",reason)
                decoded = _decode_cpu_dsp_compatibility(source,Path(scratch)/"compat.pcm",duration,
                    expected.output_samples,origin=origin,explicit_origin=timeline_origin_seconds is not None,
                    check=check,ffmpeg=ffmpeg)
            timeline = AudioTimeline.create(input_rate=decoded.rate,input_samples=decoded.samples,
                duration_seconds=duration,start_seconds=decoded.start_seconds-origin)
            dsp = None if compatibility_reason else CudaAudioDsp()
            encoder = av.CodecContext.create("aac", "w")
            encoder.sample_rate = timeline.output_rate
            encoder.layout = "stereo"
            encoder.format = "fltp"
            encoder.bit_rate = audio_bitrate
            encoder.time_base = Fraction(1, timeline.output_rate)
            encoder.open()
            samples = []
            encoded_bytes = 0
            with tempfile.NamedTemporaryFile(prefix="audio-au-",suffix=".bin",dir=directory,
                                             delete=False) as handle:
                spool = Path(handle.name)
                def save(packet):
                    nonlocal encoded_bytes
                    if check is not None:
                        check()
                    if packet.pts is None or packet.dts is None or packet.time_base is None:
                        raise AudioQualificationError("aac_packet_missing_timestamp")
                    scale = packet.time_base * timeline.output_rate
                    values = [Fraction(value)*scale for value in (packet.pts,packet.dts,packet.duration)]
                    if any(value.denominator != 1 for value in values):
                        raise AudioQualificationError("aac_packet_fractional_sample_clock")
                    pts,dts,packet_duration = map(int,values)
                    if pts >= timeline.output_samples:
                        return
                    packet_duration = min(packet_duration,timeline.output_samples-dts)
                    if packet_duration <= 0 or pts != dts:
                        raise AudioQualificationError("aac_packet_invalid_timeline")
                    if samples and dts != samples[-1].dts+samples[-1].duration:
                        raise AudioQualificationError("aac_packet_discontinuous_timeline")
                    data = bytes(packet)
                    if not data:
                        raise AudioQualificationError("aac_packet_empty")
                    handle.write(data)
                    samples.append(Sample(PacketFile(spool,encoded_bytes,len(data)),pts,dts,packet_duration,True))
                    encoded_bytes += len(data)
                position = 0
                pcm = decoded.array()
                if hasattr(pcm,"_mmap"):
                    resources.callback(pcm._mmap.close)
                blocks = (pcm[offset:offset+65536] for offset in range(0,len(pcm),65536)) if dsp is None else dsp.blocks(pcm,timeline)
                for block in blocks:
                    if check is not None:
                        check()
                    # Planar conversion is done on GPU. Host work here is only
                    # transfer/copy; the codec consumes matching fltp/48k/stereo.
                    planar = np.ascontiguousarray(block.T) if dsp is None else dsp.cp.asnumpy(dsp.cp.ascontiguousarray(block.T))
                    frame = av.AudioFrame.from_ndarray(planar,format="fltp",layout="stereo")
                    frame.sample_rate = timeline.output_rate
                    frame.time_base = Fraction(1,timeline.output_rate)
                    frame.pts = position
                    for packet in encoder.encode(frame):
                        save(packet)
                    position += block.shape[0]
                for packet in encoder.encode(None):
                    save(packet)
            if not samples or samples[0].dts > 0 or samples[-1].dts+samples[-1].duration != timeline.output_samples:
                raise AudioQualificationError("aac_encoder_incomplete_timeline")
            config = encoder.extradata
            if not config:
                raise AudioQualificationError("aac_audio_specific_config_missing")
            track = Track("audio",timeline.output_rate,samples,bytes(config),channels=2,sample_rate=timeline.output_rate)
            return PreparedAudio(track,spool,{
                "dsp_backend":"cpu_ffmpeg_compatibility" if compatibility_reason else "cuda",
                "compatibility_reason":compatibility_reason,
                "decode_backend":"cpu_ffmpeg" if compatibility_reason else "cpu_pyav","encode_backend":"cpu_native_aac",
                "mux_backend":"caller_gpu_mux","sample_rate":timeline.output_rate,"channels":2,
                "target_bitrate":audio_bitrate,"output_samples":timeline.output_samples,"packets":len(samples),
                "priming_samples":-samples[0].dts,"last_packet_duration":samples[-1].duration,
                "encoded_bytes":encoded_bytes,"source_start_seconds":str(decoded.start_seconds),
                "timeline_origin_seconds":str(origin),"pyav":av.__version__,
                "libavcodec":av.library_versions.get("libavcodec"),
                "cpu_residual":["compressed audio decode","AAC codec","file I/O and transfers"]+
                    (["PCM DSP for "+compatibility_reason] if compatibility_reason else [])})
    except BaseException:
        if spool is not None:
            spool.unlink(missing_ok=True)
        raise


KERNEL = r'''
extern "C" __global__ void audio_sinc(
    const float* src, const float* matrix, float* dst,
    const long long source_base, const long long source_count,
    const long long input_samples, const long long output_base,
    const int output_count, const long long start_output_sample,
    const int input_rate, const int output_rate,
    const int input_channels, const int output_channels, const int half_width)
{
    const int item = blockDim.x * blockIdx.x + threadIdx.x;
    if (item >= output_count * output_channels) return;
    const int output_channel = item % output_channels;
    const long long n = output_base + item / output_channels;
    const double position = ((double)n - start_output_sample) * input_rate / output_rate;
    if (position < 0.0 || position >= input_samples || source_count == 0) {
        dst[item] = 0.0f; return;
    }
    // Preserve the no-resample case exactly apart from the explicit channel map.
    if (input_rate == output_rate) {
        const long long index = (long long)position - source_base;
        float value = 0.0f;
        for (int c = 0; c < input_channels; ++c)
            value += src[index * input_channels + c] * matrix[output_channel * input_channels + c];
        dst[item] = value; return;
    }
    const double cutoff = 0.95 * fmin(1.0, (double)output_rate / input_rate);
    const double radius = half_width / cutoff;
    const long long first = (long long)ceil(position - radius);
    const long long last = (long long)floor(position + radius);
    double value = 0.0, weight_sum = 0.0;
    const double pi = 3.1415926535897932384626433832795;
    for (long long index = first; index <= last; ++index) {
        const double distance = position - index;
        const double x = distance * cutoff;
        const double sinc = fabs(x) < 1.0e-12 ? 1.0 : sin(pi*x)/(pi*x);
        const double window = 0.42 + 0.5*cos(pi*distance/radius) + 0.08*cos(2*pi*distance/radius);
        const double weight = cutoff * sinc * window;
        weight_sum += weight;
        // Reflect the finite signal at its ends, as a media resampler does,
        // instead of inventing a discontinuity against zero inside the filter.
        long long reflected = index;
        if (input_samples == 1) reflected = 0;
        else while (reflected < 0 || reflected >= input_samples) {
            if (reflected < 0) reflected = -reflected;
            if (reflected >= input_samples) reflected = 2*input_samples-reflected-1;
        }
        const long long local = reflected - source_base;
        if (local < 0 || local >= source_count) continue;
        double mixed = 0.0;
        for (int c = 0; c < input_channels; ++c)
            mixed += src[local * input_channels + c] * matrix[output_channel * input_channels + c];
        value += weight * mixed;
    }
    dst[item] = (float)(value / weight_sum);
}
'''


class CudaAudioDsp:
    """Bounded GPU memory, global-coordinate sinc filtering and silence padding.

    Input is an ndarray or memory-mapped array shaped (samples, channels).
    Different blocks share the original input coordinate system and filter
    halos, so block boundaries do not reset the resampler. Explicit timestamp
    gaps, rate changes, and channel-layout changes must be handled upstream;
    this class deliberately does not pretend to reproduce swresample async
    compensation for an unqualified discontinuous source.
    """

    def __init__(self, *, device=0, block_samples=65536, half_width=32):
        if not isinstance(block_samples, int) or block_samples <= 0:
            raise ValueError("invalid_audio_block_size")
        if not isinstance(half_width, int) or not 8 <= half_width <= 128:
            raise ValueError("invalid_audio_filter_width")
        from .gpu_compute import cupy_runtime
        cp = cupy_runtime()
        self.cp = cp
        self.device = cp.cuda.Device(device)
        self.block_samples = block_samples
        self.half_width = half_width
        with self.device:
            self.kernel = cp.RawKernel(KERNEL, "audio_sinc")

    @staticmethod
    def channel_matrix(input_channels, matrix=None):
        if matrix is None:
            if input_channels == 2:
                return ((1.0, 0.0), (0.0, 1.0))
            if input_channels == 1:
                # FFmpeg's default mono(center) -> stereo rematrix coefficients.
                gain = math.sqrt(0.5)
                return ((gain,), (gain,))
            raise AudioQualificationError("channel_layout_requires_explicit_matrix")
        rows = tuple(tuple(float(v) for v in row) for row in matrix)
        if len(rows) != 2 or any(len(row) != input_channels for row in rows):
            raise AudioQualificationError("invalid_channel_matrix")
        if not all(math.isfinite(v) for row in rows for v in row):
            raise AudioQualificationError("invalid_channel_matrix")
        return rows

    def blocks(self, pcm, timeline: AudioTimeline, *, matrix=None) -> Iterator:
        import numpy as np
        cp = self.cp
        if len(pcm.shape) != 2 or pcm.shape[0] != timeline.input_samples:
            raise AudioQualificationError("audio_shape_mismatch")
        channels = int(pcm.shape[1])
        if channels < 1:
            raise AudioQualificationError("audio_channels_missing")
        if pcm.dtype.kind not in "fiu" or pcm.dtype.itemsize > 8:
            raise AudioQualificationError("unsupported_pcm_type")
        rows = self.channel_matrix(channels, matrix)
        with self.device:
            gpu_matrix = cp.asarray(rows, dtype=cp.float32)
            cutoff = 0.95 * min(1.0, timeline.output_rate / timeline.input_rate)
            halo = math.ceil(self.half_width / cutoff) + 2
            for offset in range(0, timeline.output_samples, self.block_samples):
                count = min(self.block_samples, timeline.output_samples - offset)
                left = (offset - timeline.start_output_sample) * timeline.input_rate / timeline.output_rate
                right = (offset + count - 1 - timeline.start_output_sample) * timeline.input_rate / timeline.output_rate
                first = max(0, min(timeline.input_samples, math.floor(left) - halo))
                last = max(first, min(timeline.input_samples, math.ceil(right) + halo + 1))
                data = cp.asarray(pcm[first:last]).astype(cp.float32, copy=False)
                if pcm.dtype.kind == "i":
                    data *= 1.0 / float(1 << (pcm.dtype.itemsize * 8 - 1))
                elif pcm.dtype.kind == "u":
                    midpoint = float(1 << (pcm.dtype.itemsize * 8 - 1))
                    data = (data - midpoint) / midpoint
                data = cp.ascontiguousarray(data)
                if not bool(cp.isfinite(data).all()):
                    raise AudioQualificationError("non_finite_audio_samples")
                output = cp.empty((count, 2), dtype=cp.float32)
                args = (data, gpu_matrix, output, np.int64(first), np.int64(last-first),
                        np.int64(timeline.input_samples), np.int64(offset), np.int32(count),
                        np.int64(timeline.start_output_sample), np.int32(timeline.input_rate),
                        np.int32(timeline.output_rate), np.int32(channels), np.int32(2),
                        np.int32(self.half_width))
                self.kernel(((count*2+255)//256,), (256,), args)
                yield output

    def write_pcm(self, pcm, timeline, handle, *, matrix=None):
        """Write f32le stereo PCM; only transfer and file I/O are host work."""
        samples = 0
        for block in self.blocks(pcm, timeline, matrix=matrix):
            host = self.cp.asnumpy(block)
            handle.write(host.astype("<f4", copy=False).tobytes())
            samples += len(host)
        return {"backend": "cuda", "output_samples": samples,
                "sample_rate": timeline.output_rate, "channels": 2,
                "compressed_decode_backend": "caller", "aac_encode_backend": "none"}


BITPACK_KERNEL = r'''
extern "C" __global__ void pack_audio_bits(
    const unsigned int* codes, const unsigned char* lengths,
    const long long* ends, unsigned char* output,
    const int count, const long long total_bits, const int output_bytes)
{
    const int byte = blockDim.x * blockIdx.x + threadIdx.x;
    if (byte >= output_bytes) return;
    long long bit = (long long)byte * 8;
    int low = 0, high = count;
    while (low < high) {
        const int middle = low + (high-low)/2;
        if (ends[middle] <= bit) low = middle+1;
        else high = middle;
    }
    int symbol = low, written = 0;
    unsigned int result = 0;
    while (written < 8 && bit < total_bits && symbol < count) {
        const long long begin = symbol ? ends[symbol-1] : 0;
        const int left = (int)(ends[symbol]-bit);
        if (left <= 0) { ++symbol; continue; }
        const int take = min(left,8-written);
        const int shift = lengths[symbol] - (int)(bit-begin) - take;
        const unsigned int mask = (1u << take)-1u;
        result |= ((codes[symbol] >> shift) & mask) << (8-written-take);
        written += take;bit += take;
        if (bit == ends[symbol]) ++symbol;
    }
    output[byte] = (unsigned char)result;
}
'''


class CudaBitPacker:
    """MSB-first variable-length codeword packing, with a CPU reference contract.

    The supplied symbols may have been generated on CPU. This operation only
    claims to offload prefix-sum/bit-position calculation and bit packing.
    """
    def __init__(self):
        from .gpu_compute import cupy_runtime
        self.cp = cupy_runtime()
        self.kernel = self.cp.RawKernel(BITPACK_KERNEL,"pack_audio_bits")
        self.calls = 0

    def __call__(self, codes, lengths, output):
        import numpy as np
        cp = self.cp
        if codes.ndim != 1 or lengths.ndim != 1 or len(codes) != len(lengths):
            raise ValueError("invalid_audio_codeword_shape")
        if output.dtype != np.uint8 or output.ndim != 1 or not output.flags.c_contiguous:
            raise ValueError("invalid_audio_bitstream_buffer")
        if not len(codes):
            return 0
        gpu_codes = cp.asarray(codes,dtype=cp.uint32)
        gpu_lengths = cp.asarray(lengths,dtype=cp.int64)
        if bool(((gpu_lengths<0)|(gpu_lengths>32)).any()):
            raise ValueError("unsupported_audio_codeword_length")
        ends = cp.cumsum(gpu_lengths,dtype=cp.int64)
        bits = int(ends[-1].get())
        count_bytes = (bits+7)//8
        if count_bytes > len(output):
            raise ValueError("audio_bitstream_buffer_too_small")
        if not count_bytes:
            return 0
        packed = cp.empty(count_bytes,dtype=cp.uint8)
        self.kernel(((count_bytes+255)//256,),(256,),
                    (gpu_codes,gpu_lengths.astype(cp.uint8),ends,packed,
                     np.int32(len(codes)),np.int64(bits),np.int32(count_bytes)))
        output[:count_bytes] = cp.asnumpy(packed)
        self.calls += 1
        return bits


ADTS_KERNEL = r'''
extern "C" __global__ void assemble_adts(
    const unsigned char* payload, const long long* payload_ends,
    const long long* packet_ends, unsigned char* output, const long long total,
    const int packets, const int rate_index, const int channels)
{
    const long long byte = (long long)blockDim.x * blockIdx.x + threadIdx.x;
    if (byte >= total) return;
    int low=0, high=packets;
    while (low<high) {
        const int mid=low+(high-low)/2;
        if (packet_ends[mid]<=byte) low=mid+1; else high=mid;
    }
    const long long begin=low?packet_ends[low-1]:0;
    const long long payload_begin=low?payload_ends[low-1]:0;
    const int length=(int)(packet_ends[low]-begin);
    const int pos=(int)(byte-begin);
    unsigned char value;
    switch(pos) {
      case 0: value=255; break;
      case 1: value=241; break;
      case 2: value=64|(rate_index<<2)|(channels>>2); break;
      case 3: value=((channels&3)<<6)|((length>>11)&3); break;
      case 4: value=(length>>3)&255; break;
      case 5: value=((length&7)<<5)|31; break;
      case 6: value=252; break;
      default: value=payload[payload_begin+pos-7];
    }
    output[byte]=value;
}
'''


class CudaAdtsAssembler:
    """AAC-LC ADTS header/length/packet-position calculation on CUDA.

    Accepts already encoded raw-data-block payloads. It is a tested container
    primitive, not an AAC encoder. The caller still owns AAC syntax creation.
    """
    def __init__(self):
        from .gpu_compute import cupy_runtime
        self.cp = cupy_runtime()
        self.kernel = self.cp.RawKernel(ADTS_KERNEL,"assemble_adts")

    def assemble(self, payloads, *, sample_rate_index=3, channels=2):
        import numpy as np
        cp = self.cp
        if channels not in (1,2) or not 0 <= sample_rate_index <= 12:
            raise AudioQualificationError("unsupported_adts_configuration")
        if not payloads:
            return b""
        sizes = cp.asarray([len(payload) for payload in payloads],dtype=cp.int64)
        if bool(((sizes<1)|(sizes>8184)).any()):
            raise AudioQualificationError("invalid_adts_payload_length")
        # Host concatenation is a byte transfer only; all positions and headers
        # in the resulting bitstream are calculated on the device.
        payload = cp.asarray(np.frombuffer(b"".join(payloads),dtype=np.uint8))
        payload_ends = cp.cumsum(sizes,dtype=cp.int64)
        packet_ends = cp.cumsum(sizes+7,dtype=cp.int64)
        total = int(packet_ends[-1].get())
        output = cp.empty(total,dtype=cp.uint8)
        self.kernel(((total+255)//256,),(256,),
                    (payload,payload_ends,packet_ends,output,np.int64(total),
                     np.int32(len(payloads)),np.int32(sample_rate_index),np.int32(channels)))
        return cp.asnumpy(output).tobytes()


_AAC_PATCH_LOCK = threading.RLock()


@contextmanager
def torch_aac_perband_gain_shape():
    """Offline fix for 0.7.2's joint-gain stereo mixed-window shape mismatch.

    The joint optimizer returns one gain per frame; both stereo channels use
    that same gain. Its mixed-window caller expects an explicit channel axis.
    This only broadcasts that GPU tensor; it does not alter quantization.
    """
    from importlib.metadata import version
    if version("torch-aac") != "0.7.2":
        raise AudioQualificationError("unqualified_torch_aac_version")
    import torch_aac.encoder as module
    original = module.find_rate_distortion_sf
    def broadcast(*args, **kwargs):
        gain, factors = original(*args, **kwargs)
        if factors.ndim == 3 and factors.shape[1] == 2 and gain.ndim == 1:
            gain = gain[:, None].expand(-1, 2)
        return gain, factors
    with _AAC_PATCH_LOCK:
        module.find_rate_distortion_sf = broadcast
        try:
            yield
        finally:
            module.find_rate_distortion_sf = original


@contextmanager
def cuda_aac_bitpacker():
    """Offline torch-aac 0.7.2 evaluation adapter, scoped and restored afterward.

    It does not relabel the package's CPU syntax/section/ADTS generation as GPU.
    Call in an isolated encoder process; do not mix with concurrent unpatched
    encodes in the same interpreter. Production use is not enabled here.
    """
    from importlib.metadata import version
    if version("torch-aac") != "0.7.2":
        raise AudioQualificationError("unqualified_torch_aac_version")
    from torch_aac.cpu import _bitwriter_native as native
    from torch_aac.cpu import bitstream
    packer = CudaBitPacker()
    with _AAC_PATCH_LOCK:
        old_pack,old_available,old_writer = native.bitwriter_pack,native.is_available,bitstream.BitWriter
        native.bitwriter_pack = packer
        native.is_available = lambda: True
        bitstream.BitWriter = bitstream._CBitWriter
        try:
            yield packer
        finally:
            native.bitwriter_pack,native.is_available,bitstream.BitWriter = old_pack,old_available,old_writer
