"""Retire cold source files for the approved report-storage-20261120 run."""

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import history

ROOT = history.BASE
JOB = ROOT / "report-storage-20261120"
ARCHIVE = Path("/opt/private-storage-archive/current/scripts/private_storage_archive.py")
INPUT_KINDS = {"dw_daily", "dw_adset", "hotdrama_report"}


def json_lines(path):
    if path.is_file():
        with path.open(encoding="utf-8") as source:
            for line in source:
                if line.endswith("\n") and line.strip():
                    yield json.loads(line)


def verified_sources(job, frozen):
    verified = {}
    for journal in sorted((job / "receipts").glob("history*.jsonl")):
        if journal.name.startswith("history-retire-"):
            continue
        for entry in json_lines(journal):
            rel = entry.get("path")
            kind = history.match(rel) if rel in frozen else None
            if entry.get("status") == "verified" and kind in INPUT_KINDS and entry.get("dataset") == kind:
                verified[rel] = entry
    return verified


def replica_confirmed(source):
    confirmed = set()
    with history.connection(False) as db, db.cursor(buffered=True) as cur:
        for chunk in history.group(list(source.values()), 80):
            ids = [item["snapshot"] for item in chunk]
            cur.execute(
                "SELECT HEX(snapshot_id),relative_path,HEX(source_sha256),record_count,status FROM "
                + history.SNAP
                + " WHERE snapshot_id IN ("
                + ",".join(["UNHEX(%s)"] * len(ids))
                + ")",
                ids,
            )
            for sid, rel, source_hash, count, status in cur:
                entry = source.get(rel)
                if (
                    entry
                    and status == "verified"
                    and (sid.lower(), source_hash.lower(), count)
                    == (entry["snapshot"], entry["source_sha256"], entry["records"])
                ):
                    confirmed.add(rel)
    return confirmed


def files_in_use(case_paths):
    active_cases, open_files = set(), set()
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit() or proc.name == str(os.getpid()):
            continue
        try:
            cwd = Path(os.readlink(proc / "cwd"))
            args = (proc / "cmdline").read_bytes()
            active_cases.update(
                directory
                for directory in case_paths
                if cwd == directory or directory in cwd.parents or os.fsencode(directory) in args
            )
            for fd in (proc / "fd").iterdir():
                try:
                    stat = fd.stat()
                    open_files.add((stat.st_dev, stat.st_ino))
                except OSError:
                    continue
        except OSError:
            continue
    return active_cases, open_files


def delivery_matches(proof, root, cache):
    assets = {proof["delivery"]: proof["delivery_sha256"], **proof.get("delivery_files", {})}
    for rel, expected in assets.items():
        path = root / rel
        if root not in path.parents or path.resolve() != path or not path.is_file():
            return False
        if path not in cache:
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for data in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(data)
            cache[path] = digest.hexdigest()
        if cache[path] != expected:
            return False
    return True


