#!/usr/bin/env python3
"""Validate any node locally, optionally test COS, enroll, then run.

This never calls a social-platform publishing API or changes a service unit.
Credentials come from private environment files, never command-line values.
"""
import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.pipeline_dispatch.worker import configuration, hardware_inventory, enroll_node, run_node
from features.pipeline_dispatch.executors import NodeExecutors, cpu_thread_command


def load_environment(path):
    """Read KEY=value without shell evaluation, interpolation, or echoing values."""
    file = Path(path)
    if not file.is_absolute() or file.is_symlink() or not file.is_file():
        raise ValueError("environment_file_invalid")
    if os.name != "nt" and file.stat().st_mode & 0o077:
        raise ValueError("environment_file_permissions")
    for raw in file.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"): continue
        if line.startswith("export "): line = line[7:]
        key, sep, value = line.partition("=")
        if not sep or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise ValueError("environment_file_syntax")
        # systemd-style single/double quoted values are supported. No eval.
        if value.startswith(("'", '"')):
            words = shlex.split(value, posix=True)
            if len(words) != 1: raise ValueError("environment_file_syntax")
            value = words[0]
        os.environ[key] = value


def encode_sample(ffmpeg, ffprobe, encoder, directory, *, cpu):
    target = directory / ("sample-" + hashlib.sha256((ffmpeg + encoder).encode()).hexdigest()[:12] + ".mp4")
    command = [ffmpeg, "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=128x128:rate=10:duration=1", "-an", "-c:v", encoder, "-pix_fmt", "yuv420p", "-t", "1", str(target)]
    if cpu: command = cpu_thread_command(command)
    subprocess.run(command, capture_output=True, check=True, timeout=60)
    result = subprocess.run([ffprobe, "-v", "error", "-show_streams", "-of", "json", str(target)], capture_output=True, check=True, text=True, timeout=30)
    codecs = [row.get("codec_name") for row in json.loads(result.stdout).get("streams", [])]
    if ("hevc" if encoder == "hevc_nvenc" else "h264") not in codecs:
        raise ValueError("encode_probe_mismatch")
    return target


def cos_roundtrip(config, source, node_id):
    """Private, uniquely named diagnostic object; never a platform submission."""
    from qcloud_cos import CosConfig, CosS3Client
    client = CosS3Client(CosConfig(Region=config.cos_region, SecretId=config.cos_secret_id, SecretKey=config.cos_secret_key, Scheme="https", Timeout=30, KeepAlive=False), retry=0)
    prefix = str(config.cos_prefix).rstrip("/")
    key = prefix + "/dispatch-diagnostics/" + hashlib.sha256(node_id.encode()).hexdigest()[:16] + "/" + uuid.uuid4().hex + ".mp4"
    body = source.read_bytes(); digest = hashlib.sha256(body).hexdigest()
    client.put_object(Bucket=config.cos_bucket, Key=key, Body=body, ACL="private", ContentType="video/mp4", Metadata={"x-cos-meta-sha256": digest})
    head = {str(k).lower(): str(v) for k, v in client.head_object(Bucket=config.cos_bucket, Key=key).items()}
    if int(head.get("content-length", 0)) != len(body) or head.get("x-cos-meta-sha256") != digest:
        raise ValueError("cos_diagnostic_identity_mismatch")
    cleaned = False
    try:
        client.delete_object(Bucket=config.cos_bucket, Key=key)
        cleaned = True
    except Exception:
        pass  # Delete permission is not required by the production renderer.
    return {"read_write_verified": True, "diagnostic_removed": cleaned, **({} if cleaned else {"diagnostic_key": key})}


