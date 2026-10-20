#!/usr/bin/env python3
"""Narrow composite-app installer. Never starts services or restores databases."""
import argparse
import ast
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import subprocess
import time

APP = Path("/root/drama_material_service/app.py")
CORE = APP.parent / "features/cover_material_source.py"
SOURCE_DB = APP.parent / "data/drama_material_jobs.sqlite3"
ROOT = Path("/mnt/data-disk/ai-cover-material-source")
SYNC_DB = Path("/mnt/data-disk/youtube-cover-material-sync/sync.sqlite3")
UNITS_DIR = Path("/etc/systemd/system")
YOUTUBE_CURRENT = Path("/opt/youtube-cover-material-sync/current")
REPORT_CURRENT = Path("/opt/ai-image-daily/current")
APP_UNIT = "drama-material-api.service"
UNITS = ("youtube-cover-material-sync.service", "youtube-cover-material-sync.timer",
         "ai-image-daily.service", "ai-image-daily.timer")
DISK_UUID = "3e8ac4e8-7770-456d-9e89-2ec5dd405fa8"
CALLBACK = "post_screenshot_ai_source_callback"
BRIDGE = """def post_screenshot_ai_source_callback(job):
    from features.cover_material_source import upload_screenshot
    return upload_screenshot(job)"""
MODULE = "ops/ai-cover-custom-source"


def digest(value):
    return hashlib.sha256(value).hexdigest()


def output(*args):
    return subprocess.check_output(args, stderr=subprocess.PIPE)


def call(*args):
    subprocess.run(args, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def atomic_write(path, value, mode=0o600):
    path = Path(path)
    if path.is_symlink():
        raise RuntimeError("refuse_symlink_file")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".deploy-" + str(os.getpid()))
    original = path.stat() if path.exists() else None
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
            if original:
                os.chmod(temporary, original.st_mode & 0o7777)
                if hasattr(os, "fchown"):
                    os.fchown(stream.fileno(), original.st_uid, original.st_gid)
        os.replace(temporary, path)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def save_meta(backup, meta):
    atomic_write(Path(backup) / "deployment.json", encoded(meta))


def function_node(text):
    tree = ast.parse(text)
    matches = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
               and node.name == CALLBACK]
    if len(matches) != 1 or matches[0].decorator_list or not isinstance(matches[0], ast.FunctionDef):
        raise RuntimeError("callback_identity_not_unique_or_decorated")
    return tree, matches[0]


def without_callback(text):
    tree, target = function_node(text)
    tree.body.remove(target)
    return ast.dump(tree, include_attributes=False)


def compose_app(original, release_app, baseline):
    """Keep exact surrounding bytes; separately prove the remaining AST unchanged."""
    if digest(original) != baseline["app_sha256"]:
        raise RuntimeError("production_app_sha_drift")
    text = original.decode("utf-8")
    _tree, old = function_node(text)
    old_segment = ast.get_source_segment(text, old)
    if digest(old_segment.encode("utf-8")) != baseline["old_callback_sha256"]:
        raise RuntimeError("production_callback_sha_drift")
    new_text = release_app.decode("utf-8")
    _tree, new = function_node(new_text)
    expected = ast.dump(ast.parse(BRIDGE).body[0], include_attributes=False)
    if ast.dump(new, include_attributes=False) != expected:
        raise RuntimeError("release_callback_not_shared_bridge")
    # FunctionDef offsets exclude any trailing newline. Preserve its old newline.
    lines = text.splitlines(keepends=True)
    start = sum(len(line) for line in lines[:old.lineno - 1])
    # end_col_offset is bytes. The callback's last line can contain Unicode.
    final_line = lines[old.end_lineno - 1].encode("utf-8")[:old.end_col_offset].decode("utf-8")
    end = sum(len(line) for line in lines[:old.end_lineno - 1]) + len(final_line)
    replacement = ast.get_source_segment(new_text, new)
    newline = "\r\n" if "\r\n" in text else "\n"
    replacement = replacement.replace("\r\n", "\n").replace("\n", newline)
    result = (text[:start] + replacement + text[end:]).encode("utf-8")
    compile(result, str(APP), "exec")
    before, after = without_callback(text), without_callback(result.decode("utf-8"))
    if before != after:
        raise RuntimeError("non_callback_ast_changed")
    return result, {"original_sha256": digest(original), "installed_sha256": digest(result),
                    "callback_sha256": digest(replacement.encode("utf-8")),
                    "unchanged_ast_sha256": digest(before.encode("utf-8"))}


