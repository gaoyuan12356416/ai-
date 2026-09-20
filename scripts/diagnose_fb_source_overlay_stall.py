"""Bounded Linux-only, local FB old/new recipe diagnostic; no worker API/upload.

Run with the FB service's Python and environment, e.g. python -m
scripts.diagnose_fb_source_overlay_stall --source /private/source.mp4
--asset-root /data/assets/catalog --output-root /private/unique-diagnostic
--timeout 180 --original-kernel /private/compositor.cl
--original-command-nul /private/ffmpeg.cmdline
--alternate-ffmpeg /data/tt-post-gpu/ffmpeg/ffmpeg.bin

Original files are an optional pair. Only their data is parsed, never executed.
An optional third case changes only the FFmpeg binary for the new recipe;
the FB H264 profile, input/filter settings and original FFprobe remain in use.
Use --disable-asset-cache to compare the same assets' original media against
cached NUT looping. This changes only this diagnostic process's environment.
The output directory must be new. Each render has its own process group and a
1..180 second deadline, followed by at most 10 seconds of TERM/KILL cleanup.
Ctrl-C/SIGTERM also cleans up that test group. Partial media/logs are retained.
"""
import argparse
from dataclasses import replace
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.fb_gpu.prepare_worker import PROFILE, WorkerConfig, build_command
from features.fb_gpu.random_overlay import (
    CATEGORIES, derive_recipe, load_asset_set, selected_asset_paths, sha256_file,
    validate_recipe,
)
from features.random_gpu.compositor import BACKEND, kernel_source, validate_source_overlay
from features.random_gpu.asset_cache import ENV as ASSET_CACHE_ENV


def save(path, value):
    temporary = path.with_suffix(path.suffix + ".new")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n"); handle.flush(); os.fsync(handle.fileno())
    temporary.chmod(0o600)
    os.replace(temporary, path)


def macros(text):
    return dict(re.findall(r"^#define\s+(SCENE_[A-Z_]+)\s+([-+0-9.eE]+)f?\s*$", text, re.M))


def restore_recipe(kernel_path, command_path, assets):
    code = kernel_path.read_text(encoding="utf-8")
    defines = macros(code)
    if defines.get("SCENE_WIDTH") != "720" or defines.get("SCENE_HEIGHT") != "1280":
        raise ValueError("original kernel must use the 720x1280 FB scene")
    width, height = int(defines["SCENE_MAIN_WIDTH"]), int(defines["SCENE_MAIN_HEIGHT"])
    candidates = [bp for bp in range(9800, 10201)
                  if (math.floor(720 * (bp / 10000) / 2) * 2,
                      math.floor(1280 * (bp / 10000) / 2) * 2) == (width, height)]
    if not candidates:
        raise ValueError("original kernel scale is outside the FB contract")
    rotation = round(float(defines["SCENE_ROTATION_RADIANS"]) * 180000 / math.pi)
    tint = round(float(defines["SCENE_TINT_OPACITY"]) * 10000)
    args = command_path.read_bytes().rstrip(b"\0").decode("utf-8").split("\0")
    inputs = [args[index + 1] for index, item in enumerate(args[:-1]) if item == "-i"]
    if len(inputs) not in (5, 6) or "-filter_complex" not in args:
        raise ValueError("original command must contain source plus four FB asset inputs")
    chosen, selections = {}, {}
    for category, value in zip(CATEGORIES, inputs[1:5]):
        path = Path(value)
        source_sha = path.stem if path.suffix == ".nut" and re.fullmatch(r"[0-9a-f]{64}", path.stem) else None
        matches = [row for row in assets["categories"][category]
                   if row["sha256"] == source_sha] if source_sha else [
                       row for row in assets["categories"][category] if row["name"] == path.name]
        if not matches or len({row["sha256"] for row in matches}) != 1:
            raise ValueError("original " + category + " asset is absent or ambiguous in verified catalog")
        row = matches[0]
        chosen[category] = {key: row[key] for key in ("media_type", "name", "sha256", "size")}
        selections[category] = {"original_input": value, "source_sha256": row["sha256"],
                                "matching_names": [item["name"] for item in matches]}
    recipe = {"version": 1, "asset_set_sha256": assets["manifest_sha256"], "assets": chosen,
              "rotation_millidegrees": rotation, "scale_bp": candidates[0], "tint_opacity_bp": tint}
    validate_recipe(recipe, assets)
    # Prove the reconstructed values reproduce every original base definition.
    regenerated = macros(kernel_source(recipe))
    for key in ("SCENE_MAIN_WIDTH", "SCENE_MAIN_HEIGHT", "SCENE_ROTATION_RADIANS", "SCENE_TINT_OPACITY"):
        if not math.isclose(float(defines[key]), float(regenerated[key]), rel_tol=0, abs_tol=1e-12):
            raise ValueError("original kernel transform is not exactly recoverable: " + key)
    return recipe, {"mode": "original-assets-and-equivalent-transforms", "selections": selections,
                    "original_source_path": inputs[0], "original_kernel_sha256": sha256_file(kernel_path)[0],
                    "original_command_sha256": sha256_file(command_path)[0],
                    "scale_bp_candidates": candidates, "exact_original_scale_bp_known": len(candidates) == 1,
                    "note": "Rounded kernel dimensions can encode multiple equivalent original scale_bp values; current renderer is used."}