def validate_node(config, *, encode=False, cos_check=False):
    inventory = hardware_inventory()
    enabled = {x.strip() for x in os.environ.get("PIPELINE_EXECUTORS", "").split(",") if x.strip()}
    if not enabled or enabled - set(NodeExecutors.PREPARE + NodeExecutors.PUBLISH):
        raise ValueError("executor_configuration_invalid")
    for module in ("requests", "PIL"):
        importlib.import_module(module)
    from features.tt_gpu.credentials import _aesgcm
    _aesgcm(bytes(32))
    executor = NodeExecutors(config)
    processors, profiles, checks = {}, set(), []
    for kind in sorted(enabled & set(NodeExecutors.PREPARE)):
        if kind == "youtube_prepare":
            binary = os.environ.get("YOUTUBE_AUTO_CODEX_BIN", "/usr/bin/codex")
            if not (Path(binary).is_file() or shutil.which(binary)):
                raise ValueError("youtube_codex_binary_missing")
            subprocess.run([binary, "--version"], capture_output=True, check=True, timeout=30)
            source = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
            if not (source / "auth.json").is_file():
                raise ValueError("youtube_codex_auth_missing")
            checks.append({"executor": kind, "local_generator": True})
            continue
        name = "x_repair" if kind == "x_preflight" else kind
        processor = executor._processor(name)
        processors[name] = processor
        profiles.add(processor.config.profile)
        checks.append({"executor": kind, "profile": processor.config.profile, "assets_verified": bool(getattr(processor, "assets", None) or getattr(processor.config, "random_overlay_assets", None))})
    declared = {x.strip() for x in os.environ.get("PIPELINE_RENDER_PROFILES", "").split(",") if x.strip()}
    if declared - profiles:
        raise ValueError("advertised_profile_not_supported")
    os.environ["PIPELINE_RENDER_PROFILES"] = ",".join(sorted(profiles))
    if encode or cos_check:
        with tempfile.TemporaryDirectory(prefix="node-diagnostic-", dir=config["state_root"]) as temp:
            directory, samples = Path(temp), {}
            for name, processor in processors.items():
                cfg = processor.config
                binary = str(getattr(cfg, "ffmpeg_bin", getattr(cfg, "ffmpeg", "")))
                probe = str(getattr(cfg, "ffprobe_bin", getattr(cfg, "ffprobe", "")))
                encoder = "libx264" if config["backend"] == "cpu" else getattr(cfg, "video_encoder", "h264_nvenc")
                sample_key = (binary, encoder)
                if sample_key not in samples:
                    samples[sample_key] = encode_sample(binary, probe, encoder, directory, cpu=config["backend"] == "cpu")
                if config["backend"] == "gpu" and (getattr(cfg, "compositor_backend", "") == "opencl_fused_contain_v1" or name == "x_random"):
                    from features.random_gpu.compositor import preflight
                    preflight(binary, encoder, directory)
                checks.append({"executor": name, "encode_verified": True, "encoder": encoder, "cpu_threads": 4 if config["backend"] == "cpu" else None})
                if cos_check:
                    checks.append({"executor": name, "cos": cos_roundtrip(cfg, samples[sample_key], config["node_id"])})
    return {"ok": True, "node_id": config["node_id"], "inventory": inventory, "capabilities": executor.capabilities(), "checks": checks}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", action="append", default=[], help="absolute private env file, applied in order")
    parser.add_argument("--encode", action="store_true", help="encode a one-second local diagnostic; no platform write")
    parser.add_argument("--cos-check", action="store_true", help="write/head/delete only a new private diagnostic COS object")
    parser.add_argument("--enroll", metavar="PRIVATE_CONFIG_PATH")
    parser.add_argument("--run", action="store_true", help="start workers only after all checks pass")
    args = parser.parse_args()
    try:
        for path in args.env: load_environment(path)
        config_path = os.environ.get("PIPELINE_NODE_CONFIG")
        if config_path and not Path(config_path).exists() and (args.enroll or not args.run):
            # Diagnostics precede first enrollment. Running workers still require
            # the durable credential file; no placeholder credentials are made.
            os.environ.pop("PIPELINE_NODE_CONFIG")
        config = configuration()
        Path(config["state_root"]).mkdir(parents=True, exist_ok=True, mode=0o700)
        report = validate_node(config, encode=args.encode, cos_check=args.cos_check)
        if args.enroll:
            if not args.encode or not args.cos_check:
                raise ValueError("enrollment_requires_encode_and_cos_checks")
            report["enrollment"] = enroll_node(args.enroll)
            os.environ["PIPELINE_NODE_CONFIG"] = args.enroll
        print(json.dumps(report, ensure_ascii=False), flush=True)
        if args.run: run_node(configuration())
    except Exception as exc:
        # No exception text, environment contents or upstream bodies are printed.
        code = str(getattr(exc, "code", "node_validation_failed"))
        if isinstance(exc, ValueError) and re.fullmatch(r"[a-z][a-z0-9_]{1,80}", str(exc)):
            code = str(exc)
        print(json.dumps({"ok": False, "code": code}), flush=True)
        return 1
    return 0


if __name__ == "__main__": raise SystemExit(main())