def environment_ok(require_backup_space=False):
    if os.name != "posix" or os.geteuid() != 0 or socket.gethostname().lower() != "vm-0-108-centos":
        raise RuntimeError("expected_cpu_root_host_required")
    mounted = output("findmnt", "-n", "-o", "UUID", "--mountpoint", "/mnt/data-disk").decode().strip()
    if mounted != DISK_UUID:
        raise RuntimeError("data_disk_uuid_mismatch")
    if require_backup_space:
        database_bytes = SOURCE_DB.stat().st_size
        database_bytes += sum(path.stat().st_size for path in (SYNC_DB, ROOT / "upload.sqlite3")
                              if path.exists())
        required = database_bytes * 2 + 1024 ** 3
        if shutil.disk_usage("/mnt/data-disk").free < required:
            raise RuntimeError("insufficient_backup_disk_space")


def validate_release(repo, release, commit):
    repo, release = Path(repo).resolve(), Path(release).resolve()
    if not re.fullmatch(r"[0-9a-f]{40}", commit) or release.name != commit:
        raise RuntimeError("exact_release_commit_required")
    if output("git", "-C", str(repo), "rev-parse", commit + "^{commit}").decode().strip() != commit:
        raise RuntimeError("commit_not_found")
    remote_refs = output("git", "-C", str(repo), "for-each-ref", "--format=%(refname)",
                         "--contains", commit, "refs/remotes/origin/").decode().strip()
    if not remote_refs:
        raise RuntimeError("commit_not_in_fetched_origin")
    scopes = ("app.py", "features/cover_material_source.py", MODULE,
              "ops/youtube-cover-material-sync", "ops/ai-image-daily")
    records = output("git", "-C", str(repo), "ls-tree", "-r", "-z", commit, "--", *scopes)
    files = {}
    for record in records.split(b"\0"):
        if not record:
            continue
        info, raw_path = record.split(b"\t", 1)
        mode, kind, blob = info.decode().split()
        relative = raw_path.decode("utf-8")
        path = release / relative
        if mode not in ("100644", "100755") or kind != "blob" or path.is_symlink():
            raise RuntimeError("release_regular_files_required")
        expected = digest(output("git", "-C", str(repo), "cat-file", "blob", blob))
        if not path.is_file() or release not in path.resolve().parents or digest(path.read_bytes()) != expected:
            raise RuntimeError("release_file_differs_from_git:" + relative)
        files[relative] = expected
        if relative.endswith(".py"):
            compile(path.read_bytes(), relative, "exec")
    required = {"app.py", "features/cover_material_source.py", MODULE + "/baseline.json",
                MODULE + "/deploy.py", "ops/youtube-cover-material-sync/sync.py",
                "ops/ai-image-daily/report.py", "ops/ai-image-daily/collector.py"}
    required.update(unit_relative(name) for name in UNITS)
    if not required.issubset(files):
        raise RuntimeError("required_release_file_missing")
    baseline = json.loads((release / MODULE / "baseline.json").read_text(encoding="utf-8"))
    for key in ("app_sha256", "old_callback_sha256"):
        if not re.fullmatch(r"[0-9a-f]{64}", baseline[key]):
            raise RuntimeError("invalid_baseline_hash")
    verify_unit_permissions(release)
    return release, baseline, files


def unit_relative(name):
    area = "youtube-cover-material-sync" if name.startswith("youtube-") else "ai-image-daily"
    return "ops/" + area + "/" + name


def verify_unit_permissions(release):
    youtube = (release / unit_relative(UNITS[0])).read_text(encoding="utf-8")
    report = (release / unit_relative(UNITS[2])).read_text(encoding="utf-8")
    checks = ((youtube, "ReadWritePaths=" + str(ROOT)),
              (youtube, "ReadOnlyPaths=" + str(SOURCE_DB)),
              (report, "ReadOnlyPaths=-" + str(ROOT / "upload.sqlite3")))
    for content, required in checks:
        if required not in content.splitlines():
            raise RuntimeError("release_unit_ledger_permissions_missing")


