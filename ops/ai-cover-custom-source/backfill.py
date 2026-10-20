#!/usr/bin/env python3
"""Finite, serial backfill of fixed manifests through the shared manage runner."""
import argparse
import contextlib
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from features.cover_material_source import ROOT, encoded, identity, readonly, utcnow
from manage import read_manifest, write_json


class BackfillStopped(RuntimeError):
    pass


def load_bundle(manifest, supplements=()):
    """Keep supplied order and pin both source hashes and complete file hashes."""
    paths = [Path(manifest)] + [Path(p) for p in supplements]
    entries, keys, seen, candidates, manifest_issues = [], [], set(), set(), []
    main = read_manifest(paths[0])
    parent_file_sha = hashlib.sha256(paths[0].read_bytes()).hexdigest()
    for index, path in enumerate(paths):
        body = read_manifest(path)
        if body["cutoff"] != main["cutoff"] or int(body.get("window_hours", 0)) != 168:
            raise ValueError("manifest_window_changed")
        if index:
            parent = body.get("parent_manifest") or {}
            if (body.get("kind") != "legacy_final_supplement"
                    or parent.get("sha256") != main["sha256"]
                    or parent.get("file_sha256") != parent_file_sha):
                raise ValueError("supplement_parent_mismatch")
        own_keys = []
        for source in body["sources"]:
            key = identity(source)
            if key in seen:
                raise ValueError("duplicate_key_across_manifests")
            seen.add(key)
            own_keys.append(key)
            keys.append(key)
        for issue in body.get("issues", []):
            if issue.get("excluded") is True:
                continue
            manifest_issues.append((body["sha256"], issue))
            if issue.get("reason") == "legacy_final_pending" and issue.get("candidate"):
                candidates.add(identity(issue["candidate"]))
        entries.append(dict(path=str(path.resolve()), sha256=body["sha256"],
                            file_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                            count=len(own_keys), keys=own_keys))
    unresolved = []
    for manifest_sha, issue in manifest_issues:
        if (issue.get("reason") == "legacy_final_pending" and issue.get("candidate")
                and identity(issue["candidate"]) in seen):
            continue
        unresolved.append(dict(manifest_sha256=manifest_sha,
                               source_kind=issue.get("source_kind", ""),
                               source_key=issue.get("source_key", ""),
                               reason=issue.get("reason", "unclassified_manifest_issue")))
    bundle = dict(cutoff=main["cutoff"], manifests=entries, keys=keys,
                  unresolved_candidate_keys=sorted(candidates - seen),
                  unresolved_issues=unresolved)
    bundle["sha256"] = hashlib.sha256(encoded(bundle).encode()).hexdigest()
    return bundle


def ledger_rows(root, keys):
    path = Path(root) / "upload.sqlite3"
    if not path.exists():
        return {}
    rows = {}
    with contextlib.closing(readonly(path)) as conn:
        conn.execute("BEGIN")
        for start in range(0, len(keys), 400):
            batch = keys[start:start + 400]
            if batch:
                query = "SELECT key,state,attempts,final_id FROM uploads WHERE key IN (" + ",".join("?" for _ in batch) + ")"
                rows.update({r["key"]: dict(r) for r in conn.execute(query, batch)})
    return rows


def summary(keys, rows):
    counts = {}
    unsafe = []
    for key in keys:
        row = rows.get(key)
        state = row["state"] if row else "not_started"
        counts[state] = counts.get(state, 0) + 1
        if row:
            valid = ((state == "confirmed" and int(row.get("final_id") or 0) > 0)
                     or (state == "prepared" and int(row["attempts"]) == 0))
            if not valid:
                unsafe.append(dict(key=key, state=state, attempts=row["attempts"],
                                   final_id=row.get("final_id")))
    return dict(total=len(keys), confirmed=counts.get("confirmed", 0), counts=counts,
                unsafe=unsafe, remaining=len(keys) - counts.get("confirmed", 0))


