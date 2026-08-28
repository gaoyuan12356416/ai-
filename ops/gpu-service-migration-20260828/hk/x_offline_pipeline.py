#!/usr/bin/env python3
"""Run the frozen X processor once in an isolated, offline acceptance unit."""
import argparse
import hashlib
import importlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time

from deploy import require_commit

RUN_ID = "gpu-service-migration-20260828T1502"
X_ROOT = pathlib.Path("/data/x-post-media-repair")
SOURCE_RELEASE_SHA = "170e3b1325b71a72fcd6de913982ce92bb77fa40"
SOURCE_ROOT = X_ROOT / "releases" / SOURCE_RELEASE_SHA
EVIDENCE_BASE = pathlib.Path("/data/migrations") / RUN_ID / "x-offline-pipeline"
PYTHON_ROOT = X_ROOT / "runtime/python"
FFMPEG = X_ROOT / "runtime/bin/ffmpeg"
FFPROBE = X_ROOT / "runtime/bin/ffprobe"
SOURCE_URL = "https://offline.invalid/synthetic.mp4"
FAKE_BUCKET = "offline-test-only"
SOURCE_HASHES = {
    "features/x_posts/media_repair.py": "09dfeba82598a3cce0dd483cb5b091434deb9f5f814d37099b118d0666310f3c",
    "features/x_posts/service.py": "e63a4e04b622b95b0a61489e90984b03317183726ac8420ceb9f5e0e427356e5",
    "features/x_posts/__init__.py": "b8c8436310bd08e710f62b0d6ce0623b6c282a562a5fe0112f4f69dd10cefbc3",
    "features/__init__.py": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
}


def digest(path):
    value = hashlib.sha256()
    with pathlib.Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def within(path, root):
    root = pathlib.Path(root).resolve()
    path = pathlib.Path(path).resolve()
    if root not in path.parents:
        raise ValueError("offline path must be strictly inside the acceptance directory")
    return path


def write_once(path, value):
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    with os.fdopen(os.open(str(path), flags, 0o600), "w", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def verify_namespace(evidence):
    if os.readlink("/proc/self/ns/net") == os.readlink("/proc/1/ns/net"):
        raise ValueError("the offline unit must have a private network namespace")
    for path in (X_ROOT / "state", SOURCE_ROOT, "/etc", "/usr"):
        if not os.statvfs(str(path)).f_flag & getattr(os, "ST_RDONLY", 1):
            raise ValueError("production and operating-system paths must be read-only")
    for visible, private in (("/tmp", "tmp"), ("/var/tmp", "var-tmp")):
        lhs, rhs = os.stat(visible), os.stat(str(evidence / private))
        if (lhs.st_dev, lhs.st_ino) != (rhs.st_dev, rhs.st_ino):
            raise ValueError("temporary directories must bind to acceptance data")
    for key in os.environ:
        upper = key.upper()
        if any(part in upper for part in ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "PROXY")):
            raise ValueError("offline process must not inherit credentials or proxy variables")


class FakeNotFound(Exception):
    def get_status_code(self):
        return 404


class RejectHTTP:
    def __getattr__(self, name):
        raise AssertionError("HTTP is forbidden in this offline test")


class FakeDownloader:
    def __init__(self, fixture, evidence):
        self.evidence = pathlib.Path(evidence)
        self.fixture = within(fixture, self.evidence)
        self.calls = 0

    def __call__(self, url, destination, allowed_hosts, **kwargs):
        if url != SOURCE_URL or tuple(allowed_hosts) != ("offline.invalid",):
            raise ValueError("only the fixed offline fixture is permitted")
        if not isinstance(kwargs.get("http_client"), RejectHTTP):
            raise ValueError("the rejecting HTTP adapter is required")
        destination = within(destination, self.evidence)
        size = self.fixture.stat().st_size
        if size > int(kwargs["max_bytes"]):
            raise ValueError("fixture exceeds the configured source limit")
        shutil.copyfile(str(self.fixture), str(destination))
        self.calls += 1
        return {"size": size, "sha256": digest(destination)}


