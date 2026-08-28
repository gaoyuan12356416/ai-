#!/usr/bin/env python3
"""Prepare isolated HK services; production activation requires an explicit gate."""
import argparse
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import tarfile
import urllib.request

from check_storage import inspect_storage, require_boundary

RUN_ID = "gpu-service-migration-20260828T1502"
BACKUP = pathlib.Path("/data/migrations") / RUN_ID / "hk"
INPUTS = BACKUP.parent / "hk-inputs"
X_ROOT = pathlib.Path("/data/x-post-media-repair")
AD_ROOT = pathlib.Path("/data/ad-material")
X_SHA = "fba8ff603e979b443339108cb2ce45c975fbd39f"
X_PROFILE = "x-h264-nvenc-720-duration-policy-v5"
X_TUNNEL = "x-post-media-repair-tunnel.service"
OWNED_REL = pathlib.Path("ops/gpu-service-migration-20260828/hk")
UNITS = {
    "x": ["x-post-media-repair.service"],
    "ad": ["ad-material-generation.service", "ad-material-vision.service",
           "ad-material-hk-tunnel.service"],
}
X_PACKAGES = [
    "certifi==2026.4.22", "charset-normalizer==3.4.7",
    "cos_python_sdk_v5==1.9.42", "crcmod==1.7", "idna==3.13",
    "pycryptodome==3.23.0", "requests==2.32.5", "six==1.17.0",
    "urllib3==2.6.3", "xmltodict==1.0.4",
]


def run(command, timeout=120, env=None, check=True):
    proc = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          universal_newlines=True, timeout=timeout, env=env)
    if check and proc.returncode:
        raise RuntimeError("command failed: " + command[0] + ": " + proc.stderr[-1000:])
    return proc.stdout.strip()


