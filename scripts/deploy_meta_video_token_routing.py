"""Deploy queue-based credential routing; preserve all deletion ledger data."""
import argparse
from pathlib import Path
import deploy_meta_asset_recovery as rollout
from deploy_meta_video_delete_continue import prepare


if __name__ == "__main__":
    rollout.BASE = "8798f8b098c3cd5f2e0b881c6a196399f704bff6"
    rollout.BASELINE_HASHES = {}
    rollout.FILES = ["features/fb_ad_asset_delete/" + name + ".py" for name in ("source", "graph", "store", "service")]
    rollout.FILES += ["static/fb-post-ad-delete.html", "static/fb-post-ad-delete.js"]
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("prepare", "apply", "rollback"))
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    if args.mode == "prepare":
        prepare(args.path)
    else:
        rollout.switch(args.path, args.mode == "rollback")
