#!/usr/bin/env python3
"""GitHub-first isolated node staging. Defaults to no service start/no claims.

Run on the destination Linux host with a private manifest. Never edits legacy
services, credentials, task databases, or rendering directories.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import subprocess
import sys


def command(argv, **kwargs):
    result = subprocess.run([str(x) for x in argv], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, timeout=kwargs.pop("timeout", 600), **kwargs)
    if result.returncode:
        raise ValueError("command_failed_" + Path(str(argv[0])).name.replace("-", "_"))
    return result.stdout.strip()


def private_json(path):
    path = Path(path)
    if not path.is_absolute() or path.is_symlink() or (os.name != "nt" and path.stat().st_mode & 0o077):
        raise ValueError("manifest_permissions")
    return json.loads(path.read_text())


def absolute(value):
    text = str(value)
    if not text.startswith("/") or any(p in {"..", "."} for p in text.split("/")) or not re.fullmatch(r"/[A-Za-z0-9_./-]+", text):
        raise ValueError("path_invalid")
    return PurePosixPath(text)


def plan(spec):
    node = str(spec.get("node_id") or "")
    sha = str(spec.get("sha") or "")
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", node) or not re.fullmatch(r"[a-f0-9]{40}", sha):
        raise ValueError("identity_or_commit_invalid")
    if spec.get("backend") not in {"cpu", "gpu"}:
        raise ValueError("backend_invalid")
    if not isinstance(spec.get("gpu_native", False), bool):
        raise ValueError("gpu_native_must_be_boolean")
    if spec.get("gpu_native") and spec["backend"] != "gpu":
        raise ValueError("gpu_native_requires_gpu_node")
    root = absolute(spec.get("install_root", "/opt/pipeline-dispatch"))
    state = absolute(spec["state_root"])
    if root.name != "pipeline-dispatch" or state.name != "pipeline-dispatch" or state == root or not (str(state).startswith("/data/") or str(state).startswith("/mnt/")):
        raise ValueError("dedicated_paths_required")
    repo = spec.get("repository", "git@github.com:gaoyuan12356416/ai-.git")
    if not re.fullmatch(r"(?:git@github\.com:|https://github\.com/)[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\.git", repo):
        raise ValueError("github_repository_required")
    tunnel = spec.get("tunnel")
    if tunnel:
        if not re.fullmatch(r"[a-zA-Z0-9_.-]+@[a-zA-Z0-9_.-]+", str(tunnel.get("target", ""))):
            raise ValueError("tunnel_target_invalid")
        absolute(tunnel["identity_file"]); absolute(tunnel["known_hosts"])
        if int(tunnel.get("local_port", 18846)) not in range(1024, 65536):
            raise ValueError("tunnel_port_invalid")
    for source in spec.get("environment_files", []): absolute(source)
    release = root / "releases" / sha
    return {"node_id": node, "sha": sha, "repository": repo, "root": str(root), "state": str(state),
            "release": str(release), "venv": str(root / "venvs" / sha),
            "staging": str(root / "staged" / node / sha), "unit": "pipeline-node-" + node + ".service",
            "tunnel_unit": "pipeline-node-" + node + "-tunnel.service"}


def read_environment(paths):
    values = {}
    for filename in paths:
        path = Path(filename)
        if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
            raise ValueError("source_environment_permissions")
        for raw in path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#"): continue
            if line.startswith("export "): line = line[7:]
            key, sep, value = line.partition("=")
            if not sep or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
                raise ValueError("source_environment_syntax")
            # Only renderer/COS settings; never import central account secrets.
            if key not in {"SSL_CERT_FILE", "SSL_CERT_DIR", "RANDOM_GPU_ASSET_CACHE_ROOT", "RANDOM_GPU_PIPELINE_V2"} and not key.startswith(("X_POST_", "FB_PAGE_GPU_", "TT_POST_GPU_", "TT_POST_DEFAULT_", "TT_POST_URL_", "RANDOM_OVERLAY_", "COS_", "YOUTUBE_AUTO_CODEX_", "CODEX_HOME")):
                continue
            if any(word in key for word in ("REFRESH_TOKEN", "ACCESS_TOKEN", "CLIENT_SECRET", "COOKIE")):
                raise ValueError("central_credential_in_node_source")
            if value.startswith(("'", '"')):
                words = shlex.split(value)
                if len(words) != 1: raise ValueError("source_environment_syntax")
                value = words[0]
            values[key] = value
    return values


def configure_tls_ca(environment, venv):
    """Portable Python may retain an inaccessible build-host OpenSSL path."""
    if "SSL_CERT_FILE" in environment:
        return  # Preserve explicit trust configuration; doctor validates it.
    bundle = command(["runuser", "-u", "pipeline-worker", "--", Path(venv) / "bin/python", "-c",
                      "import certifi,os; p=certifi.where(); assert os.path.isfile(p) and os.access(p,os.R_OK); print(p)"])
    absolute(bundle)
    environment["SSL_CERT_FILE"] = bundle


def runtime_requirements(spec, release):
    """Fingerprint the selected runtime and its included base requirements."""
    release = Path(release)
    base = release / "deploy/pipeline-node.requirements.txt"
    data = base.read_bytes()
    if spec.get("gpu_native"):
        selected = release / "deploy/pipeline-gpu.requirements.txt"
        data += b"\0gpu_native\0" + selected.read_bytes()
    else:
        selected = base
    return selected, hashlib.sha256(data).hexdigest()


def stage_runtime(spec, info):
    """A SHA's existing runtime is immutable, including the selected profile."""
    python = spec.get("python", "/usr/bin/python3.9")
    absolute(python)
    venv = Path(info["venv"])
    requirements, expected = runtime_requirements(spec, info["release"])
    marker = venv / ".requirements.sha256"
    if not venv.exists():
        command([python, "-m", "venv", venv])
        command([venv / "bin/python", "-m", "pip", "install", "--disable-pip-version-check", "-r", requirements], timeout=1200)
        immutable(marker, expected.encode())
    if not marker.is_file():
        raise ValueError("venv_install_incomplete")
    if marker.read_text() != expected:
        raise ValueError("venv_requirements_mismatch")


