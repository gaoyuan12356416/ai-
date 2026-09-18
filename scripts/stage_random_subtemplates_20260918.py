"""Stage a complete immutable catalog, without changing any service defaults."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.fb_gpu.random_overlay import load_asset_set


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--existing-assets", required=True, type=Path)
    parser.add_argument("--target", required=True, type=Path)
    args = parser.parse_args()
    bundle = Path(__file__).resolve().parents[1] / "assets/random-subtemplates/20260918"
    metadata = json.loads((bundle / "additions.json").read_text(encoding="utf-8"))
    base = load_asset_set(args.existing_assets, metadata["base_manifest_sha256"])
    target = args.target
    if (not target.is_absolute() or target.is_symlink()
            or target.parent != Path("/data/random-overlay-gpu/assets")
            or target.name != "catalog-" + metadata["manifest_sha256"][:12]):
        raise RuntimeError("stage_target_invalid")
    if target.exists():
        assets = load_asset_set(target, metadata["manifest_sha256"])
        print(json.dumps({"reused": True, "root": str(target), "counts": {k: len(v) for k,v in assets["categories"].items()}}))
        return
    if target.parent.is_symlink():
        raise RuntimeError("stage_parent_symlink_invalid")
    target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    target.parent.chmod(0o755)
    staging = target.with_name(target.name + ".staging")
    staging.mkdir(mode=0o755)
    staging.chmod(0o755)  # Remain readable by the Drama service under umask 077.
    for rows in base["categories"].values():
        for item in rows:
            shutil.copyfile(item["path"], staging / item["name"])
    for item in metadata["additions"]:
        source = bundle / "layers" / item["file"]
        if hashlib.sha256(source.read_bytes()).hexdigest() != item["sha256"]:
            raise RuntimeError("addition_sha256_mismatch")
        destination = staging / item["file"]
        if destination.exists():
            raise RuntimeError("addition_would_overwrite_original")
        shutil.copyfile(source, destination)
    shutil.copyfile(bundle / "manifest.json", staging / "manifest.json")
    assets = load_asset_set(staging, metadata["manifest_sha256"])
    for path in staging.iterdir():
        path.chmod(0o444)
    os.rename(staging, target)
    print(json.dumps({"reused": False, "root": str(target), "sha256": metadata["manifest_sha256"], "counts": {k: len(v) for k,v in assets["categories"].items()}}))


if __name__ == "__main__":
    main()