class FakeCOS:
    def __init__(self, evidence):
        self.evidence = pathlib.Path(evidence)
        self.objects = {}
        self.uploads = 0
        self.heads = 0

    def head_object(self, Bucket, Key):
        if Bucket != FAKE_BUCKET:
            raise ValueError("only the offline COS bucket is permitted")
        self.heads += 1
        entry = self.objects.get(Key)
        if entry is None:
            raise FakeNotFound()
        path = pathlib.Path(entry["path"])
        return {"Content-Length": str(path.stat().st_size), "x-cos-meta-sha256": digest(path)}

    def upload_file(self, **kwargs):
        if kwargs.get("Bucket") != FAKE_BUCKET:
            raise ValueError("only the offline COS bucket is permitted")
        key = pathlib.PurePosixPath(kwargs["Key"])
        if key.is_absolute() or ".." in key.parts or "\\" in str(key):
            raise ValueError("invalid fake object key")
        source = within(kwargs["LocalFilePath"], self.evidence)
        fake_root = within(self.evidence / "fake-cos", self.evidence)
        target = within(fake_root / str(key), fake_root)
        if target.exists():
            raise ValueError("fake objects must not be overwritten")
        metadata = kwargs.get("Metadata") or {}
        if metadata.get("x-cos-meta-sha256") != digest(source):
            raise ValueError("fake upload checksum metadata mismatch")
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copyfile(str(source), str(target))
        self.objects[str(key)] = {"path": str(target), "sha256": digest(target)}
        self.uploads += 1


class RecordingRunner:
    def __init__(self, evidence, binaries=(FFMPEG, FFPROBE)):
        self.evidence = pathlib.Path(evidence)
        self.binaries = {str(pathlib.Path(path).resolve()) for path in binaries}
        self.commands = []

    def __call__(self, args, **kwargs):
        args = [str(arg) for arg in args]
        if not args or str(pathlib.Path(args[0]).resolve()) not in self.binaries:
            raise ValueError("only the isolated FFmpeg and FFprobe binaries are permitted")
        for arg in args[1:]:
            if "://" in arg:
                raise ValueError("FFmpeg network inputs are forbidden")
            if os.path.isabs(arg):
                within(arg, self.evidence)
        started = time.monotonic()
        result = subprocess.run(args, **kwargs)
        self.commands.append({"argv": args, "return_code": result.returncode,
                              "elapsed_seconds": round(time.monotonic() - started, 3)})
        return result


def gpu_command_facts(commands):
    encoded = []
    for item in commands:
        argv = item["argv"]
        if "-c:v" in argv and argv[argv.index("-c:v") + 1] == "h264_nvenc":
            encoded.append(item)
    if len(encoded) != 1 or encoded[0]["return_code"] != 0:
        raise ValueError("exactly one successful original NVENC pipeline command is required")
    argv = encoded[0]["argv"]
    hwaccel = argv[argv.index("-hwaccel") + 1] if "-hwaccel" in argv else None
    # The frozen production command uses default decoding and CPU filters.
    # Do not impose a new CUDA-decode requirement on an unchanged business path.
    return {"video_encoder": "h264_nvenc", "explicit_hwaccel": hwaccel,
            "cuda_decode_requested": hwaccel == "cuda"}


def frozen_processor_module():
    if (X_ROOT / "current").resolve() != SOURCE_ROOT or SOURCE_ROOT.resolve() != SOURCE_ROOT:
        raise ValueError("X current is not the approved frozen release")
    for relative, expected in SOURCE_HASHES.items():
        if digest(within(SOURCE_ROOT / relative, SOURCE_ROOT)) != expected:
            raise ValueError("frozen X source checksum mismatch")
    sys.path.insert(0, str(SOURCE_ROOT))
    module = importlib.import_module("features.x_posts.media_repair")
    if pathlib.Path(module.__file__).resolve() != SOURCE_ROOT / "features/x_posts/media_repair.py":
        raise ValueError("unexpected X processor import origin")
    return module