def configure_gpu_runtime(spec, info, environment):
    if not spec.get("gpu_native"):
        return
    interpreter = str(PurePosixPath(info["venv"]) / "bin/python")
    if environment.get("RANDOM_GPU_PYTHON", interpreter) != interpreter:
        raise ValueError("gpu_runtime_interpreter_mismatch")
    environment.setdefault("RANDOM_GPU_NATIVE", "1")
    environment.setdefault("RANDOM_GPU_COMPUTE", "1")
    environment["RANDOM_GPU_PYTHON"] = interpreter


def gpu_runtime_doctor(spec, info):
    """Local-only tiny device check as the service user; no claims or uploads."""
    if not spec.get("gpu_native"):
        return
    code = (
        "import av; from features.random_gpu.gpu_compute import CudaSHA256; "
        "from features.random_gpu.native_video import codec_runtime; "
        "from features.random_gpu.gpu_mux import CudaMP4; "
        "codec_runtime(); "
        "assert CudaSHA256().update(b'abc').hexdigest() == "
        "'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad'; "
        "assert CudaMP4().annexb_sample(bytes.fromhex('0000016588')) == bytes.fromhex('000000026588'); "
        "av.CodecContext.create('aac','w'); print('gpu_runtime_local_doctor_passed')"
    )
    command(["runuser", "-u", "pipeline-worker", "--", Path(info["venv"]) / "bin/python", "-c", code],
            cwd=info["release"], timeout=120)


def immutable(path, data, mode=0o600):
    path = Path(path)
    if any(parent.is_symlink() for parent in (path, *path.parents)): raise ValueError("staging_symlink")
    if path.exists():
        if path.read_bytes() != data: raise ValueError("staging_content_changed")
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, "wb") as output:
        output.write(data); output.flush(); os.fsync(output.fileno())


