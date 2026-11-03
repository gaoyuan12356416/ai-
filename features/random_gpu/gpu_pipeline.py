"""Qualified NVDEC + OpenCL fit/CSC + existing compositor + NVENC RGB input.

The delivery profile, frozen recipe, source timestamps and audio arguments stay
with the caller. Unsupported source formats retain the existing hybrid path;
an error after selecting this implementation never silently retries another
renderer. Already-qualified artifacts are recovered by the existing journal.
"""
from __future__ import annotations
import hashlib
import json
import os
import subprocess
from pathlib import Path
import tempfile
from fractions import Fraction
from functools import lru_cache

ENV = 'RANDOM_GPU_PIPELINE_V2'
IMPLEMENTATION = 'nvdec-opencl-lanczos-nvenc-rgb-v1'
KERNEL = Path(__file__).with_name('gpu_fit.cl')


def qualification(info, environ=None):
    env = os.environ if environ is None else environ
    if env.get(ENV, '0') != '1': return 'disabled'
    video = (info or {}).get('video') or {}
    if video.get('codec_name') not in {'h264', 'hevc'}: return 'unsupported_decoder'
    if video.get('pix_fmt') not in {'yuv420p', 'yuvj420p'}: return 'unsupported_pixel_format'
    # These cases need distinct range/timing proofs before admission. Preserve
    # their established renderer rather than changing frozen delivery behavior.
    if video.get('color_range') == 'pc' or video.get('pix_fmt') == 'yuvj420p': return 'full_range_source'
    try:
        rate = float(Fraction(video.get('r_frame_rate') or '30'))
        average = float(Fraction(video.get('avg_frame_rate') or str(rate)))
        if rate < 20 or rate > 60 or abs(rate-average) > rate * .01: return 'variable_or_low_frame_rate'
        if abs(float(video.get('start_time') or 0)) > .1: return 'delayed_video_start'
    except (ValueError, TypeError, ZeroDivisionError): return 'unqualified_timestamps'
    if video.get('sample_aspect_ratio') not in {None, '', 'N/A', '0:1', '1:1'}: return 'non_square_pixels'
    if video.get('field_order') not in {None, '', 'unknown', 'progressive'}: return 'interlaced_source'
    if video.get('color_space') not in {None, '', 'unknown', 'bt709', 'bt470bg', 'smpte170m'}: return 'unsupported_color_matrix'
    if video.get('color_transfer') in {'smpte2084', 'arib-std-b67'}: return 'hdr_source'
    if video.get('chroma_location') not in {None, '', 'unspecified', 'left', 'center', 'topleft'}: return 'unsupported_chroma_location'
    try:
        w, h = int(video['width']), int(video['height'])
    except (KeyError, ValueError, TypeError): return 'missing_geometry'
    if not (16 <= w <= 4096 and 16 <= h <= 4096 and w % 2 == h % 2 == 0): return 'unsupported_geometry'
    return 'qualified'


@lru_cache(maxsize=32)
def _keyframe_formats(probe, source, size, modified_ns):
    # Resolution changes carry new decoder sequence headers at keyframes.
    # Inspect only keyframes; do not software-decode the whole video. CUVID can
    # hang on a changing decode surface even when -resize is fixed, so detect
    # this before selecting the accelerated implementation.
    result = subprocess.run([probe, '-v', 'error', '-threads', '2', '-skip_frame', 'nokey',
        '-select_streams', 'v:0', '-show_frames', '-show_entries',
        'frame=width,height,pix_fmt,color_space,color_range', '-of', 'json', source],
        capture_output=True, check=True, timeout=45)
    frames = json.loads(result.stdout).get('frames', [])
    return {(frame.get('width'), frame.get('height'), frame.get('pix_fmt'),
             frame.get('color_space'), frame.get('color_range')) for frame in frames}


