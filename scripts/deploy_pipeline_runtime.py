#!/usr/bin/env python3
"""Prepare verified runtime overlays; apply only to already stopped services.

This helper does not stop/start services, edit environment files, enable workers,
or restore ledgers. The cutover owner freezes admission and drains each lane.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import subprocess
import tempfile


BASE = "6700188"
FULL_COPY = {"app.py", "static/index.html", "static/quick-nav.js"}
NODE_ONLY = {"features/fb_gpu/prepare_worker.py"}
APP_ADDITIONS = {"static/pipeline-monitor.html", "static/pipeline-monitor.css", "static/pipeline-monitor.js"}


def run(args, *, cwd=None, data=None):
    result = subprocess.run([str(v) for v in args], cwd=cwd, input=data, capture_output=True, timeout=120)
    if result.returncode:
        # Command stderr may contain private runtime content; never print it.
        raise RuntimeError("command_failed:" + str(args[0]))
    return result.stdout


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inside(root, relative):
    pure = PurePosixPath(relative)
    if pure.is_absolute() or not pure.parts or any(p in {"", ".", ".."} for p in pure.parts):
        raise RuntimeError("unsafe_relative_path")
    target = root.joinpath(*pure.parts)
    if not target.resolve().is_relative_to(root.resolve()):
        raise RuntimeError("overlay_symlink_escapes_candidate:" + relative)
    return target


def save(folder, manifest):
    target = folder / "manifest.json"
    temporary = target.with_suffix(".next")
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(target)


def atomic_copy(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    current_stat = target.stat() if target.exists() else target.parent.stat()
    fd, temporary = tempfile.mkstemp(prefix=".pipeline-", dir=target.parent)
    os.close(fd)
    temporary = Path(temporary)
    try:
        shutil.copy2(source, temporary)
        if hasattr(os, "chown"):
            os.chown(temporary, current_stat.st_uid, current_stat.st_gid)
        temporary.chmod(current_stat.st_mode & 0o777 if target.exists() else 0o644)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def preserve_tree_owners(source, target):
    """copytree preserves modes but not owners, including private runtime files."""
    if not hasattr(os, "chown"):
        return
    for current, directories, files in os.walk(source, followlinks=False):
        for child in [Path(current), *(Path(current) / name for name in directories + files)]:
            copied = target / child.relative_to(source)
            if not copied.exists() and not copied.is_symlink():
                continue
            stat = child.lstat()
            os.chown(copied, stat.st_uid, stat.st_gid, follow_symlinks=False)


def verify_source(source, commit):
    head = run(["git", "rev-parse", "HEAD"], cwd=source).decode().strip()
    wanted = run(["git", "rev-parse", commit + "^{commit}"], cwd=source).decode().strip()
    if head != wanted or run(["git", "status", "--porcelain"], cwd=source).strip():
        raise RuntimeError("source_must_be_clean_exact_commit")
    origin = run(["git", "remote", "get-url", "origin"], cwd=source).decode().strip()
    if not (origin.startswith("git@github.com:") or origin.startswith("https://github.com/") or origin.startswith("ssh://git@github.com/")):
        raise RuntimeError("source_origin_must_be_github")
    refs = run(["git", "for-each-ref", "--contains", head, "--format=%(refname)", "refs/remotes/origin"], cwd=source)
    if not refs.strip():
        raise RuntimeError("source_commit_not_in_fetched_origin")
    return head


def source_files(source, commit, prefix):
    return run(["git", "ls-tree", "-r", "--name-only", commit, "--", prefix], cwd=source).decode().splitlines()


def build_plan(baseline, lanes=None):
    plan, occupied = {}, {}
    for key, record in baseline.items():
        lane, relative = key.split("/", 1)
        if lanes and lane not in lanes:
            continue
        target = Path(record["runtime_path"])
        if not target.is_absolute():
            raise RuntimeError("runtime_path_must_be_absolute")
        root = target
        for _ in PurePosixPath(relative).parts:
            root = root.parent
        if root.joinpath(*PurePosixPath(relative).parts) != target:
            raise RuntimeError("runtime_relative_path_mismatch")
        entry = plan.setdefault(lane, {"root": root, "records": {}})
        if entry["root"] != root:
            raise RuntimeError("lane_has_multiple_runtime_roots:" + lane)
        resolved = str(target.resolve())
        if resolved in occupied and occupied[resolved] != key:
            raise RuntimeError("cross_lane_runtime_path_collision:" + key)
        occupied[resolved] = key
        entry["records"][relative] = record
    if not plan or (lanes and set(lanes) != set(plan)):
        raise RuntimeError("requested_lane_missing_from_baseline")
    return plan


def verify_baseline(plan):
    for lane, entry in plan.items():
        for relative, record in entry["records"].items():
            target = entry["root"] / relative
            if record.get("missing"):
                if target.exists() or target.is_symlink():
                    raise RuntimeError("runtime_drift_new_file:" + lane + "/" + relative)
            elif not target.is_file() or sha(target) != record["sha256"]:
                raise RuntimeError("runtime_drift:" + lane + "/" + relative)


def prepare(source, commit, baseline_path, folder, *, base=BASE, lanes=None):
    source, folder = Path(source).resolve(), Path(folder).resolve()
    commit = verify_source(source, commit)
    base = run(["git", "rev-parse", base + "^{commit}"], cwd=source).decode().strip()
    baseline = json.loads(Path(baseline_path).read_text(encoding="utf-8-sig"))
    plan = build_plan(baseline, lanes)
    for entry in plan.values():
        runtime = entry["root"].resolve()
        if folder.is_relative_to(runtime) or runtime.is_relative_to(folder):
            raise RuntimeError("stage_and_runtime_roots_must_be_separate")
    verify_baseline(plan)
    folder.mkdir(parents=True, exist_ok=False, mode=0o700)
    (folder / "backups").mkdir(mode=0o700)
    package = source_files(source, commit, "features/pipeline_dispatch")
    if not package:
        raise RuntimeError("dispatch_package_missing")
    manifest = {"version": 1, "commit": commit, "base": base, "status": "preparing", "lanes": {}}
    save(folder, manifest)
    for lane, entry in plan.items():
        root = entry["root"]
        candidate = folder / "candidates" / lane
        backup = folder / "backups" / lane
        candidate.parent.mkdir(exist_ok=True)
        backup.mkdir(parents=True)
        # app is a long-lived working tree with unrelated assets; stage only
        # its exact overlays. Sidecars retain their complete existing release.
        if root.is_symlink():
            shutil.copytree(root.resolve(), candidate, symlinks=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git"))
            preserve_tree_owners(root.resolve(), candidate)
        else:
            candidate.mkdir()
        item = {"runtime_root": str(root), "previous_root": str(root.resolve()), "symlink": root.is_symlink(),
                "candidate": str(candidate), "files": {}, "baseline": entry["records"], "status": "preparing"}
        manifest["lanes"][lane] = item
        additions = set(package) | (APP_ADDITIONS if lane == "app" else set())
        for relative in sorted(set(entry["records"]) | additions):
            if relative in NODE_ONLY:
                continue
            source_path = inside(source, relative)
            live = root / relative
            target = inside(candidate, relative)
            baseline_record = entry["records"].get(relative)
            if baseline_record is None and live.exists():
                raise RuntimeError("unbaselined_addition_already_exists:" + lane + "/" + relative)
            if not source_path.is_file():
                raise RuntimeError("overlay_source_missing:" + relative)
            existed = live.is_file()
            if existed:
                saved = inside(backup, relative)
                saved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(live, saved)
            target.parent.mkdir(parents=True, exist_ok=True)
            if not existed or (lane == "app" and relative in FULL_COPY):
                shutil.copy2(source_path, target)
                strategy = "new" if not existed else "reviewed_full_copy"
            else:
                patch = run(["git", "diff", "--binary", "--full-index", base, commit, "--", relative], cwd=source)
                if not patch:
                    continue
                if not root.is_symlink():
                    shutil.copy2(live, target)
                # No 3-way guessing or whole-file fallback: any live conflict
                # stops preparation and is reviewed before a new baseline.
                try:
                    run(["git", "apply", "--check", "--whitespace=nowarn", "-"], cwd=candidate, data=patch)
                    run(["git", "apply", "--whitespace=nowarn", "-"], cwd=candidate, data=patch)
                except RuntimeError:
                    raise RuntimeError("runtime_patch_conflict:" + lane + "/" + relative) from None
                strategy = "base_to_commit_patch"
            if relative.endswith(".py"):
                compile(target.read_bytes(), relative, "exec")
            item["files"][relative] = {"existed": existed, "before_sha256": sha(live) if existed else "",
                                       "after_sha256": sha(target), "strategy": strategy}
        item["status"] = "prepared"
        save(folder, manifest)
    # Detect a runtime change that happened while candidate copies were made.
    verify_baseline(plan)
    manifest["status"] = "prepared"
    save(folder, manifest)
    return {"status": "prepared", "manifest": str(folder / "manifest.json"), "lanes": {k: len(v["files"]) for k, v in manifest["lanes"].items()}}


def assert_stopped(units):
    if not units:
        raise RuntimeError("lane_service_units_required")
    for unit in units:
        if not isinstance(unit, str) or not unit.endswith(".service") or "/" in unit:
            raise RuntimeError("invalid_service_unit")
        state = run(["systemctl", "show", unit, "-p", "ActiveState", "--value"]).decode().strip()
        if state not in {"inactive", "failed"}:
            raise RuntimeError("lane_not_drained_and_stopped:" + unit)
        if run(["systemctl", "show", unit, "-p", "LoadState", "--value"]).decode().strip() == "not-found":
            raise RuntimeError("service_unit_not_found:" + unit)


def backup_ledgers(folder, lane, paths):
    if not paths:
        raise RuntimeError("lane_ledger_paths_required")
    backups = []
    for index, value in enumerate(paths):
        path = Path(value).resolve(strict=True)
        target = folder / "backups" / lane / ("ledger-%d.sqlite3" % index)
        if target.exists():
            raise RuntimeError("ledger_backup_already_exists")
        with contextlib.closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as source, contextlib.closing(sqlite3.connect(target)) as destination:
            source.backup(destination)
            if destination.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError("ledger_backup_invalid")
        target.chmod(0o600)
        backups.append({"source": str(path), "backup": str(target), "sha256": sha(target)})
    return backups


def apply(folder, lane, controls, *, rollback=False):
    folder = Path(folder).resolve(strict=True)
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    item = manifest["lanes"][lane]
    root, candidate = Path(item["runtime_root"]), Path(item["candidate"])
    config = controls.get(lane) or {}
    assert_stopped(config.get("units"))
    expected_status = "applied" if rollback else "prepared"
    if item["status"] != expected_status:
        raise RuntimeError("lane_state_invalid:" + item["status"])
    expected_root = str(candidate.resolve()) if rollback and item["symlink"] else item["previous_root"]
    if str(root.resolve()) != expected_root:
        raise RuntimeError("runtime_release_changed")
    if not rollback:
        verify_baseline({lane: {"root": root, "records": item["baseline"]}})
    for relative, record in item["files"].items():
        live = inside(root, relative)
        expected = record["after_sha256"] if rollback else record["before_sha256"]
        if expected and (not live.is_file() or sha(live) != expected):
            raise RuntimeError("runtime_file_changed:" + relative)
        if not expected and (live.exists() or live.is_symlink()):
            raise RuntimeError("runtime_new_file_collision:" + relative)
        if not rollback and sha(inside(candidate, relative)) != record["after_sha256"]:
            raise RuntimeError("candidate_changed:" + relative)
    if not rollback:
        item["ledger_backups"] = backup_ledgers(folder, lane, config.get("ledgers"))
        save(folder, manifest)
    # Verify service state again immediately before the first runtime mutation.
    assert_stopped(config.get("units"))
    if item["symlink"]:
        # The candidate becomes executable by the existing service user; the
        # manifest and backups retain their private modes.
        folder.chmod(0o755)
        next_link = root.with_name(root.name + ".pipeline-next")
        if next_link.exists() or next_link.is_symlink():
            raise RuntimeError("temporary_release_link_exists")
        next_link.symlink_to(item["previous_root"] if rollback else candidate, target_is_directory=True)
        next_link.replace(root)
    else:
        for relative, record in item["files"].items():
            live = inside(root, relative)
            if rollback and not record["existed"]:
                live.unlink()
            else:
                source = folder / "backups" / lane / relative if rollback else candidate / relative
                atomic_copy(source, live)
    item["status"] = "rolled_back" if rollback else "applied"
    manifest["status"] = "mixed" if len({v["status"] for v in manifest["lanes"].values()}) > 1 else item["status"]
    save(folder, manifest)
    return {"status": item["status"], "lane": lane, "services_restarted": False, "ledgers_restored": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    prepare_parser = sub.add_parser("prepare")
    for name in ("source", "commit", "baseline", "folder"):
        prepare_parser.add_argument("--" + name, required=True)
    prepare_parser.add_argument("--base", default=BASE)
    prepare_parser.add_argument("--lanes", help="Comma-separated lane names; default all baseline lanes")
    for action in ("apply", "rollback"):
        operation = sub.add_parser(action)
        for name in ("folder", "lane", "controls"):
            operation.add_argument("--" + name, required=True)
    args = parser.parse_args()
    if args.action == "prepare":
        result = prepare(args.source, args.commit, args.baseline, args.folder, base=args.base, lanes=args.lanes.split(",") if args.lanes else None)
    else:
        controls = json.loads(Path(args.controls).read_text(encoding="utf-8-sig"))
        result = apply(args.folder, args.lane, controls, rollback=args.action == "rollback")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
