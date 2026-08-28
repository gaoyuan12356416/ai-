#!/usr/bin/env python3
"""Exercise real local FFmpeg with fake download/storage/TikTok transports."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import traceback
from dataclasses import replace
from pathlib import Path
from unittest import mock

from tt_migration import BASE, atomic_json, digest, read_env, verify_source


def run(source: Path, output_root: Path, only_lane: str = "both") -> dict:
    verify_source(source)
    output_root = output_root.resolve()
    if not output_root.is_relative_to(BASE / "validation"):
        raise ValueError("offline outputs must stay in the isolated validation directory")
    output_root.mkdir(parents=True, exist_ok=False, mode=0o700)
    sys.path.insert(0, str(source))
    from features.tt_gpu import worker
    from scripts.test_tt_gpu_worker import FakeObjectStore, FakeTikTokAPI

    base = read_env(BASE / "config/base.env")
    secret = read_env(BASE / "config/secrets.env")
    direct = read_env(BASE / "config/direct-outro.env")
    source_file = output_root / "synthetic-source.mp4"
    subprocess.run([
        base["TT_POST_GPU_FFMPEG_BIN"], "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc2=size=720x1280:rate=30:duration=12",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=12",
        "-c:v", "h264_nvenc", "-b:v", "900k", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-ac", "2", "-b:a", "128k", "-shortest",
        "-movflags", "+faststart", "-y", str(source_file),
    ], check=True, timeout=120, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    source_sha = digest(source_file)
    source_size = source_file.stat().st_size
    results = []
    for mode, overrides in [("random_overlay", {}), ("direct_outro", direct)]:
        if only_lane not in {"both", mode}:
            continue
        env = {**base, **secret, **overrides}
        if env.get("TT_POST_LIVE_ENABLED") != "0" or env.get("TT_POST_MANUAL_CANARY_ENABLED") != "0":
            raise ValueError("offline verification refuses open production/canary gates")
        with mock.patch.dict(os.environ, env, clear=True):
            config = worker.WorkerConfig.from_env()
        config = replace(
            config, work_root=output_root / mode,
            prepare_total_timeout=300, transcode_timeout=180,
        )
        api = FakeTikTokAPI()
        saved = output_root / (mode + ".mp4")

        class RetainingFakeObjectStore(FakeObjectStore):
            def upload(self, key, path, sha256_value, size, deadline=None):
                shutil.copyfile(path, saved)
                return super().upload(key, path, sha256_value, size, deadline)

            @staticmethod
            def url(key):
                # An inert string only: no real object is uploaded or fetched.
                return config.cos_domain.rstrip("/") + "/" + key

        store = RetainingFakeObjectStore()

        def fake_download(_url, destination, expected_sha, expected_size, _config, _deadline=None):
            if expected_sha != source_sha or expected_size != source_size:
                raise ValueError("synthetic source identity mismatch")
            shutil.copyfile(source_file, destination)
            return {"sha256": source_sha, "size": source_size}

        commands = []

        def local_runner(command, **kwargs):
            record = {"argv": [str(value) for value in command]}
            commands.append(record)
            completed = subprocess.run(command, **kwargs)
            record["returncode"] = completed.returncode
            if Path(str(command[0])).name == "ffprobe" and completed.returncode == 0:
                record["probe"] = json.loads(completed.stdout)
            # Retain only local synthetic intermediate videos before the worker
            # removes its temporary job directory. No URLs or auth are captured.
            candidate = Path(str(command[-1]))
            if candidate.is_file() and candidate.suffix == ".mp4" and candidate.is_relative_to(output_root):
                retained = output_root / (mode + "-stage-" + str(len(commands)) + ".mp4")
                shutil.copyfile(candidate, retained)
                record["retained_file"] = str(retained)
                record["retained_sha256"] = digest(retained)
            atomic_json(output_root / (mode + "-commands.json"), {"commands": commands})
            if completed.returncode:
                (output_root / (mode + "-ffmpeg-error.txt")).write_text(
                    str(completed.stderr or ""), encoding="utf-8"
                )
            return completed

        processor = worker.TTPostGPUProcessor(
            config, runner=local_runner, downloader=fake_download,
            object_store=store, tiktok_api=api,
        )
        request = {
            "job_id": "ttmigration-" + mode + "-synthetic-0001",
            "content_id": "TEST",
            "source_url": "https://" + config.allowed_source_hosts[0] + "/migration-synthetic.mp4",
            "source_sha256": source_sha, "source_size": source_size,
            "expected_profile": config.profile,
            "source_trim_tail_seconds": config.default_source_trim_tail_seconds,
        }
        # Guard against accidental future code changes escaping the fake adapters.
        try:
            with mock.patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden")):
                result = processor.prepare(request)
                reused = processor.prepare(request)
        except Exception as exc:
            atomic_json(output_root / "failure.json", {
                "lane": mode, "error_type": type(exc).__name__,
                "error_code": str(getattr(exc, "code", "offline_validation_failed")),
                "message": str(exc)[:500], "traceback": traceback.format_exc(),
                "tiktok_calls": len(api.creator_calls) + len(api.init_calls) + len(api.status_calls),
                "real_uploads": 0,
            })
            raise
        if api.creator_calls or api.init_calls or api.status_calls:
            raise AssertionError("offline test attempted a TikTok operation")
        if list(processor.publish_root.glob("*.json")):
            raise AssertionError("offline test created a publish ledger")
        if result["status"] != "ready" or not reused["reused"] or len(store.upload_calls) != 1:
            raise AssertionError("prepare/reuse contract failed")
        if digest(saved) != result["output_sha256"]:
            raise AssertionError("retained output differs from the prepared manifest")
        results.append({
            "mode": mode, "profile": result["profile"], "output_sha256": result["output_sha256"],
            "output_size": result["output_size"], "probe": result["probe"],
            "reused": reused["reused"], "ffmpeg_and_probe_commands": len(commands),
            "tiktok_calls": 0, "real_uploads": 0, "publish_ledgers": 0,
        })
        atomic_json(output_root / "verification.partial.json", {"lanes": results})
    summary = {"ok": True, "source_sha256": source_sha, "lanes": results}
    atomic_json(output_root / "verification.json", summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=BASE / "current")
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--lane", choices=("both", "random_overlay", "direct_outro"), default="both")
    args = parser.parse_args()
    try:
        print(json.dumps(run(args.source, args.output_root, args.lane), sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error_type": type(exc).__name__,
                          "error_code": str(getattr(exc, "code", "offline_validation_failed"))}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