def source_qualification(command, info):
    video = info['video']
    try:
        source = Path(command[command.index('-i') + 1])
        stat = source.stat()
        formats = _keyframe_formats(str(Path(command[0]).with_name('ffprobe')),
                                   str(source), stat.st_size, stat.st_mtime_ns)
        if not formats: return 'source_keyframes_missing'
        for width, height, pixels, matrix, color_range in formats:
            if (width, height) != (video['width'], video['height']): return 'changing_source_geometry'
            if pixels != 'yuv420p' or color_range == 'pc': return 'changing_source_pixel_format'
            if matrix not in {None, '', 'unknown', video.get('color_space')}: return 'changing_source_color'
    except (OSError, ValueError, KeyError, subprocess.SubprocessError): return 'source_scan_unavailable'
    return 'qualified'


def kernel_source(recipe, video):
    from .compositor import kernel_source as composition_source
    chroma = video.get('chroma_location')
    defs = {
        'GPU_BT709': '1' if video.get('color_space') == 'bt709' else '0',
        'GPU_FULL_RANGE': '1' if video.get('color_range') == 'pc' or video.get('pix_fmt') == 'yuvj420p' else '0',
        'GPU_CHROMA_X': '0.5f' if chroma == 'center' else '0.0f',
        'GPU_CHROMA_Y': '0.0f' if chroma == 'topleft' else '0.5f',
    }
    return (''.join('#define %s %s\n' % row for row in defs.items())
            + KERNEL.read_text(encoding='utf-8') + '\n' + composition_source(recipe))


def fuse_command(command, recipe, output, info):
    from .asset_cache import replace_asset_inputs
    from .compositor import _escape
    if qualification(info) != 'qualified': raise ValueError('gpu_pipeline_source_not_qualified')
    if source_qualification(command, info) != 'qualified': raise ValueError('gpu_pipeline_geometry_not_qualified')
    graph_index = command.index('-filter_complex')
    prefix = replace_asset_inputs(list(command[:graph_index]))
    indices = [i for i, value in enumerate(prefix) if value == '-i']
    if len(indices) not in (5, 6): raise ValueError('gpu_pipeline_input_contract')
    video = info['video']
    code = kernel_source(recipe, video)
    target = Path(output).parent / ('gpu-fit-' + hashlib.sha256(code.encode()).hexdigest()[:20] + '.cl')
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=target.parent,
                                     prefix='.gpu-fit-', delete=False) as handle:
        handle.write(code); temporary = Path(handle.name)
    try: os.replace(temporary, target)
    finally: temporary.unlink(missing_ok=True)
    # Local render provenance only. Frozen job/profile and artifact recovery
    # identity remain owned by their existing journals.
    record = {'implementation': IMPLEMENTATION,
              'kernel_sha256': hashlib.sha256(code.encode()).hexdigest(),
              'recipe_sha256': hashlib.sha256(json.dumps(recipe, sort_keys=True).encode()).hexdigest(),
              'source': {key: video.get(key) for key in ('codec_name', 'pix_fmt', 'width', 'height',
                         'color_space', 'color_range', 'color_transfer', 'chroma_location')}}
    Path(output).with_suffix('.renderer.json').write_text(json.dumps(record, sort_keys=True), encoding='utf-8')
    inputs, number = [], 0
    for position, value in enumerate(prefix[1:], start=1):
        if value == '-i':
            if number == 0:
                # Fixed qualified source canvas; dynamic-resolution streams
                # were excluded before selecting this renderer.
                inputs.extend(['-reinit_filter', '0', '-hwaccel', 'cuda',
                    '-hwaccel_output_format', 'cuda', '-c:v', video['codec_name'] + '_cuvid',
                    '-resize', '%sx%s' % (video['width'], video['height'])])
            inputs.extend(['-threads', '2'])
            if number in (1, 4) and not prefix[position + 1].endswith('.nut'):
                inputs.extend(['-framerate', '30'])
            number += 1
        inputs.append(value)
    program = "program_opencl=inputs=4:size=720x1280:source='%s':kernel=" % _escape(target)
    graph = [
        '[0:v]hwdownload,format=nv12,format=yuv420p,fps=30:start_time=0,extractplanes=y+u+v[y][u][vplane]',
        '[y]hwupload,split=2[yback][ymain]',
        '[u]hwupload,split=2[uback][umain]',
        '[vplane]hwupload,split=2[vback][vmain]',
    ]
    for index, label in enumerate(('border', 'opacity', 'corners', 'tint'), start=1):
        suffix = ',split=3[tint][clockback][clockmain]' if index == 4 else '[%s]' % label
        graph.append('[%s:v]fps=30:start_time=0,settb=1/30,format=rgba,hwupload%s' % (index, suffix))
    graph.extend([
        '[clockback][yback][uback][vback]' + program + 'fit_yuv_cover:shortest=1:eof_action=endall[source]',
        '[clockmain][ymain][umain][vmain]' + program + 'fit_yuv_contain:shortest=1:eof_action=endall[main]',
        "[source][main][border][opacity][corners][tint]program_opencl=inputs=6:size=720x1280:source='%s':kernel=compose_random_overlay_v2:shortest=1:eof_action=endall,hwdownload,format=rgba,setsar=1[v]" % _escape(target),
    ])
    tail = list(command[graph_index + 2:])
    if tail[tail.index('-c:v') + 1] not in {'h264_nvenc', 'hevc_nvenc'}:
        raise ValueError('gpu_pipeline_requires_nvenc')
    tail[tail.index('-pix_fmt') + 1] = 'rgba'
    tail[-1:-1] = ['-rgb_mode', 'yuv420']
    return [prefix[0], '-filter_complex_threads', '2', '-filter_threads', '2',
            '-init_hw_device', 'opencl=ocl:0.0', '-filter_hw_device', 'ocl',
            *inputs, '-filter_complex', ';'.join(graph), *tail]


