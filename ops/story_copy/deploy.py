"""Stage exact GitHub code, test against the active lane variants, then switch only affected owners."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

SRC = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SRC))
from ops.story_copy import integrate
from features.story_copy import store

ROOTS = {
    "fb": Path("/opt/fb-auto-post/current"),
    "x": Path("/opt/x-post-automation/current"),
    "tag": Path("/opt/x-tag-publisher/current"),
}
MODULES = {
    "fb": {
        "features/fb_auto_posts/core.py": integrate.fb_auto,
        "features/fb_video_publish/core.py": integrate.fb_video,
        "features/fb_auto_posts/publisher.py": integrate.fb_redirect,
        "features/pipeline_dispatch/fb_native.py": integrate.fb_redirect,
    },
    "x": {
        "features/x_posts/service.py": integrate.x_service,
        "features/pipeline_dispatch/x_native.py": integrate.x_native,
    },
    "tag": {
        "features/x_posts/service.py": integrate.x_service,
        "features/pipeline_dispatch/x_native.py": integrate.x_native,
    },
}
CENTRAL = Path("/opt/story-copy/current")
HEALTH = "/opt/publish-scheduler-health/current/scripts/publish_scheduler_health.py"
PUBLIC = Path("/usr/share/nginx/html")
MAIN = Path("/root/drama_material_service")
AUX = ("story-copy-settings.service", "story-copy-worker.service")
OWNERS = {"x": ("x-post-automation.service", 8810), "tag": ("x-tag-post.service", 8821)}
TRIGGERS = {
    "x": [
        "x-post-schedule.timer",
        "x-post-schedule-claim.timer",
        "x-post-manual.timer",
        "x-auto-post-scheduler.timer",
    ],
    "tag": [
        "x-tag-auto-scheduler.timer",
        "x-tag-auto-runner.timer",
        "x-tag-auto-runner.path",
        "x-tag-pool.timer",
        "x-tag-manual.timer",
    ],
}


def run(*cmd, **kwargs):
    return subprocess.run([str(x) for x in cmd], check=True, **kwargs)


def read(*cmd):
    return run(*cmd, stdout=subprocess.PIPE, text=True).stdout.strip()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def atomic(p, data):
    p.parent.mkdir(parents=True, exist_ok=True)
    metadata = p.stat() if p.exists() else None
    with tempfile.NamedTemporaryFile(dir=p.parent, delete=False) as f:
        f.write(data)
        tmp = Path(f.name)
    tmp.chmod(metadata.st_mode & 0o777 if metadata else 0o644)
    if metadata:
        os.chown(tmp, metadata.st_uid, metadata.st_gid)
    tmp.replace(p)


def symlink(link, target):
    link.parent.mkdir(parents=True, exist_ok=True)
    tmp = link.parent / (".story-copy-next-" + str(os.getpid()))
    tmp.symlink_to(target)
    tmp.replace(link)


def save(folder, data):
    atomic(folder / "record.json", json.dumps(data, indent=2).encode())


def stage_file(folder, rec, path, data):
    key = str(path)
    if key in rec["files"]:
        return
    digest = sha(key.encode())[:18]
    original = path.read_bytes() if path.exists() else None
    if original == data:
        return
    if original is not None:
        atomic(folder / "backup" / digest, original)
    atomic(folder / "staged" / digest, data)
    rec["files"][key] = {
        "name": digest,
        "before": sha(original) if original is not None else None,
        "after": sha(data),
    }


def prepare(folder):
    if (
        read("findmnt", "-n", "-o", "UUID", "--mountpoint", "/mnt/data-disk")
        != "3e8ac4e8-7770-456d-9e89-2ec5dd405fa8"
    ):
        raise RuntimeError("data disk is not mounted")
    sha1 = read("git", "-C", SRC, "rev-parse", "HEAD")
    if read("git", "-C", SRC, "status", "--porcelain", "--untracked-files=no"):
        raise RuntimeError("unclean source")
    upstream = read(
        "git",
        "-C",
        SRC,
        "ls-remote",
        "git@github.com:gaoyuan12356416/ai-.git",
        "refs/heads/codex/four-endpoint-ai-captions-20261116",
    ).split()
    if not upstream or upstream[0] != sha1:
        raise RuntimeError("GitHub commit mismatch")
    folder.mkdir(parents=True, exist_ok=False, mode=0o755)
    rec = {
        "commit": sha1,
        "source": str(SRC),
        "files": {},
        "roots": {},
        "central_before": str(CENTRAL.resolve()) if CENTRAL.is_symlink() else None,
        "active": [],
    }
    central = folder / "central"
    (central / "features").mkdir(parents=True)
    shutil.copytree(
        SRC / "features/story_copy",
        central / "features/story_copy",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    for lane, link in ROOTS.items():
        origin = link.resolve()
        candidate = folder / ("candidate-" + lane)
        shutil.copytree(
            origin,
            candidate,
            symlinks=True,
            copy_function=os.link,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git"),
        )
        package = candidate / "features/story_copy"
        if package.exists():
            shutil.rmtree(package)
        shutil.copytree(central / "features/story_copy", package)
        files = {}
        for name, fn in MODULES[lane].items():
            path = candidate / name
            old = path.read_bytes()
            new = fn(old.decode()).encode()
            compile(new, name, "exec")
            if old != new:
                atomic(path, new)
                files[name] = {"before": sha(old), "after": sha(new)}
        for name, ep in integrate.PAGES.items():
            target = candidate / "static" / name
            if target.exists() and (
                (lane == "fb" and ep == "fb") or (lane != "fb" and ep != "fb")
            ):
                atomic(target, integrate.html(target.read_text(), ep).encode())
        if (candidate / "static").is_dir():
            atomic(
                candidate / "static/story-copy-settings.js",
                (SRC / "static/story-copy-settings.js").read_bytes(),
            )
        rec["roots"][lane] = {
            "before_root": str(origin),
            "candidate": str(candidate),
            "files": files,
            "state": "prepared",
        }
    for root in (PUBLIC, MAIN / "static"):
        for name, endpoint in integrate.PAGES.items():
            p = root / name
            if p.exists():
                stage_file(
                    folder, rec, p, integrate.html(p.read_text(), endpoint).encode()
                )
        stage_file(
            folder,
            rec,
            root / "story-copy-settings.js",
            (SRC / "static/story-copy-settings.js").read_bytes(),
        )
    for n in AUX:
        stage_file(
            folder,
            rec,
            Path("/etc/systemd/system") / n,
            (SRC / "deploy" / n).read_bytes(),
        )
    stage_file(
        folder,
        rec,
        Path("/etc/nginx/default.d/story-copy.conf"),
        (SRC / "deploy/story-copy.nginx.conf").read_bytes(),
    )
    save(folder, rec)
    print("prepared", folder, sha1, flush=True)


def check_files(rec, reverse):
    for name, meta in rec["files"].items():
        p = Path(name)
        digest = sha(p.read_bytes()) if p.exists() else None
        if digest not in (meta["before"], meta["after"]):
            raise RuntimeError("preserving concurrent change " + name)


def install_files(folder, rec, reverse):
    for name, meta in rec["files"].items():
        path = Path(name)
        if reverse and meta["before"] is None:
            if path.exists() and sha(path.read_bytes()) == meta["after"]:
                path.unlink()
        else:
            atomic(
                path,
                (
                    folder / ("backup" if reverse else "staged") / meta["name"]
                ).read_bytes(),
            )


def state(unit):
    return read("systemctl", "show", "-p", "ActiveState", "--value", unit)


def sched(folder, mode, lane, reverse=False):
    base = folder / (
        ("rollback-" if reverse else "") + lane + "-schedulers-before.json"
    )
    cmd = [sys.executable, HEALTH, mode]
    cmd += (
        ["--lane", lane, "--output", base]
        if mode == "snapshot"
        else [
            "--baseline",
            base,
            "--wait-seconds",
            "25",
            "--output",
            base.with_name(base.name.replace("before", "after")),
        ]
    )
    proc = subprocess.run(list(map(str, cmd)), capture_output=True, text=True)
    if proc.returncode:
        print("scheduler", mode, lane, proc.returncode, flush=True)
    return proc.returncode


def switch_lane(folder, rec, lane, reverse):
    item = rec["roots"][lane]
    old = Path(item["candidate" if reverse else "before_root"])
    new = Path(item["before_root" if reverse else "candidate"])
    link = ROOTS[lane]
    if link.resolve() == new:
        return
    if link.resolve() != old:
        raise RuntimeError("concurrent release " + lane)
    for name, hashes in item["files"].items():
        if sha((old / name).read_bytes()) != hashes["after" if reverse else "before"]:
            raise RuntimeError("concurrent source " + name)
    if lane == "fb":
        from scripts import deploy_publish_queue_isolation as ops

        f = folder / "fb-switch"
        f.mkdir(exist_ok=True)
        if not (f / "manifest.json").exists():
            atomic(f / "manifest.json", json.dumps({"lanes": {"fb": item}}).encode())
        ops.switch(f, "fb", reverse, preserve_queue_scopes=True)
        return
    owner, port = OWNERS[lane]
    # The installed ExecStop drains accepted uploads while the server still handles them.
    if "pipeline_service_drain.py" not in read(
        "systemctl", "show", owner, "-p", "ExecStop", "--value"
    ):
        raise RuntimeError("X owner drain unavailable")
    triggers = [u for u in TRIGGERS[lane] if state(u) == "active"]
    service_active = state(owner) == "active"
    if triggers:
        run("systemctl", "stop", *triggers)
    try:
        if service_active:
            run("systemctl", "--job-mode=ignore-dependencies", "stop", owner)
        if link.resolve() != old:
            raise RuntimeError("concurrent runtime release")
        symlink(link, new)
        if service_active:
            run("systemctl", "start", owner)
            for _ in range(45):
                try:
                    with urllib.request.urlopen(
                        "http://127.0.0.1:%d/health" % port, timeout=2
                    ) as res:
                        if res.status == 200:
                            break
                except OSError:
                    time.sleep(1)
            else:
                raise RuntimeError("owner health failed")
    except BaseException:
        if link.resolve() == new:
            if service_active:
                run("systemctl", "--job-mode=ignore-dependencies", "stop", owner)
            symlink(link, old)
        if service_active:
            run("systemctl", "start", owner)
        raise
    finally:
        if triggers:
            run("systemctl", "start", *triggers)


def schema_and_access():
    store.initialize()
    for path in {store.SOURCES["xm"][1], store.SOURCES["fa"][1], store.TAG_POSTS}:
        with store.connection(path, timeout=8) as c:
            c.execute(store.RECEIPTS)
    users = ["fb-auto-post", "x-post-automation", "x-post-daily"]
    # Readers need access only to the separate caption ledger, not to Codex credentials.
    for u in users:
        run("setfacl", "-m", "u:" + u + ":rx,d:u:" + u + ":rx", store.ROOT)
        for p in store.ROOT.glob("captions.sqlite3*"):
            run("setfacl", "-m", "u:" + u + ":r", p)


def activate(folder, reverse):
    rec = json.loads((folder / "record.json").read_text())
    check_files(rec, reverse)
    for lane in ("fb", "x"):
        sched(folder, "snapshot", lane, reverse)
    if not reverse:
        for lane, item in rec["roots"].items():
            if ROOTS[lane].resolve() not in (
                Path(item["before_root"]),
                Path(item["candidate"]),
            ):
                raise RuntimeError("restage after concurrent " + lane + " release")
        schema_and_access()
        symlink(CENTRAL, folder / "central")
    try:
        for lane in ("tag", "x", "fb") if reverse else ("fb", "tag", "x"):
            print("switching", lane, flush=True)
            switch_lane(folder, rec, lane, reverse)
            if not reverse and lane not in rec["active"]:
                rec["active"].append(lane)
            if reverse and lane in rec["active"]:
                rec["active"].remove(lane)
            save(folder, rec)
    finally:
        for lane in ("fb", "x"):
            sched(folder, "restore", lane, reverse)
    if not reverse:
        install_files(folder, rec, False)
        run("systemctl", "daemon-reload")
        run("systemctl", "enable", "--now", *AUX)
        run("nginx", "-t")
        run("systemctl", "reload", "nginx")
    if reverse:
        run("systemctl", "disable", "--now", *AUX)
        install_files(folder, rec, True)
        if rec["central_before"]:
            symlink(CENTRAL, rec["central_before"])
        elif CENTRAL.is_symlink():
            CENTRAL.unlink()
        run("systemctl", "daemon-reload")
        run("nginx", "-t")
        run("systemctl", "reload", "nginx")
    rec["state"] = "rolled_back" if reverse else "active"
    save(folder, rec)
    print(rec["state"], rec["commit"], flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=("prepare", "apply", "rollback"))
    ap.add_argument("folder", type=Path)
    a = ap.parse_args()
    folder = a.folder.resolve()
    if not folder.is_relative_to("/mnt/data-disk/story-copy-deploy"):
        raise SystemExit("unexpected deployment directory")
    if a.action == "prepare":
        prepare(folder)
    else:
        activate(folder, a.action == "rollback")
