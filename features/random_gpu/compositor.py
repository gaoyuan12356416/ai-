"""Fused OpenCL composition, adapted from drama compositor df1c495.

The caller owns assets, recipe identity, encoding, audio, deadlines and storage.
This module replaces only the video filter graph. There is no automatic CPU
fallback: a failed GPU render must not silently recreate the original overload.
"""
from __future__ import annotations

import hashlib
import math
import os
from pathlib import Path
import subprocess
import tempfile

BACKEND = "opencl_fused_contain_v1"
LEGACY = "cpu_legacy"
KERNEL = Path(__file__).with_name("random_overlay.cl")


def backend(value=LEGACY):
    if value not in (LEGACY, BACKEND):
        raise ValueError("unsupported random compositor backend")
    return value


def kernel_source(recipe):
    limits = {"rotation_millidegrees": (-2000, 2000), "scale_bp": (9800, 10200),
              "tint_opacity_bp": (100, 1000)}
    for key, (low, high) in limits.items():
        value = recipe.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError("invalid random compositor transform")
    scale = recipe["scale_bp"] / 10000
    definitions = {
        "SCENE_WIDTH": "720", "SCENE_HEIGHT": "1280",
        "SCENE_MAIN_WIDTH": str(math.floor(720 * scale / 2) * 2),
        "SCENE_MAIN_HEIGHT": str(math.floor(1280 * scale / 2) * 2),
        "SCENE_ROTATION_RADIANS": "%.12ff" % (recipe["rotation_millidegrees"] * math.pi / 180000),
        "SCENE_TINT_OPACITY": "%.8ff" % (recipe["tint_opacity_bp"] / 10000),
    }
    return "".join("#define %s %s\n" % item for item in definitions.items()) + KERNEL.read_text(encoding="utf-8")


def _escape(path):
    # These paths are worker-generated, never request strings or shell code.
    return str(path).replace("\\", "/").replace(":", "\\:").replace("'", "'\\''")


def fuse_command(command, recipe, output):
    """Keep the existing delivery arguments and replace the CPU composition."""
    graph_index = command.index("-filter_complex")
    prefix = list(command[:graph_index])
    source_indices = [index for index, value in enumerate(prefix) if value == "-i"]
    if len(source_indices) not in (5, 6):
        raise ValueError("random compositor requires source plus four assets")
    code = kernel_source(recipe)
    target = Path(output).parent / ("compositor-" + hashlib.sha256(code.encode()).hexdigest()[:20] + ".cl")
    # Job locking is owned by the caller. Atomic replacement also keeps offline
    # concurrent benchmark invocations from observing a partially written kernel.
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent,
                                     prefix=".compositor-", delete=False) as handle:
        handle.write(code)
        temporary = Path(handle.name)
    try:
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    inputs = []
    input_number = 0
    for value in prefix[1:]:
        if value == "-i":
            inputs.extend(["-threads", "2"])
            if input_number in (1, 4):
                inputs.extend(["-framerate", "30"])
            input_number += 1
        inputs.append(value)
    # All five inputs use the same zero-based 30 fps clock before framesync.
    # Do not run a second fps filter after program_opencl: its output inherits
    # source PTS and must not have its first frame dropped/reindexed.
    graph = []
    labels = ("source", "border", "opacity", "corners", "tint")
    for index, label in enumerate(labels):
        # FFmpeg's input timestamp offset already rebases video and audio.
        # PTS-STARTPTS here would reset on mid-stream resolution changes.
        graph.append("[%d:v]fps=30:start_time=0,settb=1/30,format=rgba,hwupload[%s]" % (index, label))
    graph.append("[source][border][opacity][corners][tint]program_opencl=inputs=5:"
                 "size=720x1280:source='%s':kernel=compose_random_overlay_v2:"
                 "shortest=1:eof_action=endall,hwdownload,format=rgba,setsar=1,"
                 "format=yuv420p[v]" % _escape(target))
    return [prefix[0], "-filter_complex_threads", "2", "-filter_threads", "2",
            "-init_hw_device", "opencl=ocl:0.0", "-filter_hw_device", "ocl", *inputs,
            "-filter_complex", ";".join(graph), *command[graph_index + 2:]]


def preflight(ffmpeg, encoder, work_root):
    """Exercise the deployed OpenCL kernel and selected NVENC encoder at startup."""
    if encoder not in ("h264_nvenc", "hevc_nvenc"):
        raise ValueError("GPU compositor requires NVENC")
    Path(work_root).mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="compositor-preflight-", dir=work_root) as tmp:
        path = Path(tmp) / "preflight.cl"
        path.write_text(kernel_source({"rotation_millidegrees": 0, "scale_bp": 10000,
                                       "tint_opacity_bp": 100}), encoding="utf-8")
        graph = ("format=rgba,hwupload,split=5[a][b][c][d][e];"
                 "[a][b][c][d][e]program_opencl=inputs=5:size=720x1280:"
                 "source='%s':kernel=compose_random_overlay_v2,hwdownload,"
                 "format=rgba,format=yuv420p[v]" % _escape(path))
        subprocess.run([ffmpeg, "-nostdin", "-v", "error", "-filter_complex_threads", "2",
                        "-filter_threads", "2", "-init_hw_device", "opencl=ocl:0.0",
                        "-filter_hw_device", "ocl", "-f", "lavfi", "-i", "color=s=64x64:r=30:d=0.1",
                        "-filter_complex", graph, "-map", "[v]", "-c:v", encoder,
                        "-frames:v", "2", "-f", "null", "-"],
                       check=True, capture_output=True, timeout=45)
