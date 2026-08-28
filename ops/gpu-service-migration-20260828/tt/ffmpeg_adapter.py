#!/data/tt-post-gpu/runtime/bin/python
"""Private FFmpeg 7.1 adapter for the frozen 9425 direct-outro 30fps contract."""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path, PurePosixPath

BINARY_SHA256 = "c34815e5271aecd549e2334a659eebee62de5c86f763d1f15026b11582f1184d"
DIRECT_JOB = re.compile(
    r"(?:/data/tt-post-publisher/direct-outro-work/jobs/[^/]+|"
    r"/data/tt-post-gpu/validation/[A-Za-z0-9._-]+/direct_outro/jobs/[^/]+)"
)
GRAPH_PREFIX = (
    "[0:v]trim=start=0:end={source_end},setpts=PTS-STARTPTS,"
    "scale=w=720:h=1280:force_original_aspect_ratio=decrease:force_divisible_by=2,"
    "pad=720:1280:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps=30,format=yuv420p,"
    "format=yuv420p[source];[source]split=2[source_pre][source_bridge];"
    "[source_pre]trim=start=0:end={start},setpts=PTS-STARTPTS[pre];"
    "[source_bridge]trim=start={start}:end={source_end},setpts=PTS-STARTPTS,"
    "scale=w='trunc((720-214*min(t/{transition}\\,1))/2)*2':"
    "h='trunc((1280-378*min(t/{transition}\\,1))/2)*2':"
    "eval=frame,format=rgba,fade=t=out:st={fade_start}:d=0.250000:alpha=1[foreground];"
    "[1:v]trim=start=0:end={transition},setpts=PTS-STARTPTS[background];"
    "[background][foreground]overlay=x=(W-w)/2:y=(H-h)/2:shortest=1:format=auto,"
    "format=yuv420p[bridge];[1:v]trim=start={transition}:end={outro_end},"
    "setpts=PTS-STARTPTS[post];[pre][bridge][post]concat=n=3:v=1:a=0[outv];"
)
GRAPH_AUDIO_TAIL = (
    "atrim=start=0:end={source_end},asetpts=PTS-STARTPTS,"
    "afade=t=out:st={start}:d={transition}[sa];"
    "[1:a]atrim=start=0:end={outro_end},asetpts=PTS-STARTPTS,adelay={delay}|{delay}[oa];"
    "[sa][oa]amix=inputs=2:duration=longest:normalize=0,alimiter=limit=0.95[outa]"
)
GRAPH_AUDIO = GRAPH_PREFIX + "[0:a]aresample=48000:async=1:first_pts=0,apad," + GRAPH_AUDIO_TAIL
GRAPH_SILENT = GRAPH_PREFIX + "anullsrc=channel_layout=stereo:sample_rate=48000," + GRAPH_AUDIO_TAIL
PREFIX = ["-y", "-nostdin", "-hide_banner", "-loglevel", "error", "-i"]
OUTPUT_OPTIONS = [
    "-map", "[outv]", "-map", "[outa]", "-c:v", "hevc_nvenc",
    "-preset", "p6", "-tune", "hq", "-profile:v", "main", "-rc", "vbr",
    "-b:v", "900k", "-maxrate", "1350k", "-bufsize", "1800k", "-multipass", "fullres",
    "-rc-lookahead", "32", "-spatial-aq", "1", "-temporal-aq", "1", "-aq-strength", "8",
    "-bf", "3", "-b_ref_mode", "middle", "-tag:v", "hvc1", "-pix_fmt", "yuv420p",
    "-fps_mode", "cfr", "-g", "60", "-keyint_min", "60", "-flags", "+cgop",
    "-c:a", "aac", "-profile:a", "aac_low", "-ar", "48000", "-ac", "2", "-b:a", "128k",
    "-map_metadata", "-1", "-map_chapters", "-1", "-movflags", "+faststart",
]


def compile_graph(template: str) -> re.Pattern:
    parts, seen = [], set()
    for part in re.split(r"(\{[a-z_]+\})", template):
        if not part.startswith("{"):
            parts.append(re.escape(part))
            continue
        name = part[1:-1]
        if name in seen:
            parts.append("(?P=" + name + ")")
        else:
            pattern = r"[0-9]+" if name == "delay" else r"[0-9]+\.[0-9]{6}"
            parts.append("(?P<" + name + ">" + pattern + ")")
            seen.add(name)
    return re.compile("".join(parts))


GRAPHS = (compile_graph(GRAPH_AUDIO), compile_graph(GRAPH_SILENT))


def is_direct_job_path(value: str) -> bool:
    path = PurePosixPath(value)
    return ".." not in path.parts and bool(DIRECT_JOB.fullmatch(str(path.parent)))


def adapt_arguments(arguments: list[str]) -> list[str]:
    # Return the original vector unchanged for all other lanes, normalizing
    # steps, metadata queries and commands. Never join arguments through a shell.
    if not any(is_direct_job_path(value) for value in arguments):
        return arguments
    if "-filter_complex" not in arguments:
        return arguments
    # Frozen single-output layout from build_phone_match_command, 9425b39.
    # Additional outputs, aliases, duplicate options, explicit -r or altered
    # encoder parameters are ambiguous here and must fail closed.
    if len(arguments) != 12 + len(OUTPUT_OPTIONS):
        raise ValueError("ambiguous direct-outro argument count")
    if (arguments[:6] != PREFIX or arguments[7] != "-i" or arguments[9] != "-filter_complex"
            or arguments[11:-1] != OUTPUT_OPTIONS):
        raise ValueError("direct-outro command differs from the frozen contract")
    source, outro, output = (PurePosixPath(arguments[i]) for i in (6, 8, -1))
    if (not all(is_direct_job_path(str(path)) for path in (source, outro, output))
            or not source.parent == outro.parent == output.parent
            or [source.name, outro.name, output.name] != ["source.mp4", "outro-normalized.mp4", "prepared.mp4"]):
        raise ValueError("direct-outro paths or output count differ")
    match = next((match for pattern in GRAPHS if (match := pattern.fullmatch(arguments[10]))), None)
    if match is None:
        raise ValueError("direct-outro filter graph differs from the frozen contract")
    values = {name: float(value) for name, value in match.groupdict().items()}
    if (values["source_end"] < 1 or not 0.1 <= values["transition"] <= 0.9
            or values["outro_end"] <= values["transition"]
            or abs(values["start"] - (values["source_end"] - values["transition"])) > 0.000002
            or abs(values["fade_start"] - max(0, values["transition"] - 0.25)) > 0.000002
            or abs(values["delay"] - round(values["start"] * 1000)) > 1):
        raise ValueError("direct-outro graph timing is inconsistent")
    return [*arguments[:-1], "-r", "30", arguments[-1]]


def main() -> int:
    try:
        arguments = adapt_arguments(sys.argv[1:])
    except ValueError:
        print("TT private FFmpeg: unsupported direct-outro command; refusing to infer output options", file=sys.stderr)
        return 78
    binary = Path(__file__).resolve().with_name("ffmpeg.bin")
    os.execv(str(binary), [str(binary), *arguments])
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
