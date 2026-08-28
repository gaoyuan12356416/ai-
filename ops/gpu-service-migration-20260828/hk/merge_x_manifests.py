#!/usr/bin/env python3
"""Conservative US X cache import: HK wins every collision; never repairs media."""
import argparse
import collections
import hashlib
import importlib
import json
import os
import pathlib
import re
import sys
import urllib.parse

from deploy import BACKUP, INPUTS, X_ROOT, X_SHA, X_PROFILE, active, parse_env, write_json

FIELDS = {"job_key", "material_id", "pool_item_id", "source_url", "source_sha256",
          "source_size", "trigger_code", "profile", "duration_policy"}
RESULT_FIELDS = {"job_key", "profile", "output_url", "output_sha256", "output_size", "probe"}
HEX = re.compile(r"^[0-9a-f]{64}$")


def classify(source, destination, head_checker=None):
    source, destination = pathlib.Path(source), pathlib.Path(destination)
    if source.is_symlink() or not source.is_file():
        return "invalid_file"
    if destination.exists():
        if destination.is_file() and not destination.is_symlink():
            if source.read_bytes() == destination.read_bytes():
                return "already_present"
        return "hk_kept"
    try:
        data = json.loads(source.read_text())
        if not isinstance(data, dict) or data.get("status") != "ready":
            return "not_ready"
        request, result = data.get("request"), data.get("result")
        if not isinstance(request, dict) or not isinstance(result, dict):
            return "invalid_shape"
        if request.get("profile") != X_PROFILE:
            return "historical_profile"
        if set(request) != FIELDS or not RESULT_FIELDS.issubset(result):
            return "invalid_shape"
        key = source.stem
        if not HEX.fullmatch(key) or request["job_key"] != key or result["job_key"] != key:
            return "invalid_identity"
        if result["profile"] != X_PROFILE or not HEX.fullmatch(str(result["output_sha256"])):
            return "invalid_result"
        if not HEX.fullmatch(str(request["source_sha256"])):
            return "invalid_request"
        if request["duration_policy"] not in {"standard", "premium"}:
            return "invalid_request"
        if not isinstance(result["probe"], dict) or int(result["output_size"]) <= 0:
            return "invalid_result"
        if not data.get("cos_key") or urllib.parse.urlsplit(result["output_url"]).scheme != "https":
            return "invalid_result"
        if head_checker is None:
            return "eligible_requires_head"
        return "eligible" if head_checker(data) else "head_failed"
    except (OSError, ValueError, TypeError, KeyError):
        return "invalid_shape"


def load_head_checker():
    sys.path.insert(0, str(X_ROOT / "releases" / X_SHA))
    module = importlib.import_module("features.x_posts.media_repair")
    temporary_env = {}
    for name in ("cos.env", "worker.env", "token.env"):
        temporary_env.update(parse_env(X_ROOT / "config" / name))
    old = {key: os.environ.get(key) for key in temporary_env}
    try:
        os.environ.update({key: value.strip("\"'") for key, value in temporary_env.items()})
        config = module.WorkerConfig.from_env()
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    store = module.CosObjectStore(config)

    def check(data):
        try:
            module.validate_request(data["request"], X_PROFILE)
            result = data["result"]
            if result["output_url"] != store.url(data["cos_key"]):
                return False
            response = store.head(data["cos_key"])
            return store.validate_head(response, int(result["output_size"]), result["output_sha256"])
        except Exception:
            return False
    return check


def merge(source, destination, apply=False, with_head=False):
    source, destination = pathlib.Path(source), pathlib.Path(destination)
    if not source.is_dir() or not destination.is_dir():
        raise ValueError("source or destination manifest directory missing")
    if apply:
        if destination.resolve() != (X_ROOT / "state/manifests").resolve():
            raise ValueError("apply destination is restricted to new HK /data state")
        if active("x-post-media-repair.service"):
            raise ValueError("X worker must be stopped for manifest import")
        if not with_head:
            raise ValueError("apply requires COS HEAD verification")
    checker = load_head_checker() if with_head else None
    rows = []
    for candidate in sorted(source.glob("*.json")):
        target = destination / candidate.name
        decision = classify(candidate, target, checker)
        if apply and decision == "eligible":
            payload = candidate.read_bytes()
            with target.open("xb") as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(str(target), 0o600)
            decision = "imported"
        rows.append({"file": candidate.name, "decision": decision,
                     "source_sha256": hashlib.sha256(candidate.read_bytes()).hexdigest()
                     if candidate.is_file() and not candidate.is_symlink() else None})
    counts = dict(collections.Counter(item["decision"] for item in rows))
    report = {"apply": apply, "with_head": with_head, "source_count": len(rows),
              "counts": counts, "items": rows, "hk_collision_policy": "never_overwrite"}
    if apply:
        write_json(BACKUP / "x-manifest-import.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=pathlib.Path, default=
                        INPUTS / "x-us-history/data/x-post-media-repair/manifests")
    parser.add_argument("--destination", type=pathlib.Path,
                        default=pathlib.Path("/var/lib/x-post-media-repair/manifests"))
    parser.add_argument("--with-head", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(merge(args.source, args.destination, args.apply, args.with_head),
                     indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

