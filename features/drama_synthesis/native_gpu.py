"""Tracked NVDEC -> CUDA -> NVENC chunk renderer; no business or upload APIs.

The low-level 1.0.2 NVIDIA demuxer does not preserve PTS in this runtime and its
seek path crashes. PyAV therefore supplies demuxed Annex-B packets and exact
timestamps; NVDEC is used only for pixel decoding. Runtime preflight, immutable
asset receipts and the parent compositor's checkpoints gate production use.
"""
from __future__ import annotations

from collections import deque
from fractions import Fraction
import json
import math
import os
from pathlib import Path
import subprocess
import time

from .asset_cache import verified_entry
from .composition import CANVAS_FPS, CANVAS_HEIGHT, CANVAS_WIDTH
from .gpu_compositor import compile_opencl_kernel, kernel_template_path
from .h264_headers import packet_dimensions

KERNEL = Path(__file__).with_name("cuda") / "random_overlay_v1.cu"
IDENTITY = "nvdec-cuda-nvenc-rgba-demux-v2"


def fps_slot(seconds):
    """FFmpeg AV_ROUND_NEAR_INF, using exact rational timestamps."""
    value = Fraction(seconds) * CANVAS_FPS
    if value < 0:
        return -fps_slot(-Fraction(seconds))
    return (2 * value.numerator + value.denominator) // (2 * value.denominator)


def container_origin(container, stream):
    # FFmpeg's input -ss uses the container clock. Video can start a few
    # milliseconds after audio; subtracting video.start_time changes the
    # 25 -> 30 fps duplicate pattern and breaks chunk boundary alignment.
    if container.start_time is not None:
        return Fraction(container.start_time, 1000000)
    return (stream.start_time or 0) * stream.time_base


class Timeline:
    """Select the last input frame whose rounded 30 fps PTS is due."""
    def __init__(self, frames):
        self.frames = iter(frames)
        self.current = next(self.frames)
        self.origin = fps_slot(self.current[0])
        self.following = next(self.frames, None)

    def at(self, index):
        slot = index + self.origin
        while self.following is not None and fps_slot(self.following[0]) <= slot:
            self.current = self.following
            self.following = next(self.frames, None)
        if self.following is None and Fraction(slot, CANVAS_FPS) - self.current[0] > 1:
            raise RuntimeError("native_source_ended_early")
        return self.current[1]


def _asset_frames(av, np, entry, phase, image):
    container = av.open(entry["path"])
    try:
        stream = container.streams.video[0]
        if (stream.codec_context.name != "rawvideo" or stream.codec_context.format.name != "rgba"
                or (stream.width, stream.height) != (CANVAS_WIDTH, CANVAS_HEIGHT)):
            raise RuntimeError("native_asset_format_invalid")
        time_base = stream.time_base
        phase = Fraction(str(phase))
        container.seek(int(phase / time_base), stream=stream, backward=True, any_frame=False)
        # Keep the absolute clock through trim -> fps -> setpts. Subtracting
        # phase before rounding moves duplicate frames at fractional phases.
        offset = Fraction(0)
        while True:
            last_end = None
            yielded = False
            for packet in container.demux(stream):
                if packet.size == 0:
                    continue
                if packet.size != CANVAS_WIDTH * CANVAS_HEIGHT * 4 or packet.pts is None:
                    raise RuntimeError("native_asset_packet_invalid")
                timestamp = packet.pts * time_base
                duration = packet.duration * time_base if packet.duration else Fraction(1, CANVAS_FPS)
                last_end = timestamp + duration
                if timestamp + offset < phase:
                    continue
                array = np.frombuffer(packet, dtype=np.uint8).reshape(CANVAS_HEIGHT, CANVAS_WIDTH, 4)
                yield timestamp + offset, array
                yielded = True
                if image:
                    # Infinite repeated reference: the consumer uploads it once.
                    index = 1
                    while True:
                        yield Fraction(index, CANVAS_FPS), array
                        index += 1
            if not yielded or last_end is None or last_end <= 0:
                raise RuntimeError("native_asset_empty")
            offset += last_end
            container.seek(0, stream=stream, backward=True, any_frame=False)
    finally:
        container.close()