def current_links(baseline):
    result = {}
    for name, path, expected in (("youtube", YOUTUBE_CURRENT, baseline["youtube_release"]),
                                 ("report", REPORT_CURRENT, baseline["report_release"])):
        if not path.is_symlink() or not path.exists() or path.resolve().name != expected:
            raise RuntimeError("previous_release_drift:" + name)
        result[name] = {"path": str(path), "link": os.readlink(path), "target": str(path.resolve())}
    return result


def unit_state(name):
    state = output("systemctl", "show", name, "--property=ActiveState", "--value").decode().strip()
    if state not in ("active", "inactive", "failed", "activating", "deactivating", "reloading"):
        raise RuntimeError("unknown_unit_state:" + name)
    return state


def require_idle():
    for name in (APP_UNIT, *UNITS):
        if unit_state(name) not in ("inactive", "failed"):
            raise RuntimeError("unit_not_drained:" + name)


def backup_sqlite(path, target):
    start = time.monotonic()
    def progress(_status, _remaining, _total):
        if time.monotonic() - start > 120:
            raise RuntimeError("online_backup_timeout")
    with contextlib.closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro",
                                          uri=True, timeout=10)) as source:
        source.execute("PRAGMA query_only=ON")
        with contextlib.closing(sqlite3.connect(target)) as destination:
            source.backup(destination, pages=1024, progress=progress, sleep=0.05)
            if destination.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError("online_backup_quick_check_failed")
    os.chmod(target, 0o600)
    return digest(Path(target).read_bytes())


def read_crontab():
    result = subprocess.run(["crontab", "-l"], capture_output=True)
    if result.returncode not in (0, 1) or (result.returncode == 1 and b"no crontab" not in result.stderr.lower()):
        raise RuntimeError("crontab_snapshot_failed")
    return result.stdout


def backup_optional_ledger(source, target):
    source, target = Path(source), Path(target)
    entry = {"source": str(source), "exists": source.exists(),
             "path": str(target), "sha256": None, "restore_allowed": False}
    if source.is_symlink() and not source.exists():
        raise RuntimeError("ledger_database_symlink_broken")
    if source.exists():
        if not source.is_file():
            raise RuntimeError("ledger_database_not_regular_file")
        # Existing ledgers are mandatory backups: never swallow SQLite or I/O errors.
        entry["sha256"] = backup_sqlite(source, target)
    return entry


def prepare(repo, release, commit):
    environment_ok(require_backup_space=True)
    release, baseline, hashes = validate_release(repo, release, commit)
    composed, proof = compose_app(APP.read_bytes(), (release / "app.py").read_bytes(), baseline)
    links = current_links(baseline)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup = ROOT / "deployments" / stamp
    backup.mkdir(parents=True, exist_ok=False, mode=0o700)
    meta = {"phase": "preparing", "created_at": now(), "commit": commit, "repo": str(Path(repo).resolve()),
            "release": str(release), "baseline": baseline, "release_hashes": hashes, "composition": proof,
            "links": links, "files": {}, "units": {}, "database_restore_allowed": False}
    save_meta(backup, meta)
    files = {"app.py": APP, "cover_material_source.py": CORE}
    files.update({name: UNITS_DIR / name for name in (APP_UNIT, *UNITS)})
    for name, path in files.items():
        if path.is_symlink():
            raise RuntimeError("snapshot_file_is_symlink:" + name)
        item = {"path": str(path), "exists": path.exists(), "sha256": None}
        if path.exists():
            raw = path.read_bytes()
            atomic_write(backup / "files" / name, raw)
            item.update(sha256=digest(raw), mode=path.stat().st_mode & 0o7777)
        meta["files"][name] = item
    for name in (APP_UNIT, *UNITS):
        effective = output("systemctl", "cat", name)
        atomic_write(backup / "effective-units" / name, effective)
        enabled = subprocess.run(["systemctl", "is-enabled", name], capture_output=True, text=True)
        meta["units"][name] = {"active_state": unit_state(name), "enabled": enabled.stdout.strip(),
                               "effective_sha256": digest(effective)}
    cron = read_crontab()
    atomic_write(backup / "root.crontab", cron)
    meta["crontab_sha256"] = digest(cron)
    meta["source_database_backup"] = {"path": str(backup / "source.sqlite3"), "restore_allowed": False,
                                      "sha256": backup_sqlite(SOURCE_DB, backup / "source.sqlite3")}
    meta["ledger_backups"] = {
        "youtube_sync": backup_optional_ledger(SYNC_DB, backup / "youtube-sync.sqlite3"),
        "shared_upload": backup_optional_ledger(ROOT / "upload.sqlite3", backup / "shared-upload.sqlite3"),
    }
    atomic_write(backup / "composed-app.py", composed)
    meta["phase"] = "prepared"
    save_meta(backup, meta)
    return backup