def plan_retirement():
    sys.path.insert(0, str(ARCHIVE.parent))
    from private_storage_archive import signature

    frozen = {row["path"]: row for row in json.loads((JOB / "plans/history-v1.json").read_text())}
    proofs = json.loads((JOB / "status/history-deliveries.json").read_text())
    nested = JOB / "status/history-nested-deliveries.json"
    if nested.exists():
        proofs.update(json.loads(nested.read_text()))
    source = verified_sources(JOB, frozen)
    confirmed = replica_confirmed(source)
    cases = {ROOT / key for key, value in proofs.items() if value.get("allow")}
    active, opened = files_in_use(cases)
    previous = {
        Path(row["path"])
        for ledger in (JOB / "receipts").glob("history-retire-*.jsonl")
        for row in json_lines(ledger)
        if row.get("event") == "deleted"
    }
    files, skipped, asset_hashes, case_result = [], Counter(), {}, {}
    for rel, row in source.items():
        p = ROOT / rel
        if p in previous:
            skipped["already_retired"] += 1
            continue
        proof = proofs.get(p.parent.relative_to(ROOT).as_posix(), {})
        if not proof.get("allow") or rel not in confirmed or p.parent in active:
            skipped["delivery_or_active_or_replica"] += 1
            continue
        if not p.is_file() or p.resolve() != p:
            skipped["path_changed"] += 1
            continue
        stat = p.lstat()
        original = frozen[rel]
        if (
            (stat.st_dev, stat.st_ino) in opened
            or stat.st_nlink != 1
            or (stat.st_ino, stat.st_size, stat.st_mtime_ns)
            != (original["inode"], original["size"], original["mtime_ns"])
        ):
            skipped["changed_or_open"] += 1
            continue
        if p.parent not in case_result:
            try:
                case_result[p.parent] = delivery_matches(proof, ROOT, asset_hashes)
            except OSError:
                case_result[p.parent] = False
        if not case_result[p.parent]:
            skipped["delivery_changed"] += 1
            continue
        files.append(dict(path=str(p), signature=signature(p), expected_sha256=row["source_sha256"]))
    label = "history-retire-" + str(time.time_ns())
    plan_file = JOB / "plans" / (label + ".json")
    document = dict(
        namespace=JOB.name,
        created_at=time.time(),
        files=files,
        allowed_roots=sorted({str(Path(v["path"]).parent) for v in files}),
    )
    with plan_file.open("x") as stream:
        json.dump(document, stream)
        stream.flush()
        os.fsync(stream.fileno())
    result = dict(
        label=label,
        files=len(files),
        allocated=sum(v["signature"]["allocated"] for v in files),
        retained=skipped,
    )
    return plan_file, result


def restore_original(rel):
    path = ROOT / rel
    frozen = {v["path"] for v in json.loads((JOB / "plans/history-v1.json").read_text())}
    if (
        rel not in frozen
        or history.match(rel) not in INPUT_KINDS
        or path.resolve() != path
        or path.exists()
    ):
        raise ValueError("restore_requires_missing_original_path_in_approved_plan")
    for journal in sorted((JOB / "receipts").glob("history-retire-*.jsonl"), reverse=True):
        receipt = next(
            (
                v
                for v in json_lines(journal)
                if v.get("event") == "deleted"
                and v.get("namespace") == JOB.name
                and v.get("private")
                and v["path"] == str(path)
            ),
            None,
        )
        if receipt:
            subprocess.run(
                [
                    "python3",
                    str(ARCHIVE),
                    "get",
                    "--key",
                    receipt["key"],
                    "--output",
                    str(path),
                    "--sha256",
                    receipt["sha256"],
                    "--size",
                    str(receipt["size"]),
                    "--json",
                ],
                check=True,
            )
            return
    raise ValueError("original_archive_receipt_missing")


def main():
    import fcntl

    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--archive", action="store_true")
    action.add_argument("--restore", metavar="RELATIVE_SOURCE_PATH")
    args = parser.parse_args()
    os.umask(0o077)
    with (JOB / "status/history-retirement.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if args.restore:
            restore_original(args.restore)
            return
        plan, result = plan_retirement()
        print(json.dumps(result), flush=True)
        if not args.archive or not result["files"]:
            return
        label = result["label"]
        journal = JOB / "receipts" / (label + ".jsonl")
        log = JOB / "status" / (label + ".log")
        with log.open("x") as output:
            run = subprocess.run(
                [
                    "python3",
                    str(ARCHIVE),
                    "batch",
                    "--plan",
                    str(plan),
                    "--manifest",
                    str(journal),
                    "--delete-verified",
                ],
                stdout=output,
                check=False,
            )
        removed = {Path(row["path"]): row for row in json_lines(journal) if row.get("event") == "deleted"}
        if any(p.exists() or not v.get("private") for p, v in removed.items()):
            raise RuntimeError("post_archive_validation_failed")
        print(
            json.dumps(
                dict(
                    removed=len(removed),
                    released_bytes=sum(v["released_bytes"] for v in removed.values()),
                    archive_exit=run.returncode,
                )
            ),
            flush=True,
        )
        if run.returncode or len(removed) != result["files"]:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