def preflight(ffmpeg, encoder, directory):
    """Actual decode/fit/composition/RGB encode admission, never publication."""
    if os.environ.get(ENV, '0') != '1': return
    from features.fb_gpu.random_overlay import load_asset_set, CATEGORIES
    root = os.environ.get('X_POST_RANDOM_TEMPLATE_ASSET_ROOT') or os.environ.get('FB_PAGE_GPU_RANDOM_OVERLAY_ROOT') or os.environ.get('TT_POST_GPU_RANDOM_OVERLAY_ROOT')
    sha = os.environ.get('X_POST_RANDOM_TEMPLATE_MANIFEST_SHA256') or os.environ.get('FB_PAGE_GPU_RANDOM_OVERLAY_MANIFEST_SHA256') or os.environ.get('TT_POST_GPU_RANDOM_OVERLAY_MANIFEST_SHA256')
    assets = load_asset_set(root, sha)
    with tempfile.TemporaryDirectory(prefix='gpu-source-preflight-', dir=directory) as temp:
        source, output = Path(temp)/'source.mp4', Path(temp)/'output.mp4'
        subprocess.run([ffmpeg, '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
                        'testsrc2=s=192x320:r=30:d=0.2', '-c:v', encoder,
                        '-pix_fmt', 'yuv420p', str(source)], check=True, capture_output=True, timeout=30)
        command = [ffmpeg, '-nostdin', '-v', 'error', '-i', str(source)]
        for category in CATEGORIES:
            path = assets['categories'][category][0]['path']
            command += ['-i', str(path)]
        command += ['-filter_complex', 'replaced', '-map', '[v]', '-an', '-c:v', encoder,
                    '-pix_fmt', 'yuv420p', '-frames:v', '2', str(output)]
        info = {'video': {'codec_name': 'hevc' if encoder == 'hevc_nvenc' else 'h264',
                         'width': 192, 'height': 320, 'pix_fmt': 'yuv420p'}}
        recipe = {'rotation_millidegrees': 0, 'scale_bp': 10000, 'tint_opacity_bp': 100}
        subprocess.run(fuse_command(command, recipe, output, info),
                       check=True, capture_output=True, timeout=45)
        subprocess.run([ffmpeg, '-nostdin', '-v', 'error', '-i', str(output),
                        '-f', 'null', '-'], check=True, capture_output=True, timeout=30)
