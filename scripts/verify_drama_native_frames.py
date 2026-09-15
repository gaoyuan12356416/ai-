#!/usr/bin/env python3
"""Private GPU QA: independent software decode and FFmpeg animation clocks.

Only this diagnostic reads sampled GPU pixels back to the CPU. The production
renderer keeps its decoded and composed video frames on the device.
"""
from fractions import Fraction
import argparse
import hashlib
import itertools
import json
from pathlib import Path
import resource
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from features.drama_synthesis.asset_cache import verified_entry
from features.drama_synthesis.native_gpu import (
    KERNEL, Timeline, _asset_frames, _main_frames, container_origin,
)


def main():
    import av
    import cupy as cp
    import numpy as np
    import PyNvVideoCodec as nvc
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--start", type=float, default=0)
    parser.add_argument("--asset-plan")
    parser.add_argument("--report", required=True)
    args = parser.parse_args()
    report = Path(args.report)
    allowed = Path('/data/drama-synthesis-gpu/experiments/frame-pipeline-20260915')
    if report.exists() or report.is_symlink() or not report.resolve().is_relative_to(allowed):
        raise SystemExit('qa_report_path_invalid')
    cp.cuda.Device(0).use()
    # The pixel decoder kernel has no dependency on recipe geometry constants.
    code = KERNEL.read_text(encoding='utf-8').split('extern "C" __global__ void compose', 1)[0]
    module = cp.RawModule(code='#define SCENE_WIDTH 720\n#define SCENE_HEIGHT 1280\n'+code,
                          options=('--std=c++11',))
    kernel = module.get_function('nv12_rgba')
    decoded_surface = {}
    def convert(grid, block, arguments):
        decoded_surface['nv12'] = arguments[0]
        kernel(grid, block, arguments)
    start = Fraction(str(args.start))
    errors, samples, frame_count = [], [], 0
    with av.open(args.source) as software:
        stream = software.streams.video[0]
        stream.thread_type = 'SLICE'
        stream.codec_context.thread_count = 1
        origin = container_origin(software, stream)
        software.seek(int((start+origin)/stream.time_base), stream=stream, backward=True, any_frame=False)
        reference = ((f.pts*f.time_base-origin-start, f) for f in software.decode(stream))
        reference = ((pts, f) for pts, f in reference if pts >= 0)
        device = _main_frames(av, nvc, cp, args.source, args.start, convert)
        previous_shape = None
        for i, pair in enumerate(itertools.zip_longest(device, reference)):
            gpu, cpu = pair
            if gpu is None or cpu is None:
                errors.append('frame_count_mismatch')
                break
            pts, pixels = gpu
            expected_pts, frame = cpu
            shape = (frame.height, frame.width, 4)
            if pts != expected_pts or tuple(pixels.shape) != shape:
                errors.append('timestamp_or_dimension_mismatch:%d' % i)
                break
            if i < 3 or i % 251 == 0 or shape != previous_shape:
                # Compare decoder output before any color converter. PyAV
                # 12.3's RGB reformat does not honor the BT.709 source matrix
                # in this runtime; it is not a valid color reference.
                reference_nv12 = frame.reformat(format='nv12').to_ndarray()
                actual_nv12 = decoded_surface['nv12'].get()
                nv12_diff = np.abs(actual_nv12.astype(np.int16)-reference_nv12.astype(np.int16))
                expected = frame.reformat(format='rgba', src_colorspace='ITU709').to_ndarray()
                actual = pixels.get()
                diff = np.abs(actual.astype(np.int16)-expected.astype(np.int16))
                mae = float(diff.mean())
                samples.append({'frame':i, 'seconds':float(pts), 'shape':list(shape[:2]),
                                'nv12_max_error':int(nv12_diff.max()),
                                'pyav_rgb_diagnostic_mae':round(mae, 5)})
                if nv12_diff.max() != 0:
                    errors.append('decoded_pixel_difference:%d' % i)
            previous_shape = shape
            frame_count += 1
    assets = []
    if args.asset_plan:
        plan = json.loads(Path(args.asset_plan).read_text())
        for category, item in plan['assets'].items():
            if item['media_type'] == 'image/png':
                continue
            entry = verified_entry(plan['asset_cache_root'], item['sha256'])
            duration = float(plan['asset_durations'][category])
            phase = 30 % duration
            timeline = Timeline(_asset_frames(av, np, entry, phase, False))
            actual = [hashlib.md5(timeline.at(i)).hexdigest() for i in range(90)]
            cmd = [plan['ffmpeg'], '-v', 'error', '-stream_loop', '-1', '-i', entry['path'],
                   '-vf', 'trim=start=%.6f,fps=30,setpts=PTS-STARTPTS' % phase,
                   '-frames:v', '90', '-an', '-fps_mode', 'passthrough', '-f', 'framemd5', '-']
            output = subprocess.check_output(cmd, text=True, timeout=120)
            expected = [line.rsplit(',',1)[-1].strip() for line in output.splitlines()
                        if line and not line.startswith('#')]
            equal = actual == expected
            assets.append({'category':category, 'phase':phase, 'frames':len(expected),
                           'all_pixel_hashes_match_ffmpeg':equal})
            if not equal:
                errors.append('asset_clock_mismatch:'+category)
    result = {'ok':not errors, 'errors':errors, 'source':str(Path(args.source).name),
              'start':args.start, 'decoded_frames':frame_count, 'samples':samples, 'assets':assets}
    report.write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps(result))
    return 0 if not errors else 1


if __name__ == '__main__':
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    raise SystemExit(main())
