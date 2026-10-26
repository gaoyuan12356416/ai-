#!/usr/bin/env python3
"""One-time, frozen-manifest cleanup of violating Facebook Page posts."""
import argparse
import contextlib
import csv
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from cleanup_core import (Ledger, canonicalize_evidence, execute, reconcile,
                          load_manifest, manifest_bytes, manifest_sha256)
from cleanup_graph import PageGraph, RateBudget
from cleanup_source import SourceAdapter


EXPECTED_UUID = "3e8ac4e8-7770-456d-9e89-2ec5dd405fa8"


def atomic_json(path, obj):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(obj, stream, ensure_ascii=False, sort_keys=True, indent=2, default=str)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(str(temporary), str(path))
    descriptor = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def verify_data_disk(run_dir):
    resolved = Path(run_dir).resolve()
    root = Path("/mnt/data-disk").resolve()
    if root not in resolved.parents:
        raise RuntimeError("run_directory_must_be_on_data_disk")
    value = subprocess.check_output(
        ["findmnt", "-n", "-o", "UUID", "--target", str(root)], text=True).strip()
    if value != EXPECTED_UUID:
        raise RuntimeError("data_disk_uuid_mismatch")
    if os.statvfs(str(root)).f_bavail * os.statvfs(str(root)).f_frsize < 1024 ** 3:
        raise RuntimeError("data_disk_free_space_below_1GiB")