def _main_frames(av, nvc, cp, source, start, convert):
    container = av.open(str(source))
    try:
        stream = container.streams.video[0]
        context = stream.codec_context
        # The production normalization stage guarantees H.264 without B frames.
        # This permits an exact FIFO mapping of input PTS to NVDEC output even
        # though DecodedFrame.timestamp is zero in NVIDIA's 1.0.2 wheel.
        if context.name != "h264" or context.has_b_frames or context.format.name != "yuv420p":
            raise RuntimeError("native_source_codec_unsupported")
        if int(context.colorspace) != 1 or int(context.color_range) != 1:
            raise RuntimeError("native_source_color_unsupported")
        start = Fraction(str(start))
        origin = container_origin(container, stream)
        time_base = stream.time_base
        container.seek(int((start + origin) / time_base), stream=stream, backward=True, any_frame=False)
        bsf = av.BitStreamFilterContext("h264_mp4toannexb", stream)
        cp.cuda.runtime.free(0)
        context_pointer = cp.cuda.driver.ctxGetCurrent()
        def new_decoder():
            return nvc.CreateDecoder(gpuid=0, codec=nvc.cudaVideoCodec.H264, usedevicememory=True,
                                     cudacontext=context_pointer, cudastream=cp.cuda.get_current_stream().ptr)
        decoder = new_decoder()
        dimensions = None
        pending = deque()

        def decoded(packet):
            for frame in decoder.Decode(packet):
                if not pending:
                    raise RuntimeError("native_decoder_timestamp_mismatch")
                timestamp = pending.popleft() - origin - start
                if timestamp < 0:
                    continue
                if frame.format != nvc.Pixel_Format.NV12:
                    raise RuntimeError("native_decoder_format_unsupported")
                array = cp.from_dlpack(frame)
                height, width = int(array.shape[0] * 2 // 3), int(array.shape[1])
                if width % 2 or height % 2 or not 16 <= width <= 4096 or not 16 <= height <= 4096:
                    raise RuntimeError("native_decoder_dimensions_invalid")
                rgba = cp.empty((height, width, 4), dtype=cp.uint8)
                convert(((width + 15)//16, (height + 15)//16), (16, 16),
                        (array, rgba, width, height, int(array.strides[0])))
                # The next Decode may recycle the NVIDIA-owned surface.
                cp.cuda.get_current_stream().synchronize()
                yield timestamp, rgba

        for packet in container.demux(stream):
            if packet.size == 0:
                continue
            for filtered in bsf.filter(packet):
                if filtered.pts is None:
                    raise RuntimeError("native_source_timestamp_missing")
                new_dimensions = packet_dimensions(bytes(filtered))
                if new_dimensions is not None:
                    if dimensions is not None and new_dimensions != dimensions:
                        # PyAV's BSF consumes the original packet; use the
                        # returned packet's flags, not the now-empty input.
                        if not filtered.is_keyframe:
                            raise RuntimeError("native_resolution_change_requires_keyframe")
                        empty = nvc.PacketData()
                        empty.bsl, empty.bsl_data = 0, 0
                        yield from decoded(empty)
                        if pending:
                            raise RuntimeError("native_boundary_frames_missing")
                        decoder = new_decoder()
                    dimensions = new_dimensions
                pending.append(filtered.pts * time_base)
                data = nvc.PacketData()
                data.bsl, data.bsl_data = filtered.size, filtered.buffer_ptr
                data.pts = int(pending[-1] * 1000000)
                yield from decoded(data)
        empty = nvc.PacketData()
        empty.bsl, empty.bsl_data = 0, 0
        yield from decoded(empty)
        if pending:
            raise RuntimeError("native_decoder_frames_missing")
    finally:
        container.close()


def render(plan, progress=None):
    import av
    import cupy as cp
    import numpy as np
    import PyNvVideoCodec as nvc
    import torch

    started = time.monotonic()
    torch.cuda.set_device(0)
    cp.cuda.Device(0).use()
    torch.set_num_threads(1)
    prefix = compile_opencl_kernel(plan["spec"])["source"].split("// FFmpeg program_opencl", 1)[0]
    kernel_path = kernel_template_path(plan["spec"], cuda=True)
    module = cp.RawModule(code=prefix + kernel_path.read_text(encoding="utf-8"), options=("--std=c++11",))
    convert = module.get_function("nv12_rgba")
    compose = module.get_function("compose")
    to_nv12 = module.get_function("rgba_nv12")
    chunk = plan["chunk"]
    start, count = float(chunk["start_seconds"]), int(chunk["frame_count"])
    if not math.isfinite(start) or start < 0 or not 1 <= count <= 9000:
        raise RuntimeError("native_chunk_invalid")
    source = Timeline(_main_frames(av, nvc, cp, plan["source"], start, convert))
    assets = []
    for category in ("border", "opacity_video", "corners", "tint"):
        item = plan["assets"][category]
        entry = verified_entry(plan["asset_cache_root"], item["sha256"], verify_digest=False)
        duration = float(plan["asset_durations"].get(category) or 0)
        phase = start % duration if duration > 0 else 0
        assets.append(Timeline(_asset_frames(av, np, entry, phase, item["media_type"] == "image/png")))
    uploaded = [None] * 4
    array_ids = [None] * 4
    rgba = cp.empty((CANVAS_HEIGHT, CANVAS_WIDTH, 4), dtype=cp.uint8)
    nv12 = cp.empty((CANVAS_HEIGHT * 3//2, CANVAS_WIDTH), dtype=cp.uint8)
    tensor = torch.from_dlpack(nv12)
    encoder = nvc.CreateEncoder(CANVAS_WIDTH, CANVAS_HEIGHT, "NV12", False,
                               codec="h264", preset="P3", profile="high", fps=str(CANVAS_FPS),
                               # SDK bf=1 means IPP (FFmpeg's -bf 0); bf=0 is all-I.
                               gop="60", idrperiod="60", bf="1", rc="constqp", constqp="19")
    output = Path(plan["output"])
    raw = output.with_name(output.name + ".native.h264")
    if output.exists() or output.is_symlink():
        raise RuntimeError("native_output_already_exists")
    grid = ((CANVAS_WIDTH+15)//16, (CANVAS_HEIGHT+15)//16)
    owns_raw = False
    try:
        with raw.open("xb") as handle:
            owns_raw = True
            for index in range(count):
                frame = source.at(index)
                for asset_index, asset in enumerate(assets):
                    array = asset.at(index)
                    if array is not array_ids[asset_index]:
                        uploaded[asset_index] = cp.asarray(array)
                        array_ids[asset_index] = array
                compose(grid, (16, 16), (frame, int(frame.shape[1]), int(frame.shape[0]),
                                        *uploaded, rgba))
                to_nv12(grid, (16, 16), (rgba, nv12))
                cp.cuda.get_current_stream().synchronize()
                handle.write(bytes(encoder.Encode(tensor)))
                if progress is not None and (index % 30 == 0 or index == count-1):
                    progress(index+1, time.monotonic()-started)
            handle.write(bytes(encoder.EndEncode()))
            handle.flush()
            os.fsync(handle.fileno())
        subprocess.run([
            plan["ffmpeg"], "-nostdin", "-hide_banner", "-loglevel", "error",
            "-r", str(CANVAS_FPS), "-f", "h264", "-i", str(raw),
            "-map", "0:v:0", "-an", "-c:v", "copy", "-color_range", "tv",
            "-colorspace", "bt709", "-color_trc", "bt709", "-color_primaries", "bt709",
            "-chroma_sample_location", "left", "-bsf:v",
            "h264_metadata=video_full_range_flag=0:colour_primaries=1:"
            "transfer_characteristics=1:matrix_coefficients=1:chroma_sample_loc_type=0",
            "-movflags", "+faststart", str(output),
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=180)
    finally:
        if owns_raw and raw.is_file() and not raw.is_symlink():
            raw.unlink()


def main(argv=None):
    import argparse
    import resource
    import ctypes
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    ctypes.CDLL(None).prctl(4, 0, 0, 0, 0)  # No crash dumps of production media memory.
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True)
    args = parser.parse_args(argv)
    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))

    def progress(frame, elapsed):
        print("frame=%d\nout_time_us=%d\nfps=%.3f\nspeed=%.3fx\nprogress=continue" % (
            frame, round(frame / CANVAS_FPS * 1000000), frame / max(.001, elapsed),
            frame / CANVAS_FPS / max(.001, elapsed)), flush=True)

    render(plan, progress=progress)
    print("progress=end", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