@contextlib.contextmanager
def runner_lock(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / "backfill.lock").open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise BackfillStopped("backfill_already_running") from None
        else:
            import fcntl
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise BackfillStopped("backfill_already_running") from None
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def run_backfill(manifest, supplements=(), *, root=ROOT, batch_size=50,
                 round_timeout=3600, max_rounds=None, resume=False, invoke=subprocess.run):
    batch_size = int(batch_size)
    if not 1 <= batch_size <= 50 or round_timeout <= 0:
        raise ValueError("invalid_backfill_limits")
    progress_path = Path(root) / "backfill-progress.json"
    with runner_lock(root):
        bundle = load_bundle(manifest, supplements)
        state = dict(status="running", pid=os.getpid(), started_at=utcnow(),
                     updated_at=utcnow(), cutoff=bundle["cutoff"],
                     bundle_sha256=bundle["sha256"], manifests=bundle["manifests"],
                     batch_size=batch_size, rounds=[])
        if progress_path.exists():
            previous = json.loads(progress_path.read_text(encoding="utf-8"))
            if previous.get("bundle_sha256") != bundle["sha256"]:
                raise BackfillStopped("progress_manifest_mismatch")
            if previous.get("status") != "complete" and not resume:
                raise BackfillStopped("previous_run_requires_explicit_resume")
            state["previous_run"] = dict(status=previous.get("status"),
                                         started_at=previous.get("started_at"),
                                         rounds=len(previous.get("rounds", [])))

        def checkpoint(**fields):
            state.update(fields, updated_at=utcnow())
            write_json(progress_path, state)

        def stop(reason, **fields):
            checkpoint(status="stopped", reason=reason, **fields)
            raise BackfillStopped(reason)

        round_limit = sum(math.ceil(entry["count"] / batch_size) for entry in bundle["manifests"])
        if max_rounds is not None:
            round_limit = min(round_limit, max(0, int(max_rounds)))
        checkpoint(round_limit=round_limit)
        try:
            for round_no in range(round_limit + 1):
                current_bundle = load_bundle(manifest, supplements)
                if current_bundle["sha256"] != bundle["sha256"]:
                    stop("manifest_changed_during_run")
                rows = ledger_rows(root, bundle["keys"])
                status = summary(bundle["keys"], rows)
                checkpoint(summary=status)
                if status["unsafe"]:
                    stop("unresolved_ledger_outcome", unsafe=status["unsafe"])
                if status["remaining"] == 0:
                    if bundle["unresolved_candidate_keys"]:
                        stop("pending_candidates_need_supplement",
                             unresolved_candidate_keys=bundle["unresolved_candidate_keys"])
                    if bundle["unresolved_issues"]:
                        stop("manifest_unresolved_issues",
                             unresolved_issues=bundle["unresolved_issues"])
                    checkpoint(status="complete", finished_at=utcnow(), reason="")
                    return state
                if round_no == round_limit:
                    stop("finite_round_limit_reached")
                entry = next(e for e in bundle["manifests"]
                             if any(k not in rows or rows[k]["state"] != "confirmed" for k in e["keys"]))
                selected = [k for k in entry["keys"] if k not in rows or rows[k]["state"] != "confirmed"][:batch_size]
                round_record = dict(number=round_no + 1, manifest_sha256=entry["sha256"],
                                    selected_keys=selected, started_at=utcnow(),
                                    confirmed_before=status["confirmed"], status="running")
                state["rounds"].append(round_record)
                checkpoint(active_round=round_no + 1)
                command = [sys.executable, str(Path(__file__).with_name("manage.py")), "run",
                           "--root", str(root), "--manifest", entry["path"],
                           "--max-new", str(batch_size), "--keys"] + selected
                try:
                    result = invoke(command, capture_output=True, text=True,
                                    encoding="utf-8", timeout=round_timeout, check=False)
                except subprocess.TimeoutExpired:
                    round_record.update(status="stopped", finished_at=utcnow())
                    stop("round_timeout_no_retry")
                rows_after = ledger_rows(root, bundle["keys"])
                after = summary(bundle["keys"], rows_after)
                round_record.update(finished_at=utcnow(), returncode=result.returncode,
                                    confirmed_after=after["confirmed"],
                                    status="complete" if result.returncode == 0 else "stopped")
                checkpoint(summary=after)
                if result.returncode != 0:
                    stop("manage_round_failed", child_returncode=result.returncode)
                if after["unsafe"]:
                    stop("unresolved_ledger_outcome", unsafe=after["unsafe"])
                if any(k not in rows_after or rows_after[k]["state"] != "confirmed" for k in selected):
                    stop("round_did_not_confirm_selected_keys")
        except BackfillStopped:
            raise
        except Exception as error:
            checkpoint(status="stopped", reason="runner_exception",
                       exception_type=type(error).__name__)
            raise BackfillStopped("runner_exception:" + type(error).__name__) from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--supplement", action="append", default=[])
    parser.add_argument("--root", default=os.environ.get("AI_COVER_SOURCE_ROOT", ROOT))
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--round-timeout", type=int, default=3600)
    parser.add_argument("--max-rounds", type=int)
    parser.add_argument("--resume", action="store_true",
                        help="Explicitly resume the same immutable bundle after reconciliation")
    args = parser.parse_args(argv)
    try:
        result = run_backfill(args.manifest, args.supplement, root=args.root,
                              batch_size=args.batch_size, round_timeout=args.round_timeout,
                              max_rounds=args.max_rounds, resume=args.resume)
        print(encoded(dict(status=result["status"], summary=result["summary"],
                           bundle_sha256=result["bundle_sha256"])))
        return 0
    except BackfillStopped as error:
        print(encoded(dict(status="stopped", reason=str(error))))
        return 2
    except Exception as error:
        print(encoded(dict(status="stopped", reason="preflight_exception",
                           exception_type=type(error).__name__)))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