@contextlib.contextmanager
def process_lock(run_dir):
    import fcntl
    # One global lock, even when another process uses a different run directory.
    lock_path = Path("/mnt/data-disk/fb-violation-post-cleanup/process.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("another_cleanup_is_running")
        yield


def safe_credential(credential):
    context = getattr(credential, "context", None)
    if callable(context):
        context = context()
    if context is None:
        context = {"credential_id": str(getattr(credential, "id", "")),
                   "fb_user_id": str(getattr(credential, "fb_user_id", ""))}
    return dict(context)


class BoundTarget:
    def __init__(self, adapter, target):
        self.adapter = adapter
        self.target = target
        self.client = None
        self.first_inspection = None

    def _source_check(self):
        source_ids = set()
        for row in self.target["evidence"]:
            if row.get("source_row_id"):
                source_ids.add(str(row["source_row_id"]))
            source_ids.update(map(str, row.get("source_row_ids", [])))
        return self.adapter.source.precheck(
            self.target["page_id"], self.target["material_ids"], source_row_ids=sorted(source_ids))

    def precheck(self):
        result = self._source_check()
        if not result.get("ok"):
            return result
        preferred = next((row.get("publisher_fb_user_id") for row in self.target["evidence"]
                          if row.get("publisher_fb_user_id")), None)
        credentials = self.adapter.source.credentials(self.target["page_id"], preferred)
        tried = []
        seen = set()
        for credential in credentials:
            token = credential.token
            digest = hashlib.sha256(token.encode()).digest()
            if digest in seen:
                continue
            seen.add(digest)
            context = safe_credential(credential)
            candidate = PageGraph(self.target["page_id"], token, context,
                                  self.adapter.budget, self.adapter.version)
            identity = candidate.identity()
            inspection = candidate.inspect(self.target["post_id"]) if identity.get("ok") else None
            tried.append({"context": context, "identity": identity, "inspection": inspection})
            if identity.get("ok") and inspection and inspection.get("ok"):
                fresh = self._source_check()
                if not fresh.get("ok"):
                    candidate.close()
                    return fresh
                self.client = candidate
                self.first_inspection = inspection
                return {"ok": True, "credential_id": str(context.get("credential_id", context.get("id", ""))),
                        "context": context, "source_check": fresh}
            candidate.close()
            if len(seen) >= 3:
                break
        return {"ok": False, "reason": "post_identity_or_permission_unconfirmed", "credential_checks": tried}

    def inspect(self, post_id):
        if self.first_inspection is not None:
            response = self.first_inspection
            self.first_inspection = None
            return response
        if self.client is None:
            # Reconciliation can read an object which is now absent. Do not
            # require its pre-delete readability or repeat a DELETE.
            credentials = self.adapter.source.credentials(self.target["page_id"])
            for credential in credentials[:3]:
                candidate = PageGraph(self.target["page_id"], credential.token,
                                      safe_credential(credential), self.adapter.budget,
                                      self.adapter.version)
                if candidate.identity().get("ok"):
                    self.client = candidate
                    break
                candidate.close()
        if self.client is None:
            return {"ok": False, "error": "no_readable_page_credential"}
        return self.client.inspect(post_id)

    def reconcile_precheck(self):
        credentials = self.adapter.source.credentials(self.target["page_id"])
        for credential in credentials[:3]:
            context = safe_credential(credential)
            candidate = PageGraph(self.target["page_id"], credential.token, context,
                                  self.adapter.budget, self.adapter.version)
            result = candidate.identity()
            if result.get("ok"):
                self.client = candidate
                return {"ok": True, "credential_id": context["credential_id"], "context": context}
            candidate.close()
        return {"ok": False, "reason": "no_readable_page_credential"}

    def delete(self, post_id):
        if self.client is None:
            raise RuntimeError("precheck_required")
        # One final current-state read after the durable intent and immediately
        # before the external mutation. A rejection sends no DELETE.
        self.client.before_delete = self._source_check
        return self.client.delete(post_id)

    def close(self):
        if self.client:
            self.client.close()


class Adapter:
    def __init__(self, source, version="v25.0"):
        self.source = source
        self.version = version
        self.budget = RateBudget()

    def for_target(self, target):
        return BoundTarget(self, target)


def normalized_evidence(snapshot, source, version):
    candidates = snapshot["candidates"]
    known = {(str(r["page_id"]), str(r["real_post_id"]), str(r.get("source_row_id", "")))
             for r in candidates if str(r.get("real_post_id", "")) not in ("", "0", "None")}
    evidence, unresolved = [], list(snapshot.get("unresolved", []))
    budget = RateBudget()
    resolution_cache = {}
    for row in candidates:
        page = str(row["page_id"])
        real = str(row.get("real_post_id") or "")
        if real == "0":
            real = ""
        raw = str(row.get("raw_post_id") or "")
        origin = str(row.get("table", row.get("origin", "")))
        allow_post_suffix = not origin.startswith("fb_")
        if "_" in raw:
            normalized_real = real if "_" in real else page + "_" + real
            if raw.split("_", 1)[0] != page or (real and normalized_real != raw):
                unresolved.append(dict(row, reason="conflicting_explicit_post_identity"))
                continue
            if not real:
                real = raw
        if not real and allow_post_suffix and (page, raw, str(row.get("source_row_id", ""))) in known:
            real = raw
        resolution = None
        if not real:
            key = (page, raw, allow_post_suffix)
            if key not in resolution_cache:
                resolved = {"ok": False, "error": "no_eligible_credential"}
                credentials = source.credentials(page, row.get("publisher_fb_user_id"))
                for credential in credentials[:3]:
                    graph = PageGraph(page, credential.token, safe_credential(credential), budget, version)
                    try:
                        if graph.identity().get("ok"):
                            resolved = graph.resolve(raw, allow_post_suffix=allow_post_suffix)
                            if resolved.get("ok"):
                                break
                    finally:
                        graph.close()
                resolution_cache[key] = resolved
            resolution = resolution_cache[key]
            if not resolution.get("ok"):
                unresolved.append(dict(row, reason="post_id_unresolved", resolution=resolution))
                continue
            real = resolution["id"]
        post_id = real if "_" in real else page + "_" + real
        evidence.append(dict(row, post_id=post_id, page_id=page,
                             material_id=str(row["material_id"]),
                             groups=row["group_ids"], published_at=row["published_at_utc"],
                             source_kind=row.get("table", row.get("origin", "")),
                             source_log_id=str(row["log_id"]), fb_violation=1,
                             resolution=resolution))
    return evidence, unresolved


def write_report(run_dir, ledger, manifest, summary):
    rows = ledger.rows()
    group_counts = {}
    by_id = {target["post_id"]: target for target in manifest["targets"]}
    for row in rows:
        for group in by_id[row["post_id"]]["groups"]:
            counts = group_counts.setdefault(str(group), {})
            status = row["status"]
            counts[status] = counts.get(status, 0) + 1
    report = {"at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
              "manifest_sha256": manifest_sha256(manifest), "summary": summary,
              "groups": group_counts, "results": rows}
    atomic_json(run_dir / "results.json", report)
    columns = ["post_id", "page_id", "material_ids", "groups", "status", "verification"]
    temporary = run_dir / "results.csv.tmp"
    with temporary.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            target = by_id[row["post_id"]]
            writer.writerow({"post_id": row["post_id"], "page_id": target["page_id"],
                             "material_ids": ",".join(target["material_ids"]),
                             "groups": ",".join(map(str, target["groups"])),
                             "status": row["status"], "verification": row.get("verification", "")})
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(str(temporary), str(run_dir / "results.csv"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["scan", "execute", "reconcile", "report"])
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--manifest-sha256")
    parser.add_argument("--stage", choices=["canary", "remaining", "all"], default="canary")
    parser.add_argument("--version", default="v25.0")
    parser.add_argument("--window-end")
    args = parser.parse_args()
    run_dir = Path(args.run_dir).resolve()
    verify_data_disk(run_dir)
    if args.command == "scan":
        run_dir.mkdir(parents=True, exist_ok=False)
    elif not run_dir.is_dir():
        raise RuntimeError("run_directory_missing")
    with process_lock(run_dir):
        if args.command == "scan":
            source = SourceAdapter.from_env()
            end = dt.datetime.fromisoformat(args.window_end.replace("Z", "+00:00")) if args.window_end else dt.datetime.now(dt.timezone.utc)
            if end.tzinfo is None:
                raise ValueError("window_timezone_required")
            end = end.astimezone(dt.timezone.utc).replace(microsecond=0)
            start = end - dt.timedelta(days=90)
            snapshot = source.freeze(start.isoformat(), end.isoformat())
            atomic_json(run_dir / "source-snapshot.json", snapshot)
            evidence, unresolved = normalized_evidence(snapshot, source, args.version)
            manifest = canonicalize_evidence(evidence, start.isoformat(), end.isoformat())
            manifest["scope_snapshot"] = snapshot["pages"]
            manifest["unresolved"] = manifest.get("unresolved", []) + unresolved
            manifest["graph_version"] = args.version
            manifest["created_at_utc"] = end.isoformat()
            raw = manifest_bytes(manifest)
            path = run_dir / "manifest.json"
            with path.open("xb") as stream:
                stream.write(raw); stream.flush(); os.fsync(stream.fileno())
            digest = manifest_sha256(manifest)
            atomic_json(run_dir / "manifest-sha256.json", {"sha256": digest})
            os.chmod(str(path), 0o400)
            print(json.dumps({"manifest_sha256": digest, "targets": len(manifest["targets"]),
                              "unresolved": len(unresolved), "run_dir": str(run_dir)}, ensure_ascii=False), flush=True)
            return
        if not args.manifest_sha256:
            raise ValueError("explicit_manifest_sha256_required")
        manifest = load_manifest(run_dir / "manifest.json", args.manifest_sha256)
        if args.version != manifest.get("graph_version", args.version):
            raise ValueError("graph_version_changed")
        if args.command == "execute":
            binding = Path("/mnt/data-disk/fb-violation-post-cleanup/active-execution.json")
            expected = {"run_dir": str(run_dir), "manifest_sha256": args.manifest_sha256}
            if binding.exists():
                if json.loads(binding.read_text(encoding="utf-8")) != expected:
                    raise RuntimeError("different_execution_ledger_cannot_bypass_previous_run")
            else:
                atomic_json(binding, expected)
        ledger = Ledger(run_dir / "deletions.sqlite3")
        try:
            ledger.import_manifest(manifest)
            if args.command == "report":
                summary = ledger.summary()
            else:
                adapter = Adapter(SourceAdapter.from_env(), args.version)
                if args.command == "execute":
                    summary = execute(manifest, ledger, adapter, stage=args.stage, workers=2, canary_size=5)
                else:
                    summary = reconcile(manifest, ledger, adapter)
            write_report(run_dir, ledger, manifest, summary)
            print(json.dumps(summary, ensure_ascii=False), flush=True)
        finally:
            ledger.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Never print a traceback containing database or credential objects.
        print(json.dumps({"fatal": type(exc).__name__, "message": str(exc)[:300]}), file=sys.stderr)
        raise SystemExit(1)