def exercise(evidence, module, runner):
    fixture = evidence / "synthetic-input.mp4"
    command = [str(FFMPEG), "-hide_banner", "-loglevel", "error", "-nostdin",
               "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=2",
               "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2",
               "-map", "0:v:0", "-map", "1:a:0", "-t", "2",
               "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
               "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "48000", "-ac", "2",
               "-movflags", "+faststart", str(fixture)]
    result = runner(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True, timeout=30, check=False,
                    env=module._safe_subprocess_environment())
    if result.returncode:
        raise ValueError("synthetic fixture creation failed")
    config = module.WorkerConfig(
        enabled=True, host="127.0.0.1", port=0, token="offline-not-production",
        allowed_hosts=("offline.invalid",), work_root=evidence / "processor-state",
        ffmpeg_bin=str(FFMPEG), ffprobe_bin=str(FFPROBE),
        cos_secret_id="offline-only", cos_secret_key="offline-only",
        cos_bucket=FAKE_BUCKET, cos_region="offline", cos_domain="https://offline.invalid",
        cos_prefix="offline-acceptance", transcode_timeout=40,
    )
    fake_cos = FakeCOS(evidence)
    downloader = FakeDownloader(fixture, evidence)
    processor = module.MediaRepairProcessor(
        config, runner=runner, cos_client=fake_cos, downloader=downloader, http_client=RejectHTTP())
    payload = {"job_key": hashlib.sha256((RUN_ID + str(evidence)).encode()).hexdigest(),
               "material_id": "999999999", "pool_item_id": 999999999,
               "source_url": SOURCE_URL, "source_sha256": digest(fixture),
               "source_size": fixture.stat().st_size, "trigger_code": "operator_forced_repair",
               "profile": module.REPAIR_PROFILE, "duration_policy": "standard"}
    first = processor.repair(payload)
    command_count = len(runner.commands)
    manifest = config.work_root / "manifests" / (payload["job_key"] + ".json")
    manifest_hash = digest(manifest)
    second = processor.repair(payload)
    write_once(evidence / "processor-results.json", {
        "first": first, "second": second, "fake_downloads": downloader.calls,
        "fake_cos_uploads": fake_cos.uploads, "fake_cos_heads": fake_cos.heads,
        "commands_before_reuse": command_count, "commands_after_reuse": len(runner.commands),
        "manifest_before_reuse_sha256": manifest_hash, "manifest_after_reuse_sha256": digest(manifest),
    })
    if (first["status"] != "ready" or first["reused"] or second["status"] != "ready"
            or not second["reused"] or downloader.calls != 1 or fake_cos.uploads != 1
            or len(runner.commands) != command_count or digest(manifest) != manifest_hash):
        raise ValueError("offline repair and reuse invariants failed")
    if {k: v for k, v in first.items() if k != "reused"} != {
            k: v for k, v in second.items() if k != "reused"}:
        raise ValueError("reused result differs from the repaired result")
    gpu = gpu_command_facts(runner.commands)
    stored = next(iter(fake_cos.objects.values()))
    if (digest(stored["path"]) != first["output_sha256"]
            or pathlib.Path(stored["path"]).stat().st_size != first["output_size"]):
        raise ValueError("retained output does not match the processor result")
    return {"type": "frozen_processor_with_real_ffmpeg_and_fake_network_adapters",
            "source_hashes": SOURCE_HASHES, "profile": module.REPAIR_PROFILE,
            "first": first, "second": second, "output_artifact": stored["path"],
            "manifest": str(manifest), "manifest_sha256": manifest_hash,
            "fixture_sha256": digest(fixture), "fake_downloads": downloader.calls,
            "fake_cos_uploads": fake_cos.uploads, "fake_cos_heads": fake_cos.heads,
            "gpu_command": gpu,
            "production_state_readonly": True, "private_network_verified": True,
            "real_cos_or_platform_calls": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=pathlib.Path)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--approved-run-id", required=True)
    args = parser.parse_args()
    if args.approved_run_id != RUN_ID or not re.fullmatch(r"[0-9a-f]{40}", args.sha):
        raise ValueError("the exact acceptance run and approved pushed SHA are required")
    if pathlib.Path(sys.prefix).resolve() != PYTHON_ROOT:
        raise ValueError("use the isolated X Python runtime")
    require_commit(args.repo, args.sha)
    evidence = EVIDENCE_BASE / args.sha
    if evidence.resolve() != evidence or not evidence.is_dir():
        raise ValueError("the dedicated acceptance directory must be prepared without symlinks")
    verify_namespace(evidence)
    os.umask(0o077)
    write_once(evidence / "attempt.json", {"sha": args.sha, "run_id": RUN_ID})
    runner = RecordingRunner(evidence)
    report = {"ok": False, "sha": args.sha, "run_id": RUN_ID,
              "source_release_sha": SOURCE_RELEASE_SHA, "source_root": str(SOURCE_ROOT)}
    try:
        memory = subprocess.check_output([
            "/usr/bin/nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            timeout=10, text=True).strip().splitlines()
        if not memory or int(memory[0].strip()) < 1024:
            raise ValueError("at least 1024 MiB free GPU memory is required")
        module = frozen_processor_module()
        report.update(exercise(evidence, module, runner))
        report["ok"] = True
    except Exception as error:
        report["error_type"] = type(error).__name__
        code = getattr(error, "code", "offline_acceptance_failed")
        report["error_code"] = code if re.fullmatch(r"[a-z_]{1,80}", str(code)) else "unclassified"
    finally:
        report["commands"] = runner.commands
        write_once(evidence / "result.json", report)
    print(json.dumps({"ok": report["ok"], "report": str(evidence / "result.json"),
                      "error_code": report.get("error_code")}))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print(json.dumps({"ok": False, "error_type": type(error).__name__}), file=sys.stderr)
        sys.exit(1)
