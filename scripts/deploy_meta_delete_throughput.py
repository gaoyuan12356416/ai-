"""Deploy bounded Meta deletion throughput without changing ledger history."""
import argparse
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import subprocess

import deploy_meta_asset_recovery as rollout


BASE = "9c2178fa7b08899e3ffcf360dc114c44596ae76c"
FILES = ["features/fb_ad_asset_delete/" + name + ".py" for name in
         ("bridge", "execution", "graph", "service", "source", "store")]
API_SERVICE = "drama-material-api.service"


def require_api_stopped():
    """Avoid loading a mixture of old and new modules during either switch."""
    result = subprocess.run(["systemctl", "show", API_SERVICE,
        "--property=LoadState", "--property=ActiveState", "--property=MainPID"],
        check=True, capture_output=True, text=True)
    state = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if (state.get("LoadState"), state.get("ActiveState"), state.get("MainPID")) != ("loaded", "inactive", "0"):
        raise RuntimeError("Stop %s and confirm it is inactive with MainPID=0 before switching code" % API_SERVICE)


def prepare(release):
    captured = io.StringIO()
    with redirect_stdout(captured):
        rollout.prepare(release)
    result = json.loads(captured.getvalue())
    backup = Path(result["backup"])
    plan = json.loads((backup / "plan.json").read_text())
    # Shared recovery intentionally keeps newly added modules during rollback;
    # the old entry points do not import them. Permit that exact retained hash
    # on a same-backup reapply, without accepting arbitrary file drift.
    for item in plan["files"]:
        if item["before"] is None:
            item["rollback_after"] = item["after"]
    (backup / "plan.json").write_text(json.dumps(plan, indent=2))
    (backup / "hashes.json").write_text(json.dumps({str(path.relative_to(backup)): rollout.digest(path)
        for path in backup.rglob("*") if path.is_file() and path.name != "hashes.json"}, indent=2))
    print(json.dumps(result))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "apply", "rollback"))
    parser.add_argument("path", type=Path)
    args = parser.parse_args(argv)
    rollout.BASE, rollout.BASELINE_HASHES, rollout.FILES = BASE, {}, FILES
    if args.mode == "prepare":
        prepare(args.path)
    else:
        require_api_stopped()
        rollout.switch(args.path, args.mode == "rollback")


if __name__ == "__main__":
    main()
