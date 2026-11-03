"""Qualified NVDEC + OpenCL fit/CSC + existing compositor + NVENC RGB input.

The delivery profile, frozen recipe, source timestamps and audio arguments stay
with the caller. Unsupported source formats retain the existing hybrid path;
an error after selecting this implementation never silently retries another
renderer. Already-qualified artifacts are recovered by the existing journal.
"""
from __future__ import annotations
import hashlib
import os
from pathlib import Path
import tempfile

ENV = 'RANDOM_GPU_PIPELINE_V2'
IMPLEMENTATION = 'nvdec-opencl-lanczos-nvenc-rgb-v1'
KERNEL = Path(__file__).with_name('gpu_fit.cl')


def qualification(info, environ=None):
    env = os.environ if environ is None else environ
    if env.get(ENV, '0') != '1': return 'disabled'
    video = (info or {}).get('video') or {}
    if video.get('codec_name') not in {'h264', 'hevc'}: return 'unsupported_decoder'
    if video.get('pix_fmt') not in {'yuv420p', 'yuvj420p'}: return 'unsupported_pixel_format'
    if video.get('field_order') not in {None, '', 'unknown', 'progressive'}: return 'interlaced_source'
    if video.get('color_space') not in {None, '', 'unknown', 'bt709', 'bt470bg', 'smpte170m'}: return 'unsupported_color_matrix'
    if video.get('color_transfer') in {'smpte2084', 'arib-std-b67'}: return 'hdr_source'
    if video.get('chroma_location') not in {None, '', 'unspecified', 'left', 'center', 'topleft'}: return 'unsupported_chroma_location'
    try:
        w, h = int(video['width']), int(video['height'])
    except (KeyError, ValueError, TypeError): return 'missing_geometry'
    if not (16 <= w <= 4096 and 16 <= h <= 4096 and w % 2 == h % 2 == 0): return 'unsupported_geometry'
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
    inputs, number = [], 0
    for position, value in enumerate(prefix[1:], start=1):
        if value == '-i':
            if number == 0:
                # CUVID resizes to the declared source canvas on the device,
                # keeping filter geometry stable on changing-resolution input.
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