def load_deployment(backup):
    backup = Path(backup).resolve()
    if (ROOT / "deployments").resolve() not in backup.parents or not backup.is_dir():
        raise RuntimeError("deployment_outside_shared_root")
    return backup, json.loads((backup / "deployment.json").read_text(encoding="utf-8"))


def verify_prepared_snapshot(meta):
    for name, entry in meta["files"].items():
        path = Path(entry["path"])
        if path.is_symlink() or path.exists() != entry["exists"]:
            raise RuntimeError("prepared_file_drift:" + name)
        if entry["exists"] and digest(path.read_bytes()) != entry["sha256"]:
            raise RuntimeError("prepared_file_drift:" + name)
    for name, entry in meta["units"].items():
        if digest(output("systemctl", "cat", name)) != entry["effective_sha256"]:
            raise RuntimeError("effective_unit_drift:" + name)
    if current_links(meta["baseline"]) != meta["links"]:
        raise RuntimeError("prepared_symlink_drift")
    if digest(read_crontab()) != meta["crontab_sha256"]:
        raise RuntimeError("prepared_crontab_drift")


def validate_cutover(path, expected_hash):
    raw = Path(path).read_bytes()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_hash) or digest(raw) != expected_hash:
        raise RuntimeError("cutover_evidence_hash_mismatch")
    evidence = json.loads(raw)
    cutoff = dt.datetime.fromisoformat(evidence["cutoff"].replace("Z", "+00:00"))
    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise RuntimeError("cutover_requires_timezone")
    for key in ("old_outbound_blocked", "old_http_drained", "new_outbound_held"):
        if evidence.get(key) is not True:
            raise RuntimeError("cutover_attestation_missing:" + key)
    if type(evidence.get("old_http_inflight")) is not int or evidence["old_http_inflight"] != 0:
        raise RuntimeError("old_http_not_drained")
    if not isinstance(evidence.get("evidence"), str) or not evidence["evidence"].strip():
        raise RuntimeError("cutover_operator_evidence_required")
    return evidence, raw


def switch(current, target):
    if not current.is_symlink():
        raise RuntimeError("current_symlink_required")
    temporary = current.with_name(current.name + ".deploy-" + str(os.getpid()))
    if temporary.exists() or temporary.is_symlink():
        raise RuntimeError("temporary_link_already_exists")
    try:
        temporary.symlink_to(target, target_is_directory=True)
        os.replace(temporary, current)
    finally:
        temporary.unlink(missing_ok=True)


def verify_units(backup, release):
    folder = backup / "verify-units"
    folder.mkdir(exist_ok=False, mode=0o700)
    for name in UNITS:
        text = (release / unit_relative(name)).read_text(encoding="utf-8")
        text = text.replace(str(YOUTUBE_CURRENT), str(release)).replace(str(REPORT_CURRENT), str(release))
        atomic_write(folder / name, text.encode("utf-8"))
    call("systemd-analyze", "verify", *[str(folder / name) for name in UNITS])


