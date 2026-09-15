"""Deploy account-scoped Meta Video deletion from an exact GitHub release."""
import argparse
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import deploy_meta_asset_recovery as rollout


def guard_video_rollback(source):
    """Older runtimes must never replay account failures as node deletions."""
    marker = "    def delete(self, obj, prepared=None):\n"
    assert source.count(marker) == 1, "rollback Graph adapter drift"
    guarded = source.replace(marker, marker +
        '        if obj.get("kind") == "video":\n'
        '            raise GraphError("video_execution_rolled_back", "Video execution is disabled after rollback; restore the account-video release before retrying")\n', 1)
    compile(guarded, "rollback_video_graph.py", "exec")
    return guarded


def prepare(release):
    captured = io.StringIO()
    with redirect_stdout(captured):
        rollout.prepare(release)
    result = json.loads(captured.getvalue())
    backup = Path(result["backup"])
    plan = json.loads((backup / "plan.json").read_text())
    item = next(x for x in plan["files"] if x["source"] == "features/fb_ad_asset_delete/graph.py")
    guard = backup / "rollback_video_graph.py"
    guard.write_text(guard_video_rollback((backup / item["saved"]).read_text()), encoding="utf-8")
    item.update(rollback_saved=guard.name, rollback_after=rollout.digest(guard))
    (backup / "plan.json").write_text(json.dumps(plan, indent=2))
    (backup / "hashes.json").write_text(json.dumps({str(p.relative_to(backup)): rollout.digest(p)
        for p in backup.rglob("*") if p.is_file() and p.name != "hashes.json"}, indent=2))
    print(json.dumps(dict(result, rollback_video_execution="disabled")))


if __name__ == "__main__":
    rollout.BASE = "b0d5790d3b5b637b27ff22cc53785cc4df82ea37"
    rollout.BASELINE_HASHES = {}
    rollout.FILES = ["features/fb_ad_asset_delete/" + name + ".py"
                     for name in ("bridge", "graph", "service", "source", "store")]
    rollout.FILES += ["static/fb-post-ad-delete." + ext for ext in ("html", "js", "css")]
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("prepare", "apply", "rollback"))
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    if args.mode == "prepare":
        prepare(args.path)
    else:
        rollout.switch(args.path, args.mode == "rollback")
