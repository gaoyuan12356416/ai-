#!/usr/bin/env python3
"""Fail closed on the approved HK volume and service path, not on a new mount."""
import argparse
import json
import os
import pathlib
import shutil
import subprocess

EXPECTED_UUID = "659e6f89-71fa-463d-842e-ccdf2c06e0fe"
ALLOWED_ROOTS = ("/data/x-post-media-repair", "/data/ad-material")


def require_boundary(path, roots=ALLOWED_ROOTS):
    real = pathlib.Path(path).resolve()
    if not any(real == pathlib.Path(r) or pathlib.Path(r) in real.parents for r in roots):
        raise ValueError("service path escaped its approved /data boundary")
    return real


def inspect_storage(path, min_free_gib=20, expected_uuid=EXPECTED_UUID):
    real = require_boundary(path)
    if not real.is_dir():
        raise ValueError("service directory is missing")
    volume_uuid = subprocess.check_output(
        ["findmnt", "-n", "-o", "UUID", "-T", str(real)], universal_newlines=True
    ).strip()
    if volume_uuid != expected_uuid:
        raise ValueError("unexpected backing volume UUID")
    usage = shutil.disk_usage(str(real))
    if usage.free < min_free_gib * 1024 ** 3:
        raise ValueError("approved volume has insufficient free space")
    return {
        "ok": True, "path": str(real), "volume_uuid": volume_uuid,
        "free_bytes": usage.free, "minimum_free_gib": min_free_gib,
        "independent_mount_required": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, choices=ALLOWED_ROOTS)
    parser.add_argument("--min-free-gib", type=int, default=20)
    args = parser.parse_args()
    print(json.dumps(inspect_storage(args.root, args.min_free_gib), sort_keys=True))


if __name__ == "__main__":
    main()