def unit_text(spec, info):
    tunnel = spec.get("tunnel")
    dependencies = "After=network-online.target" + (" " + info["tunnel_unit"] if tunnel else "") + "\nWants=network-online.target" + (" " + info["tunnel_unit"] if tunnel else "")
    return ("[Unit]\nDescription=Isolated pipeline pull node " + info["node_id"] + "\n" + dependencies
        + "\n[Service]\nType=simple\nUser=pipeline-worker\nGroup=pipeline-worker\nWorkingDirectory=" + info["release"]
        + "\nEnvironmentFile=" + info["staging"] + "/node.env\nExecStart=" + info["venv"] + "/bin/python " + info["release"] + "/scripts/pipeline_worker.py"
        + "\nRestart=on-failure\nRestartSec=10\nKillMode=mixed\nTimeoutStopSec=infinity\nUMask=0077\nNoNewPrivileges=true\nProtectSystem=strict\nProtectHome=true\nPrivateTmp=true\nReadWritePaths=" + info["state"]
        + "\n[Install]\nWantedBy=multi-user.target\n")


def stage_catalogs(spec, info):
    catalogs = {}
    def digest(path):
        value = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""): value.update(chunk)
        return value.hexdigest()
    for entry in spec.get("asset_catalogs", []):
        source = Path(str(absolute(entry["root"])))
        sha = str(entry["sha256"])
        manifest = source / "manifest.json"
        if not re.fullmatch(r"[a-f0-9]{64}", sha) or source.is_symlink() or manifest.is_symlink() or digest(manifest) != sha:
            raise ValueError("catalog_manifest_invalid")
        body = json.loads(manifest.read_text()); files = [manifest]
        for rows in body["categories"].values():
            for row in rows:
                path = source / row["name"]
                if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(source.resolve()) or path.stat().st_size != row["size"] or digest(path) != row["sha256"]:
                    raise ValueError("catalog_asset_invalid")
                files.append(path)
        target = Path(info["state"]) / "assets" / sha if entry.get("copy", False) else source
        if target != source:
            if any(parent.is_symlink() for parent in (target, *target.parents)): raise ValueError("catalog_target_symlink")
            target.mkdir(parents=True, exist_ok=True, mode=0o755)
            target.parent.chmod(0o755)
            for path in files:
                dest = target / path.relative_to(source)
                if any(parent.is_symlink() for parent in (dest, *dest.parents)): raise ValueError("catalog_target_symlink")
                dest.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                if dest.is_symlink(): raise ValueError("catalog_target_symlink")
                if dest.exists():
                    if digest(dest) != digest(path): raise ValueError("catalog_target_changed")
                else:
                    # Destination is a new content-addressed node catalog only.
                    with dest.open("xb") as output, path.open("rb") as handle:
                        shutil.copyfileobj(handle, output, 1024 * 1024)
                        output.flush(); os.fsync(output.fileno())
                    dest.chmod(0o444)
        catalogs[sha] = str(target)
    return catalogs