def apply(backup, cutover_path, expected_evidence_hash):
    environment_ok()
    backup, meta = load_deployment(backup)
    if meta["phase"] != "prepared":
        raise RuntimeError("deployment_not_prepared")
    release, baseline, hashes = validate_release(meta["repo"], meta["release"], meta["commit"])
    if hashes != meta["release_hashes"] or baseline != meta["baseline"]:
        raise RuntimeError("prepared_release_drift")
    verify_prepared_snapshot(meta)
    require_idle()
    if not (ROOT / "HOLD").is_file() or (ROOT / "HOLD").is_symlink():
        raise RuntimeError("shared_outbound_hold_required")
    evidence, evidence_raw = validate_cutover(cutover_path, expected_evidence_hash)
    composed, proof = compose_app(APP.read_bytes(), (release / "app.py").read_bytes(), baseline)
    if proof != meta["composition"] or digest((backup / "composed-app.py").read_bytes()) != proof["installed_sha256"]:
        raise RuntimeError("composition_proof_drift")
    verify_units(backup, release)
    # No changes occur before all preflights above succeed. After a partial apply,
    # never restore old callback code: use rollback to hold the current bridge.
    atomic_write(backup / "cutover-evidence.json", evidence_raw)
    meta.update(phase="applying", cutoff=evidence["cutoff"], cutover_sha256=expected_evidence_hash,
                apply_started_at=now())
    save_meta(backup, meta)
    atomic_write(CORE, (release / "features/cover_material_source.py").read_bytes(), 0o644)
    atomic_write(APP, composed, 0o644)
    if digest(APP.read_bytes()) != proof["installed_sha256"] or without_callback(APP.read_text(encoding="utf-8")) != without_callback(
            (backup / "files/app.py").read_text(encoding="utf-8")):
        raise RuntimeError("installed_app_verification_failed")
    meta["phase"] = "bridge_installed"
    save_meta(backup, meta)
    switch(YOUTUBE_CURRENT, release)
    switch(REPORT_CURRENT, release)
    for name in UNITS:
        atomic_write(UNITS_DIR / name, (release / unit_relative(name)).read_bytes(), 0o644)
    call("systemctl", "daemon-reload")
    for name in UNITS:
        if digest((UNITS_DIR / name).read_bytes()) != hashes[unit_relative(name)]:
            raise RuntimeError("installed_unit_verification_failed")
    require_idle()
    meta.update(phase="installed_held", installed_at=now(), restarts_performed=False, hold_removed=False,
                installed_files={"app.py": digest(APP.read_bytes()),
                                 "cover_material_source.py": digest(CORE.read_bytes())})
    save_meta(backup, meta)
    return meta


def place_hold():
    import fcntl
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (ROOT / "outbound.lock").open("a+b") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            atomic_write(ROOT / "HOLD", b"Deployment rollback: outbound held; reconcile existing attempts only.\n")
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def rollback(backup):
    environment_ok()
    backup, meta = load_deployment(backup)
    # This is an operational rollback to a durable HOLD, not restoration of an
    # old sender. Stop the timer before the worker; preserve every DB and bridge.
    place_hold()
    call("systemctl", "disable", "--now", "youtube-cover-material-sync.timer")
    call("systemctl", "stop", "youtube-cover-material-sync.service")
    for name in UNITS[:2]:
        if unit_state(name) not in ("inactive", "failed"):
            raise RuntimeError("rollback_worker_not_stopped")
    _tree, node = function_node(APP.read_text(encoding="utf-8"))
    if ast.dump(node, include_attributes=False) != ast.dump(ast.parse(BRIDGE).body[0], include_attributes=False):
        raise RuntimeError("rollback_held_but_bridge_not_installed")
    meta.update(phase="rollback_held", rollback_at=now(), hold_removed=False,
                rollback_policy="keep_bridge_and_all_databases", timer_reenable_requires_review=True)
    save_meta(backup, meta)
    return meta


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare_parser = sub.add_parser("prepare")
    for name in ("repo", "release", "commit"):
        prepare_parser.add_argument("--" + name, required=True)
    apply_parser = sub.add_parser("apply")
    apply_parser.add_argument("--deployment", required=True)
    apply_parser.add_argument("--cutover-evidence", required=True)
    apply_parser.add_argument("--evidence-sha256", required=True)
    rollback_parser = sub.add_parser("rollback")
    rollback_parser.add_argument("--deployment", required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        result = {"prepared": str(prepare(args.repo, args.release, args.commit)), "services_started": False}
    elif args.command == "apply":
        meta = apply(args.deployment, args.cutover_evidence, args.evidence_sha256)
        result = {key: meta[key] for key in ("phase", "commit", "cutoff", "hold_removed")}
    else:
        meta = rollback(args.deployment)
        result = {key: meta[key] for key in ("phase", "rollback_policy", "hold_removed")}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
