#!/usr/bin/env python3
"""Verify that a deploy candidate still contains all active shared features."""

import argparse
import json
import sys
from pathlib import Path


DEFAULT_MANIFEST = Path(__file__).resolve().parents[1] / "deploy" / "live_feature_guard.json"


def read_text(path):
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="replace")


def load_manifest(path):
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data.get("features"), list):
        raise SystemExit("manifest must contain a features array")
    return data


def selected_features(manifest, names):
    features = manifest.get("features") or []
    if not names:
        return features
    wanted = set(names)
    selected = [feature for feature in features if feature.get("id") in wanted]
    found = {feature.get("id") for feature in selected}
    missing = sorted(wanted - found)
    if missing:
        raise SystemExit("unknown feature(s): %s" % ", ".join(missing))
    return selected


def check_file(root, file_rule, label):
    path = root / file_rule["path"]
    failures = []
    if not path.exists():
        return ["%s missing file: %s" % (label, path)]
    text = read_text(path)
    for token in file_rule.get("contains") or []:
        if token not in text:
            failures.append("%s missing token in %s: %s" % (label, path, token))
    return failures


def check_public_file(public_root, file_rule, label):
    public_path = file_rule.get("public_path")
    if not public_root or not public_path:
        return []
    path = public_root / public_path
    failures = []
    if not path.exists():
        return ["%s missing public file: %s" % (label, path)]
    text = read_text(path)
    for token in file_rule.get("contains") or []:
        if token not in text:
            failures.append("%s missing public token in %s: %s" % (label, path, token))
    return failures


def run_checks(root, public_root, features):
    failures = []
    checked = 0
    for feature in features:
        feature_id = feature.get("id") or "<unknown>"
        for file_rule in feature.get("required") or []:
            checked += 1
            label = "[%s]" % feature_id
            failures.extend(check_file(root, file_rule, label))
            failures.extend(check_public_file(public_root, file_rule, label))
    return checked, failures


def main(argv=None):
    parser = argparse.ArgumentParser(description="Guard shared live AI backend features before deployment.")
    parser.add_argument("--root", default=".", help="Candidate service root, for example /root/drama_material_service.")
    parser.add_argument("--public-root", default="", help="Optional nginx public root, for example /usr/share/nginx/html.")
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST), help="Feature guard manifest path.")
    parser.add_argument("--feature", action="append", default=[], help="Check only this feature id. Repeatable.")
    parser.add_argument("--list", action="store_true", help="List feature ids and exit.")
    args = parser.parse_args(argv)

    manifest_path = Path(args.manifest).resolve()
    manifest = load_manifest(manifest_path)
    if args.list:
        for feature in manifest.get("features") or []:
            print("%s\t%s" % (feature.get("id", ""), feature.get("description", "")))
        return 0

    root = Path(args.root).resolve()
    public_root = Path(args.public_root).resolve() if args.public_root else None
    features = selected_features(manifest, args.feature)
    checked, failures = run_checks(root, public_root, features)
    if failures:
        print("LIVE FEATURE GUARD FAILED")
        print("manifest: %s" % manifest_path)
        print("root: %s" % root)
        if public_root:
            print("public_root: %s" % public_root)
        for failure in failures:
            print("- %s" % failure)
        return 1
    print("LIVE FEATURE GUARD PASSED: %d feature(s), %d file rule(s)" % (len(features), checked))
    return 0


if __name__ == "__main__":
    sys.exit(main())
