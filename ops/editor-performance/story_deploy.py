"""Stage the independent editor; separately select the default for new creations."""

import argparse
from contextlib import closing
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess

import deploy as editor

FILES = [
    "video-editing-ai-story.js",
    "video-editing-ai-story.css",
    "video-editing-ai-production.js",
    "video-editing-ai-studio.js",
    "video-editing-ai.html",
]


def write(path, obj):
    editor.atomic(path, json.dumps(obj, indent=2).encode())


def resume(manifest):
    record = json.loads(manifest.read_text())
    if record["state"] == "prepared" or record["state"] == "switching":
        p = Path(record["repo"]) / "ops/editor-gpu-cos/deploy.py"
        spec = importlib.util.spec_from_file_location("compatible_editor_cpu_switch", p)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if editor.CURRENT.resolve() == Path(record["previous"]):
            module.switch(record, manifest)
        elif (
            editor.CURRENT.resolve() != Path(record["release"])
            or editor.cmd("systemctl", "is-active", editor.UNIT) != "active"
        ):
            raise RuntimeError("Check active independent editor before resuming")
        record["state"] = "api_active"
        write(manifest, record)
    if record.get("static_manifest") or record.get("static_verified"):
        return
    static_base = record.get("static_base", record["base"])
    changed = [
        name
        for name in FILES
        if editor.blob(record["repo"], static_base, "static/" + name)
        != editor.blob(record["repo"], record["version"], "static/" + name)
    ]
    if not changed:
        for dest in editor.STATIC:
            for name in FILES:
                if editor.read(dest / name) != editor.blob(
                    record["repo"], record["version"], "static/" + name
                ):
                    raise RuntimeError("Concurrent editor static change: " + str(dest / name))
        record.update(state="compatible", static_verified=True)
        write(manifest, record)
        print(json.dumps({"state": "compatible", "manifest": str(manifest)}))
        return
    cmd = [
        "python3",
        str(Path(record["repo"]) / "ops/editor-performance/static_update.py"),
        "install",
        "--repo",
        record["repo"],
        "--version",
        record["version"],
        "--base",
        static_base,
        "--files",
        *changed,
    ]
    result = json.loads(subprocess.check_output(cmd, text=True))
    record["state"] = "compatible"
    record["static_manifest"] = result["manifest"]
    write(manifest, record)
    print(json.dumps({"state": "compatible", "manifest": str(manifest)}))


def stage(args):
    repo = args.repo.resolve()
    if editor.cmd("git", "-C", repo, "rev-parse", "HEAD") != args.version or editor.cmd(
        "git", "-C", repo, "status", "--porcelain"
    ):
        raise RuntimeError("Need a clean exact checkout")
    if not editor.cmd("git", "-C", repo, "branch", "-r", "--contains", args.version):
        raise RuntimeError("Fetch GitHub commit first")
    previous = editor.CURRENT.resolve()
    if previous.name != args.base:
        raise RuntimeError("Concurrent independent editor release")
    static_base = args.static_base or args.base
    for dest in editor.STATIC:
        for name in FILES:
            if editor.read(dest / name) != editor.blob(repo, static_base, "static/" + name):
                raise RuntimeError("Concurrent editor static change: " + str(dest / name))
    folder = editor.ROOT / "backups" / ("story-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    folder.mkdir(parents=True, mode=0o700)
    with closing(sqlite3.connect(editor.DB.as_uri() + "?mode=ro", uri=True)) as src:
        with closing(sqlite3.connect(folder / "before-editor.sqlite3")) as dst:
            src.backup(dst)
    (folder / "before-editor.sqlite3").chmod(0o600)
    release = editor.prepare_release(repo, args.base, args.version, previous)
    record = {
        "role": "cpu",
        "current": str(editor.CURRENT),
        "previous": str(previous),
        "release": str(release),
        "repo": str(repo),
        "base": args.base,
        "static_base": static_base,
        "version": args.version,
        "state": "prepared",
    }
    manifest = folder / "manifest.json"
    write(manifest, record)
    print("prepared_manifest=" + str(manifest), flush=True)
    resume(manifest)


def default(manifest, version):
    record = json.loads(manifest.read_text())
    if editor.CURRENT.resolve() != Path(record["release"]) or not (
        record.get("static_manifest") or record.get("static_verified")
    ):
        raise RuntimeError("Verify active compatible release and static resources")
    with closing(sqlite3.connect(editor.DB)) as db:
        with db:
            changed = db.execute(
                "UPDATE studio_story_defaults SET version=? WHERE singleton=1", (version,)
            ).rowcount
            if changed != 1:
                raise RuntimeError("New-session default missing")
    record["new_session_version"] = version
    write(manifest, record)
    print(json.dumps({"new_session_version": version, "manifest": str(manifest)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["stage", "resume", "enable", "rollback-default"])
    parser.add_argument("--repo", type=Path)
    parser.add_argument("--base")
    parser.add_argument("--static-base")
    parser.add_argument("--version")
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args()
    if args.action == "stage":
        stage(args)
    elif args.action == "resume":
        resume(args.manifest)
    else:
        default(args.manifest, 2 if args.action == "enable" else 1)