def group_exists(pid):
    try: os.killpg(pid, 0); return True
    except ProcessLookupError: return False


def stop_group(process):
    events = []
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if not group_exists(process.pid): break
        try: os.killpg(process.pid, sig)
        except ProcessLookupError: break  # Group exited between the check and signal.
        events.append(sig.name)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            process.poll()  # Reap the leader; never signal a production PID.
            if not group_exists(process.pid): break
            time.sleep(0.05)
    process.poll()
    if group_exists(process.pid):
        raise RuntimeError("diagnostic process group still exists; inspect recorded PGID before continuing")
    return events


def probe(config, path, timeout):
    process = subprocess.Popen([config.ffprobe, "-v", "error", "-protocol_whitelist", "file,pipe", "-show_streams", "-show_format", "-of", "json", str(path)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=min(timeout, 30))
        if process.returncode:
            return {"ok": False, "returncode": process.returncode, "error": stderr.decode(errors="replace")[-4000:]}
        raw = json.loads(stdout)
        fields = ("codec_name", "profile", "width", "height", "pix_fmt", "avg_frame_rate", "r_frame_rate",
                  "time_base", "start_time", "duration", "nb_frames", "channels", "sample_rate")
        streams = raw.get("streams", [])
        return {"ok": True, "format": {key: raw.get("format", {}).get(key) for key in ("duration", "start_time", "size")},
                **{kind: [{key: row.get(key) for key in fields if key in row} for row in streams
                          if row.get("codec_type") == kind] for kind in ("video", "audio")}}
    except (subprocess.TimeoutExpired, ValueError) as exc:
        return {"ok": False, "error": type(exc).__name__}
    finally:
        stop_group(process)
        for handle in (process.stdout, process.stderr): handle.close()


def last_progress(path):
    keys = {"frame", "fps", "out_time", "out_time_us", "out_time_ms", "speed", "total_size", "progress"}
    result = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        key, separator, value = line.partition("=")
        if separator and key in keys: result[key] = value
    return result


def render_case(config, source, root, recipe, assets, info, timeout, expected_source_sha):
    root.mkdir(mode=0o700)
    output, progress = root / "output.mp4", root / "progress.log"
    result = {"case": root.name, "recipe": recipe, "source_sha256": expected_source_sha,
              "ffmpeg": config.ffmpeg, "ffprobe": config.ffprobe, "timeout_seconds": timeout}
    process, interrupted = None, None
    started = time.monotonic()
    try:
        if sha256_file(source)[0] != expected_source_sha: raise ValueError("source changed before diagnostic render")
        command = build_command(config, source, output, info, recipe, selected_asset_paths(recipe, assets))
        for index in reversed([index for index, value in enumerate(command) if value == "-i"]):
            command[index:index] = ["-protocol_whitelist", "file,pipe"]
        command[1:1] = ["-progress", "pipe:1", "-stats_period", "1", "-nostats"]
        command[command.index("-loglevel") + 1] = "warning"
        (root / "command.nul").write_bytes(b"\0".join(os.fsencode(part) for part in command) + b"\0")
        save(root / "result.json", {**result, "outcome": "starting"})
        with progress.open("wb") as stdout, (root / "stderr.log").open("wb") as stderr:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                                       start_new_session=True)
            result.update(pid=process.pid, pgid=process.pid)
            save(root / "result.json", {**result, "outcome": "running"})
            try:
                result["returncode"] = process.wait(timeout=timeout)
                result["outcome"] = "completed" if process.returncode == 0 else "failed"
            except subprocess.TimeoutExpired:
                result["outcome"] = "timeout"
    except BaseException as exc:
        result.update(outcome="interrupted" if isinstance(exc, KeyboardInterrupt) else "failed", error=type(exc).__name__ + ": " + str(exc))
        if isinstance(exc, (KeyboardInterrupt, SystemExit)): interrupted = exc
    finally:
        if process is not None:
            try: result["cleanup_signals"] = stop_group(process)
            except RuntimeError as exc: result["cleanup_error"] = str(exc)
            result["returncode"] = process.returncode
        result["wall_seconds"] = round(time.monotonic() - started, 3)
        result["last_progress"] = last_progress(progress) if progress.exists() else {}
        if output.exists() and not result.get("cleanup_error"):
            result["output_sha256"], result["output_size"] = sha256_file(output)
            result["probe"] = probe(config, output, timeout)
            if result["probe"].get("ok"):
                result["audio_streams"] = len(result["probe"]["audio"])
                result["duration_delta_seconds"] = float(result["probe"]["format"]["duration"]) - info["duration"]
        save(root / "result.json", result)
    if interrupted: raise interrupted
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "asset-root", "output-root"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--asset-manifest-sha256", help="Defaults to the configured FB manifest SHA")
    parser.add_argument("--original-kernel", type=Path)
    parser.add_argument("--original-command-nul", type=Path)
    parser.add_argument("--alternate-ffmpeg", type=Path, help="Optional third new-recipe case; preserves FB H264 output settings")
    parser.add_argument("--disable-asset-cache", action="store_true", help="Read original catalog media instead of cached NUT inputs for all cases")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--opacity-bp", type=int, default=350)
    parser.add_argument("--scale-bp", type=int, default=13000)
    args = parser.parse_args(argv)
    if os.name != "posix": parser.error("Linux/POSIX process groups are required")
    if not 1 <= args.timeout <= 180: parser.error("timeout must be 1..180 seconds")
    if bool(args.original_kernel) != bool(args.original_command_nul): parser.error("original kernel and NUL command must be supplied together")
    source, root = args.source.resolve(strict=True), args.output_root
    if not source.is_file() or not root.is_absolute() or root.exists() or root.is_symlink():
        parser.error("source must be a local file; output-root must be a new absolute directory")
    overlay = {"version": 1, "opacity_bp": args.opacity_bp, "scale_bp": args.scale_bp}
    validate_source_overlay({"source_overlay": overlay})
    config = WorkerConfig.from_env()
    if args.original_kernel and config.compositor_backend != BACKEND:
        parser.error("original OpenCL recovery requires the configured OpenCL backend")
    for value in (config.ffmpeg, config.ffprobe, *([str(args.alternate_ffmpeg)] if args.alternate_ffmpeg else [])):
        if not Path(value).is_absolute() or not Path(value).is_file(): parser.error("local absolute FFmpeg/FFprobe binaries required")
    if args.disable_asset_cache:
        os.environ.pop(ASSET_CACHE_ENV, None)
    os.umask(0o077)
    root.mkdir(mode=0o700, parents=True)
    report = {"ok": False, "profile": PROFILE, "backend": config.compositor_backend, "cases": [],
              "no_network_upload_or_publication": True, "source": str(source),
              "asset_cache_root": os.environ.get(ASSET_CACHE_ENV) or None,
              "asset_cache_disabled_by_flag": args.disable_asset_cache}
    def interrupted(_signum, _frame): raise KeyboardInterrupt("diagnostic interrupted")
    previous = {sig: signal.signal(sig, interrupted) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        assets = load_asset_set(args.asset_root, args.asset_manifest_sha256 or config.asset_manifest_sha256)
        digest, size = sha256_file(source)
        report.update(source_sha256=digest, source_size=size, asset_set_sha256=assets["manifest_sha256"])
        if args.original_kernel:
            legacy, report["reconstruction"] = restore_recipe(args.original_kernel, args.original_command_nul, assets)
        else:
            legacy = derive_recipe(job_id="offline-" + digest, content_id=digest, profile=PROFILE,
                                   source_url_sha256=digest, asset_set=assets)
            legacy.pop("source_overlay")
            report["reconstruction"] = {"mode": "deterministic-source-sha-selected-assets"}
        report["source_probe"] = probe(config, source, args.timeout)
        if not report["source_probe"]["ok"] or len(report["source_probe"]["video"]) != 1:
            raise ValueError("source probe failed or did not contain one video stream")
        info = {"has_audio": bool(report["source_probe"]["audio"]),
                "duration": float(report["source_probe"]["format"]["duration"])}
        if not math.isfinite(info["duration"]) or info["duration"] <= 0: raise ValueError("source duration invalid")
        save(root / "report.json", report)
        new_recipe = {**legacy, "source_overlay": overlay}
        cases = [("old", legacy, config), ("new", new_recipe, config)]
        if args.alternate_ffmpeg:
            cases.append(("new-alternate-ffmpeg", new_recipe, replace(config, ffmpeg=str(args.alternate_ffmpeg))))
        for name, recipe, case_config in cases:
            report["cases"].append(render_case(case_config, source, root / name, recipe, assets, info, args.timeout, digest))
            save(root / "report.json", report)
            if report["cases"][-1].get("cleanup_error"):
                raise RuntimeError(report["cases"][-1]["cleanup_error"])
        report["ok"] = all(row["outcome"] == "completed" and row.get("probe", {}).get("ok") for row in report["cases"])
    except BaseException as exc:
        report["error"] = type(exc).__name__ + ": " + str(exc)
    finally:
        save(root / "report.json", report)
        for sig, handler in previous.items(): signal.signal(sig, handler)
    print(json.dumps({"ok": report["ok"], "report": str(root / "report.json"),
                      "cases": [{"case": row["case"], "outcome": row["outcome"], "last_progress": row["last_progress"]} for row in report["cases"]]}))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