def digest(path):
    result = hashlib.sha256()
    with pathlib.Path(path).open("rb") as src:
        for block in iter(lambda: src.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def verified_archive(name):
    if name not in {"ad-runtime.tgz", "ad-data.tgz", "x-us-history.tgz"}:
        raise ValueError("unknown migration archive")
    path = INPUTS / name
    proof = INPUTS / (name + ".verified.json")
    if not proof.is_file():
        proof = INPUTS / "transfer.json"
    if not proof.is_file():
        raise ValueError("archive has no completed transfer verification")
    records = json.loads(proof.read_text())["files"]
    expected = next((item for item in records if item.get("name") == name), None)
    if (expected is None or path.is_symlink() or not path.is_file()
            or path.stat().st_size != expected.get("bytes")
            or digest(path) != expected.get("sha256")):
        raise ValueError("migration archive integrity mismatch")
    return path


def private_dir(path):
    pathlib.Path(path).mkdir(parents=True, exist_ok=True)
    os.chmod(str(path), 0o700)


def write_private(path, content):
    path = pathlib.Path(path)
    private_dir(path.parent)
    temporary = path.with_name(path.name + ".partial")
    temporary.write_text(content, encoding="utf-8")
    os.chmod(str(temporary), 0o600)
    os.replace(str(temporary), str(path))


def write_json(path, value):
    write_private(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def safe_extract(archive, target):
    target = pathlib.Path(target).resolve()
    private_dir(target)
    with tarfile.open(str(archive), "r:gz") as tf:
        for item in tf.getmembers():
            destination = (target / item.name).resolve()
            if target != destination and target not in destination.parents:
                raise ValueError("archive path escaped target")
            if item.issym() or item.islnk():
                base = destination.parent if item.issym() else target
                link = (base / item.linkname).resolve()
                if target != link and target not in link.parents:
                    raise ValueError("archive link escaped target")
            elif not (item.isfile() or item.isdir()):
                raise ValueError("unsupported archive entry")
        tf.extractall(str(target))


def require_commit(repo, sha):
    if not re.fullmatch(r"[0-9a-f]{40}", sha or ""):
        raise ValueError("an exact pushed SHA is required")
    if run(["git", "-C", str(repo), "rev-parse", "HEAD"]) != sha:
        raise ValueError("repository HEAD does not match approved SHA")
    run(["git", "-C", str(repo), "diff", "--exit-code", sha, "--", str(OWNED_REL)])
    run(["git", "-C", str(repo), "merge-base", "--is-ancestor", sha,
         "origin/codex/gpu-service-migration-20260828"])


def active(unit):
    return run(["systemctl", "is-active", unit], check=False) in {"active", "activating"}


def tunnel_snapshot():
    fragment = pathlib.Path(run(["systemctl", "show", X_TUNNEL,
                                 "-p", "FragmentPath", "--value"]))
    if not fragment.is_file():
        raise ValueError("existing X tunnel unit is missing")
    return {"fragment": str(fragment), "sha256": digest(fragment),
            "active": active(X_TUNNEL),
            "enabled": run(["systemctl", "is-enabled", X_TUNNEL], check=False)}


def capture_x_tunnel_baseline():
    path = BACKUP / "x-tunnel-dependency-baseline.json"
    snapshot = tunnel_snapshot()
    if not path.exists():
        write_json(path, snapshot)
        return snapshot
    baseline = json.loads(path.read_text())
    for key in ("fragment", "sha256", "enabled"):
        if snapshot[key] != baseline[key]:
            raise ValueError("existing X tunnel changed; review before worker stop")
    return baseline


def restore_x_tunnel_if_previously_active():
    path = BACKUP / "x-tunnel-dependency-baseline.json"
    if not path.exists():
        return {"baseline_recorded": False, "restored": False}
    baseline = capture_x_tunnel_baseline()
    if baseline["active"]:
        run(["systemctl", "start", X_TUNNEL])
        if not active(X_TUNNEL):
            raise ValueError("originally active X tunnel did not resume")
    return {"baseline_recorded": True, "restored": baseline["active"]}


def preserve_units(component):
    destination = BACKUP / "units"
    private_dir(destination)
    path = BACKUP / (component + "-baseline.json")
    if path.exists():
        return json.loads(path.read_text())
    states = {}
    for unit in UNITS[component]:
        target = pathlib.Path("/etc/systemd/system") / unit
        dropins = run(["systemctl", "show", unit, "-p", "DropInPaths", "--value"], check=False)
        if dropins:
            raise ValueError("unexpected existing unit drop-ins; review before staging")
        if component == "ad" and target.exists():
            raise ValueError("HK ad unit already exists; do not overwrite an unowned service")
        if target.exists():
            shutil.copy2(str(target), str(destination / unit))
            os.chmod(str(destination / unit), 0o600)
        states[unit] = {
            "existed": target.exists(), "active": active(unit),
            "enabled": run(["systemctl", "is-enabled", unit], check=False) == "enabled",
            "sha256": digest(target) if target.exists() else None,
        }
    write_json(path, states)
    return states


def make_venv(destination, packages):
    destination = pathlib.Path(destination)
    marker = destination / "migration-runtime.json"
    if marker.exists():
        if json.loads(marker.read_text()).get("packages") != packages:
            raise ValueError("staged runtime has different package pins")
        return
    private_dir(destination.parent)
    run(["/usr/bin/python3.9", "-m", "venv", str(destination)])
    env = dict(os.environ)
    for name in ("pip-cache", "pip-tmp"):
        private_dir(BACKUP / name)
    env.update({"PIP_CACHE_DIR": str(BACKUP / "pip-cache"),
                "TMPDIR": str(BACKUP / "pip-tmp"), "PIP_DISABLE_PIP_VERSION_CHECK": "1"})
    output = run([str(destination / "bin/python"), "-m", "pip", "install",
                  "--index-url", "https://pypi.org/simple",
                  "--disable-pip-version-check"] + packages, timeout=900, env=env)
    write_private(destination / "migration-build.log", output + "\n")
    write_json(marker, {"packages": packages, "base": "/usr/bin/python3.9"})


def parse_env(path):
    result = {}
    for line in pathlib.Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise ValueError("invalid environment key")
        result[key] = value
    return result


def stage_x():
    if active(UNITS["x"][0]):
        command = run(["systemctl", "show", UNITS["x"][0], "-p", "ExecStart", "--value"])
        if str(X_ROOT) in command:
            raise ValueError("new X runtime already active; do not restage")
    source = pathlib.Path("/opt/x-post-media-repair/current").resolve()
    if source.name != X_SHA:
        raise ValueError("live X source release drifted")
    release = X_ROOT / "releases" / X_SHA
    if not release.exists():
        shutil.copytree(str(source), str(release), symlinks=True)
    make_venv(X_ROOT / "runtime/python", X_PACKAGES)
    private_dir(X_ROOT / "runtime/bin")
    for name in ("ffmpeg", "ffprobe"):
        shutil.copy2("/usr/bin/" + name, str(X_ROOT / "runtime/bin" / name))
    for name in ("manifests", "locks", "work", "tmp", "var-tmp"):
        private_dir(X_ROOT / "state" / name)
    private_dir(X_ROOT / "config")
    for old, new in (("/etc/x-post-media-repair.cos.env", "cos.env"),
                     ("/etc/x-post-media-repair.token", "token.env")):
        shutil.copy2(old, str(X_ROOT / "config" / new))
        os.chmod(str(X_ROOT / "config" / new), 0o600)
    values = parse_env("/etc/x-post-media-repair.env")
    values.update({
        "X_POST_MEDIA_REPAIR_WORK_ROOT": str(X_ROOT / "state"),
        "X_POST_MEDIA_REPAIR_FFMPEG_BIN": str(X_ROOT / "runtime/bin/ffmpeg"),
        "X_POST_MEDIA_REPAIR_FFPROBE_BIN": str(X_ROOT / "runtime/bin/ffprobe"),
        "TMPDIR": str(X_ROOT / "state/tmp"),
    })
    write_private(X_ROOT / "config/worker.env",
                  "".join(k + "=" + v + "\n" for k, v in sorted(values.items())))
    if (INPUTS / "x-us-history.tgz").is_file():
        safe_extract(verified_archive("x-us-history.tgz"), INPUTS / "x-us-history")
    return str(release)


def stage_ad(source, sha):
    if any(active(unit) for unit in UNITS["ad"]):
        raise ValueError("HK ad service active; do not restage")
    runtime = AD_ROOT / "runtime"
    safe_extract(verified_archive("ad-runtime.tgz"), runtime)
    make_venv(runtime / "python", ["Pillow==8.4.0"])
    private_dir(runtime / "bin")
    wrapper = runtime / "bin/codex"
    wrapper.write_text(
        '#!/bin/sh\nexec /data/ad-material/runtime/usr/bin/node '
        '/data/ad-material/runtime/usr/lib/node_modules/@openai/codex/bin/codex.js "$@"\n'
    )
    os.chmod(str(wrapper), 0o755)
    version_env = dict(os.environ)
    version_env["PATH"] = str(runtime / "usr/bin") + ":/usr/bin:/bin"
    private_dir(runtime / "version-check-home")
    version_env.update({"HOME": str(runtime / "version-check-home"),
                        "CODEX_HOME": str(runtime / "version-check-home/.codex"),
                        "TMPDIR": str(BACKUP / "pip-tmp")})
    if run([str(runtime / "usr/bin/node"), "--version"]) != "v22.22.2":
        raise ValueError("staged Node version mismatch")
    if run([str(wrapper), "--version"], env=version_env) != "codex-cli 0.147.0":
        raise ValueError("staged Codex version mismatch")
    unpacked = INPUTS / "ad-data"
    safe_extract(verified_archive("ad-data.tgz"), unpacked)
    mapping = {
        "root/ad_material_generation_jobs": "generation/jobs",
        "root/ad_material_vision_jobs": "vision/jobs",
        "root/ad_material_generation_workspace": "generation/workspace",
        "usr/share/nginx/html/ad-material-generation": "generation/public",
    }
    for relative, target in mapping.items():
        shutil.copytree(str(unpacked / relative), str(AD_ROOT / target),
                        symlinks=True, dirs_exist_ok=True)
    for component in ("generation", "vision"):
        for name in ("jobs", "home", "cache", "tmp", "var-tmp"):
            private_dir(AD_ROOT / component / name)
    private_dir(AD_ROOT / "auth-source")
    private_dir(AD_ROOT / "config")
    write_private(AD_ROOT / "auth-source/config.toml",
                  'model = "gpt-5.5"\napproval_policy = "never"\n')
    for name in ("generation.env", "vision.env"):
        values = parse_env(source / "env" / name)
        write_private(AD_ROOT / "config" / name,
                      "".join(k + "=" + v + "\n" for k, v in values.items()))
    release = AD_ROOT / "releases" / sha
    if not release.exists():
        shutil.copytree(str(source / "source"), str(release))
    return str(release)


def stage(repo, sha, component):
    require_commit(repo, sha)
    source = repo / OWNED_REL
    root = X_ROOT if component == "x" else AD_ROOT
    require_boundary(root)
    private_dir(root)
    inspect_storage(str(root))
    private_dir(BACKUP)
    preserve_units(component)
    control = BACKUP / "control" / sha
    if not control.exists():
        shutil.copytree(str(source), str(control),
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    release = stage_x() if component == "x" else stage_ad(source, sha)
    staged_units = BACKUP / ("staged-units-" + component)
    private_dir(staged_units)
    for unit in UNITS[component]:
        template = (source / "units" / (unit + ".in")).read_text()
        write_private(staged_units / unit, template.replace("@CONTROL@", str(control)))
    record = {"phase": "staged_not_activated", "component": component,
              "github_sha": sha, "release": release, "control": str(control),
              "staged_units": str(staged_units),
              "unit_hashes": {u: digest(staged_units / u) for u in UNITS[component]}}
    write_json(BACKUP / (component + "-staged.json"), record)
    return record


def require_cutover(approved, upstream_paused):
    if approved != RUN_ID or not upstream_paused:
        raise ValueError("explicit root cutover approval and paused upstream are required")


def switch_link(link, target):
    link = pathlib.Path(link)
    if link.exists() and not link.is_symlink():
        raise ValueError("refusing to replace a non-symlink current")
    temporary = link.with_name("current.migration-next")
    if temporary.is_symlink():
        temporary.unlink()
    temporary.symlink_to(target)
    os.replace(str(temporary), str(link))


def health(url):
    import time
    last = None
    for attempt in range(20):
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                return json.loads(response.read().decode())
        except (OSError, ValueError) as error:
            last = error
            time.sleep(0.5)
    raise RuntimeError("health check did not become ready") from last


def verify(component):
    root = X_ROOT if component == "x" else AD_ROOT
    result = {"component": component, "storage": inspect_storage(str(root))}
    services = UNITS[component][:-1] if component == "ad" else UNITS[component]
    for unit in services:
        if not active(unit):
            raise ValueError("service is not active: " + unit)
        pid = int(run(["systemctl", "show", unit, "-p", "MainPID", "--value"]))
        command = pathlib.Path("/proc/%d/cmdline" % pid).read_bytes().replace(b"\0", b" ").decode()
        if str(root) not in command:
            raise ValueError("service still uses its pre-migration runtime")
        area = root / "state" if component == "x" else root / ("vision" if "vision" in unit else "generation")
        for namespace, actual in (("tmp", "tmp"), ("var/tmp", "var-tmp")):
            ns = os.stat("/proc/%d/root/%s" % (pid, namespace))
            disk = os.stat(str(area / actual))
            if (ns.st_dev, ns.st_ino) != (disk.st_dev, disk.st_ino):
                raise ValueError("temporary directory does not bind to /data")
        result[unit] = {"pid": pid, "data_tmp_bind": True}
    if component == "x":
        result["health"] = health("http://127.0.0.1:8820/health")
        if result["health"].get("profile") != X_PROFILE:
            raise ValueError("X profile changed")
        result["tunnel_active"] = active(X_TUNNEL)
        if not result["tunnel_active"]:
            raise ValueError("existing X reverse tunnel is not active")
    else:
        result["generation_health"] = health("http://127.0.0.1:8797/health")
        result["vision_health"] = health("http://127.0.0.1:8796/health")
        result["tunnel_active"] = active(UNITS["ad"][-1])
        if not result["tunnel_active"]:
            raise ValueError("ad reverse tunnel is not active")
    return result


def activate(component, source_ad_stopped):
    record = json.loads((BACKUP / (component + "-staged.json")).read_text())
    units_dir = pathlib.Path(record["staged_units"])
    for unit, expected in record["unit_hashes"].items():
        if digest(units_dir / unit) != expected:
            raise ValueError("staged unit changed since preparation")
    root = X_ROOT if component == "x" else AD_ROOT
    inspect_storage(str(root))
    if component == "x":
        history = INPUTS / "x-us-history/data/x-post-media-repair/manifests"
        if not history.is_dir():
            raise ValueError("US X history is not staged; wait for relay and restage")
        if any(pathlib.Path("/var/lib/x-post-media-repair/work").iterdir()):
            raise ValueError("old X worker has active or residual work; inspect before stopping")
        capture_x_tunnel_baseline()
        run(["systemctl", "stop", UNITS["x"][0]], timeout=1950)
        source = pathlib.Path("/var/lib/x-post-media-repair/manifests")
        shutil.copytree(str(source), str(BACKUP / "hk-manifests-at-cutover"),
                        dirs_exist_ok=True)
        shutil.copytree(str(source), str(X_ROOT / "state/manifests"),
                        dirs_exist_ok=True)
        history = INPUTS / "x-us-history/data/x-post-media-repair/manifests"
        if history.is_dir():
            run([str(X_ROOT / "runtime/python/bin/python"),
                 str(pathlib.Path(__file__).with_name("merge_x_manifests.py")),
                 "--source", str(history), "--destination", str(X_ROOT / "state/manifests"),
                 "--apply", "--with-head"], timeout=600)
    elif (not source_ad_stopped or not (AD_ROOT / "auth-source/auth.json").is_file()
          or not (INPUTS / "auth-transfer.json").is_file()
          or not (INPUTS / "ad-final-sync.json").is_file()):
        raise ValueError("US ad stop, final data sync and protected auth transfer are required")
    switch_link(root / "current", record["release"])
    for unit in UNITS[component]:
        shutil.copy2(str(units_dir / unit), "/etc/systemd/system/" + unit)
        os.chmod("/etc/systemd/system/" + unit, 0o644)
    run(["systemctl", "daemon-reload"])
    for unit in UNITS[component]:
        run(["systemctl", "enable", unit])
        run(["systemctl", "start", unit])
    if component == "x":
        restore_x_tunnel_if_previously_active()
    result = verify(component)
    write_json(BACKUP / (component + "-activated.json"), result)
    return result


def rollback(component):
    baseline = json.loads((BACKUP / (component + "-baseline.json")).read_text())
    staged = json.loads((BACKUP / (component + "-staged.json")).read_text())
    for unit in UNITS[component]:
        target = pathlib.Path("/etc/systemd/system") / unit
        allowed = {staged["unit_hashes"][unit], baseline[unit]["sha256"]}
        if target.is_file() and digest(target) not in allowed:
            raise ValueError("unit was changed by another deployment; manual review required")
    for unit in reversed(UNITS[component]):
        run(["systemctl", "stop", unit], timeout=2550, check=False)
    for unit, old in baseline.items():
        target = pathlib.Path("/etc/systemd/system") / unit
        if old["existed"]:
            shutil.copy2(str(BACKUP / "units" / unit), str(target))
            os.chmod(str(target), 0o644)
        else:
            run(["systemctl", "disable", unit], check=False)
            if target.is_file():
                target.unlink()
    run(["systemctl", "daemon-reload"])
    for unit, old in baseline.items():
        if old["enabled"]:
            run(["systemctl", "enable", unit])
        if old["active"]:
            run(["systemctl", "start", unit])
    if component == "x":
        restore_x_tunnel_if_previously_active()
    result = {"phase": "units_restored_data_preserved", "component": component,
              "note": "No US service starts automatically. Preserve /data manifests for reconciliation."}
    write_json(BACKUP / (component + "-rollback.json"), result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["stage", "verify", "activate", "rollback"])
    parser.add_argument("--component", choices=["x", "ad"], required=True)
    parser.add_argument("--repo", type=pathlib.Path)
    parser.add_argument("--sha")
    parser.add_argument("--cutover-approved")
    parser.add_argument("--upstream-paused", action="store_true")
    parser.add_argument("--source-ad-stopped", action="store_true")
    args = parser.parse_args()
    if args.action in {"activate", "rollback"}:
        require_cutover(args.cutover_approved, args.upstream_paused)
    if args.action == "stage":
        if args.repo is None:
            parser.error("--repo and --sha are required for stage")
        result = stage(args.repo, args.sha, args.component)
    elif args.action == "verify":
        result = verify(args.component)
    elif args.action == "activate":
        result = activate(args.component, args.source_ad_stopped)
    else:
        result = rollback(args.component)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
