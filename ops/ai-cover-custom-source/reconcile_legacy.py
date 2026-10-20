#!/usr/bin/env python3
"""Read back old pending finals and optionally freeze an immutable supplement.

This never submits material. A supplement can be consumed by manage.py run
using the same content signatures as its original frozen candidates.
"""
import argparse
import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from features.cover_material_source import Catalog, encoded, identity, utcnow
from history import timestamp, verify_legacy_candidates


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def load_parent(path, expected_sha=None):
    path = Path(path)
    raw = path.read_bytes()
    parent = json.loads(raw)
    if parent.get("sha256") != digest(parent["sources"]):
        raise ValueError("parent_manifest_hash_mismatch")
    if expected_sha and parent["sha256"] != expected_sha:
        raise ValueError("unexpected_parent_manifest_hash")
    if int(parent.get("window_hours", 0)) != 168:
        raise ValueError("parent_window_mismatch")
    timestamp(parent["cutoff"])
    return parent, dict(path=str(path.resolve()), sha256=parent["sha256"],
                        file_sha256=hashlib.sha256(raw).hexdigest())


def reconcile_pending(parent, catalog):
    """Only issues carrying original legacy_final_pending candidates are eligible."""
    if parent.get("sha256") != digest(parent["sources"]):
        raise ValueError("parent_manifest_hash_mismatch")
    end = timestamp(parent["cutoff"])
    start = end - dt.timedelta(hours=168)
    existing = {identity(x) for x in parent["sources"]}
    seen, candidates = set(), []
    for issue in parent["issues"]:
        if issue.get("reason") != "legacy_final_pending":
            continue
        source = copy.deepcopy(issue["candidate"])
        key = identity(source)
        if (source["source_key"] != issue["source_key"]
                or source["source_kind"] != issue["source_kind"]
                or source.get("legacy_final_ids")
                or not source.get("is_backfill")
                or source.get("backfill_kind") != "migration"
                or not start <= timestamp(source["legacy_accepted_at"]) < end):
            raise ValueError("pending_candidate_not_in_parent_scope")
        if key in existing or key in seen:
            raise ValueError("duplicate_pending_candidate")
        seen.add(key)
        candidates.append(source)
    sources, issues = verify_legacy_candidates(candidates, catalog, recheck_images=True)
    if {identity(x) for x in sources} - seen:
        raise ValueError("supplement_identity_changed")
    return sources, issues, digest(candidates)


def supplement_body(parent, reference, sources, issues, candidates_sha):
    return dict(kind="legacy_final_supplement", cutoff=parent["cutoff"],
                window_hours=168, created_at=utcnow(), parent_manifest=reference,
                parent_candidates_sha256=candidates_sha,
                sources=sources, issues=issues, sha256=digest(sources))


def freeze_supplement(path, body):
    """Create once; never rewrite a previous supplement or its parent manifest."""
    path = Path(path)
    if path.resolve() == Path(body["parent_manifest"]["path"]).resolve():
        raise ValueError("supplement_cannot_overwrite_parent")
    if body["sha256"] != digest(body["sources"]):
        raise ValueError("supplement_hash_mismatch")

    def existing():
        old = json.loads(path.read_text(encoding="utf-8"))
        comparable = lambda x: {k: v for k, v in x.items() if k != "created_at"}
        if old.get("sha256") != digest(old["sources"]) or comparable(old) != comparable(body):
            raise ValueError("immutable_supplement_already_exists")
        return old

    if path.exists():
        return existing()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(encoded(body))
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        try:
            # A hard link publishes the fully flushed file without overwriting
            # an existing destination, including concurrent invocations.
            os.link(temporary, path)
        except FileExistsError:
            return existing()
    finally:
        Path(temporary).unlink()
    return body


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent", required=True)
    parser.add_argument("--expected-parent-sha")
    parser.add_argument("--output", help="Optional new immutable supplement; omit for read-only plan")
    args = parser.parse_args(argv)
    parent, reference = load_parent(args.parent, args.expected_parent_sha)
    sources, issues, candidates_sha = reconcile_pending(parent, Catalog(dict(os.environ)))
    body = supplement_body(parent, reference, sources, issues, candidates_sha)
    if args.output:
        body = freeze_supplement(args.output, body)
    print(encoded(dict(cutoff=body["cutoff"], parent_sha256=reference["sha256"],
                       parent_file_sha256=reference["file_sha256"],
                       sha256=body["sha256"], count=len(body["sources"]),
                       issues=body["issues"], output=args.output)))


if __name__ == "__main__":
    main()