def tunnel_text(spec):
    tunnel = spec["tunnel"]
    argv = ["/usr/bin/ssh", "-N", "-i", tunnel["identity_file"], "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "UserKnownHostsFile=" + tunnel["known_hosts"], "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3", "-L", "127.0.0.1:" + str(tunnel.get("local_port", 18846)) + ":127.0.0.1:18846", tunnel["target"]]
    return "[Unit]\nDescription=Dedicated pipeline coordinator SSH forward\nAfter=network-online.target\nWants=network-online.target\n[Service]\nUser=root\nExecStart=" + " ".join(argv) + "\nRestart=always\nRestartSec=10\nNoNewPrivileges=true\n[Install]\nWantedBy=multi-user.target\n"


def stage(spec, info):
    import pwd
    if os.geteuid() != 0: raise ValueError("root_required_for_staging")
    root, state = Path(info["root"]), Path(info["state"])
    for target in (root, state):
        for ancestor in (target, *target.parents):
            if ancestor.is_symlink(): raise ValueError("dedicated_path_symlink")
        target.mkdir(parents=True, exist_ok=True, mode=0o755)
    try: user = pwd.getpwnam("pipeline-worker")
    except KeyError:
        command(["useradd", "--system", "--no-create-home", "--home-dir", str(state), "--shell", "/usr/sbin/nologin", "pipeline-worker"])
        user = pwd.getpwnam("pipeline-worker")
    state.chmod(0o700); os.chown(state, user.pw_uid, user.pw_gid)
    mirror = root / "repository.git"
    if not mirror.exists():
        command(["git", "init", "--bare", mirror])
        command(["git", "--git-dir", mirror, "remote", "add", "origin", info["repository"]])
    if command(["git", "--git-dir", mirror, "remote", "get-url", "origin"]) != info["repository"]:
        raise ValueError("repository_origin_mismatch")
    command(["git", "--git-dir", mirror, "fetch", "--depth=1", "origin", info["sha"]])
    if command(["git", "--git-dir", mirror, "rev-parse", "FETCH_HEAD"]) != info["sha"]:
        raise ValueError("github_commit_mismatch")
    release, venv, staging = Path(info["release"]), Path(info["venv"]), Path(info["staging"])
    if not release.exists(): command(["git", "--git-dir", mirror, "worktree", "add", "--detach", release, info["sha"]])
    if command(["git", "-C", release, "rev-parse", "HEAD"]) != info["sha"] or command(["git", "-C", release, "status", "--porcelain"]):
        raise ValueError("release_changed")
    stage_runtime(spec, info)
    environment = read_environment(spec.get("environment_files", []))
    environment.update({str(k): str(v) for k, v in spec.get("environment", {}).items()})
    configure_gpu_runtime(spec, info, environment)
    configure_tls_ca(environment, venv)
    catalogs = stage_catalogs(spec, info)
    if catalogs:
        active_sha = spec.get("active_catalog_sha256") or next(iter(catalogs))
        if active_sha not in catalogs: raise ValueError("active_catalog_missing")
        for root_key, sha_key in (("X_POST_RANDOM_TEMPLATE_ASSET_ROOT", "X_POST_RANDOM_TEMPLATE_MANIFEST_SHA256"), ("FB_PAGE_GPU_RANDOM_OVERLAY_ROOT", "FB_PAGE_GPU_RANDOM_OVERLAY_MANIFEST_SHA256"), ("TT_POST_GPU_RANDOM_OVERLAY_ROOT", "TT_POST_GPU_RANDOM_OVERLAY_MANIFEST_SHA256")):
            environment[root_key], environment[sha_key] = catalogs[active_sha], active_sha
        environment["RANDOM_OVERLAY_ASSET_CATALOGS"] = json.dumps(catalogs, separators=(",", ":"))
    environment.update(PIPELINE_NODE_ID=info["node_id"], PIPELINE_RENDER_BACKEND=spec["backend"], PIPELINE_NODE_STATE_ROOT=info["state"], PIPELINE_NODE_CONFIG=info["state"] + "/credentials.json")
    for key, suffix in (("X_POST_MEDIA_REPAIR_WORK_ROOT", "x-repair"), ("FB_PAGE_GPU_WORK_ROOT", "fb-prepare"), ("TT_POST_GPU_WORK_ROOT", "tt-prepare")):
        environment[key] = info["state"] + "/" + suffix
    if not environment.get("PIPELINE_COORDINATOR_URL"):
        environment["PIPELINE_COORDINATOR_URL"] = "http://127.0.0.1:" + str(spec.get("tunnel", {}).get("local_port", 18846))
    if spec["backend"] == "cpu":
        environment.update(FB_PAGE_GPU_VIDEO_ENCODER="libx264", FB_PAGE_GPU_COMPOSITOR_BACKEND="cpu_legacy", FB_PAGE_GPU_PROFILE="fb-post-random-overlay-libx264-720x1280-v1", TT_POST_GPU_VIDEO_ENCODER="libx264", TT_POST_GPU_COMPOSITOR_BACKEND="cpu_legacy")
    for key, value in environment.items():
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or any(c in value for c in "\n\r\0"):
            raise ValueError("environment_syntax")
    env_text = "".join(k + "=" + json.dumps(v, ensure_ascii=True) + "\n" for k, v in sorted(environment.items()))
    immutable(staging / "node.env", env_text.encode())
    # Only this new node's state/staged files change owner; legacy roots are readonly.
    os.chown(staging / "node.env", user.pw_uid, user.pw_gid)
    for parent in (staging, staging.parent, staging.parent.parent): parent.chmod(0o755)
    immutable(staging / info["unit"], unit_text(spec, info).encode(), 0o644)
    if spec.get("tunnel"): immutable(staging / info["tunnel_unit"], tunnel_text(spec).encode(), 0o644)
    return user


def activate(spec, info):
    # Refuse replacement of a running pool. Parent must drain it beforehand.
    for unit in (info["unit"],):
        result = subprocess.run(["systemctl", "is-active", "--quiet", unit], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode == 0: raise ValueError("existing_node_requires_drain")
    if not (Path(info["state"]) / "credentials.json").is_file(): raise ValueError("node_not_enrolled")
    if not (Path(info["staging"]) / ".verified").is_file(): raise ValueError("node_not_verified")
    units = [info["unit"]] + ([info["tunnel_unit"]] if spec.get("tunnel") else [])
    for unit in units:
        command(["install", "-m", "0644", str(Path(info["staging"]) / unit), "/etc/systemd/system/" + unit])
    command(["systemctl", "daemon-reload"])
    command(["systemctl", "enable", "--now", info["unit"]])


def start_tunnel(info):
    unit = info["tunnel_unit"]
    source, target = Path(info["staging"]) / unit, Path("/etc/systemd/system") / unit
    if target.exists() and target.read_bytes() != source.read_bytes():
        raise ValueError("existing_tunnel_configuration_changed")
    command(["install", "-m", "0644", source, target])
    command(["systemctl", "daemon-reload"])
    command(["systemctl", "start", unit])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, help="absolute mode0600 JSON; no secret command arguments")
    parser.add_argument("--plan", action="store_true", help="validate/print safe paths without mutation")
    parser.add_argument("--verify", action="store_true", help="run local encode/catalog/COS diagnostics after staging")
    parser.add_argument("--verify-local", action="store_true", help="run local encode/catalog/runtime diagnostics only; no COS/enrollment/start")
    parser.add_argument("--enroll", action="store_true", help="requires --verify; token from private env/file")
    parser.add_argument("--activate", action="store_true", help="start only this new inactive enrolled node")
    args = parser.parse_args()
    try:
        if args.verify_local and (args.verify or args.enroll or args.activate):
            raise ValueError("local_verification_cannot_activate_or_enroll")
        spec = private_json(args.manifest); info = plan(spec)
        if not args.plan:
            stage(spec, info)
            if args.enroll and not args.verify: raise ValueError("enrollment_requires_verification")
            if args.verify or args.verify_local:
                gpu_runtime_doctor(spec, info)
                if args.enroll and spec.get("tunnel"): start_tunnel(info)
                argv = ["runuser", "-u", "pipeline-worker", "--", info["venv"] + "/bin/python", info["release"] + "/scripts/pipeline_node_check.py", "--env", info["staging"] + "/node.env", "--encode"]
                if args.verify: argv += ["--cos-check"]
                if args.enroll: argv += ["--enroll", info["state"] + "/credentials.json"]
                command(argv, timeout=1200)
                immutable(Path(info["staging"]) / (".verified" if args.verify else ".verified-local"), info["sha"].encode())
            if args.activate: activate(spec, info)
        print(json.dumps({"ok": True, "plan_only": args.plan, "activated": bool(args.activate and not args.plan), **info}))
        return 0
    except Exception as exc:
        code = str(exc) if isinstance(exc, ValueError) and re.fullmatch(r"[a-z][a-z0-9_]{1,100}", str(exc)) else "node_install_failed"
        print(json.dumps({"ok": False, "code": code})); return 1


if __name__ == "__main__": raise SystemExit(main())
