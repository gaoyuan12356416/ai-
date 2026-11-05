"""Isolated native rendering process; no credentials, upload or job mutation.

The parent keeps the frozen recipe and source identity. This process owns CUDA
contexts and temporary spools, and publishes output only after successful mux.
"""
from __future__ import annotations

import argparse
from contextlib import nullcontext
import json
import os
from pathlib import Path
import sys
import tempfile
import time


def enabled():
    return os.environ.get('RANDOM_GPU_NATIVE', '0') == '1'


def command(source, output, info, recipe, assets, *, cache_root=None,
            encoder_options=None, audio_bitrate=192000, ffmpeg='/usr/bin/ffmpeg'):
    """Build the FB H264/CQ21 contract without credentials or source URLs."""
    spec = dict(source=str(source), info=info, recipe=recipe,
                assets={key:str(value) for key,value in assets.items()},
                cache_root=cache_root or os.environ.get('RANDOM_GPU_ASSET_CACHE_ROOT'),
                encoder_options=encoder_options,audio_bitrate=audio_bitrate,ffmpeg=str(ffmpeg))
    if not spec['cache_root']:
        raise ValueError('native_pipeline_requires_verified_asset_cache')
    python = os.environ.get('RANDOM_GPU_PYTHON', sys.executable)
    return [python, '-m', 'features.random_gpu.native_pipeline', '--spec',
            json.dumps(spec, separators=(',', ':')), str(output)]


def progress_path(output):
    return Path(str(output)+'.progress')


def render(spec, output, work_dir=None):
    from .gpu_audio import prepare_audio_track
    from .gpu_mux import native_video_track
    from .cpu_mux import CpuMP4
    from .native_resize import CachedWeightRenderer
    from .native_video import CachedAssets, encode_video
    output = Path(output)
    progress = progress_path(output)
    started = time.monotonic()
    last_progress = 0
    counter = 0
    stage = 'initializing'
    timings = {}
    stage_wall, stage_cpu = started, time.process_time()

    def advance(name):
        nonlocal stage, stage_wall, stage_cpu
        now, cpu = time.monotonic(), time.process_time()
        timings[stage] = dict(wall_seconds=now-stage_wall, cpu_seconds=cpu-stage_cpu)
        stage, stage_wall, stage_cpu = name, now, cpu

    def check():
        # Called only by work actually advancing (decoded packets/frames,
        # audio chunks or written samples), never by a background heartbeat.
        nonlocal last_progress, counter
        counter += 1
        now = time.monotonic()
        if now-last_progress >= .5:
            progress.write_text(f'{stage}:{counter}', encoding='ascii')
            last_progress = now

    context = (nullcontext(str(work_dir)) if work_dir is not None else
               tempfile.TemporaryDirectory(prefix='.native-', dir=output.parent))
    with context as directory:
        scratch = Path(directory)
        check()
        layers = CachedAssets(spec['assets'], spec['cache_root'], check)
        try:
            advance('video')
            record = encode_video(spec['source'], scratch/'video.h264',
                spec['info'], spec['recipe'], layers, check,
                encoder_options=spec.get('encoder_options'), renderer_class=CachedWeightRenderer)
        finally:
            layers.close()
        advance('video_packets')
        video = native_video_track(scratch/'video.h264', record,
                                   scratch/'video.avcc', check=check, mux=CpuMP4())
        advance('audio')
        audio = prepare_audio_track(spec['source'], scratch,
            spec['info']['duration'], check=check,
            timeline_origin_seconds=(spec['info'].get('format') or {}).get('start_time', '0'),
            audio_bitrate=spec.get('audio_bitrate',192000),ffmpeg=spec.get('ffmpeg','/usr/bin/ffmpeg'))
        advance('mux')
        result = CpuMP4().mux([video, audio.track], scratch/'finished.mp4',
                              spec['info']['duration'], check=check)
        os.replace(scratch/'finished.mp4', output)
        advance('complete')
        result.update(video={k:v for k,v in record.items() if k!='packets'},
                      audio=audio.report, timings=timings,
                      wall_seconds=time.monotonic()-started)
        # Bounded diagnostic metadata contains neither source URL nor credentials.
        output.with_suffix(output.suffix+'.native.json').write_text(
            json.dumps(result, sort_keys=True), encoding='utf-8')
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--spec', required=True)
    parser.add_argument('--work-dir', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    try:
        result = render(json.loads(args.spec), args.output, args.work_dir)
        print(json.dumps(result, sort_keys=True), flush=True)
    finally:
        progress_path(args.output).unlink(missing_ok=True)


if __name__ == '__main__':
    main()
