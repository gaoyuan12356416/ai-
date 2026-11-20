"""Install/roll back only the FB template list assets; never restart services."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

SOURCE = Path(__file__).resolve().parents[1]
BASELINES = {
    "fb-auto-publish-templates.js": {
        "82406dbda72ecda064a606112839e93c419e8ea97180cd966439afeb44b06489",
        "57cccc45e9a2dfe30d6daa28e6234edeb23a0fcae22caefea9337c3407b075ef",
    },
    "fb-auto-publish-templates.html": {
        "72d7c9204dc28f5614706171db10aa117cdad38ae85e39807668e191ba3e63af",
        "754d9a90aa9aea3ae6a1fea6d10856a84cdf1412fd31a74d001bf51302bd53f6",
    },
    "fb-auto-publish-templates.css": {"7dc7edccec1bfb80d4a0df1b50a933f3e63e892efe047e54d3a3e351d13aa895", None},
}


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def services():
    return run("systemctl", "show", "drama-material-api.service", "fb-auto-post-service.service",
               "-p", "Id", "-p", "MainPID", "-p", "ActiveState")


def atomic(path, data, row):
    temporary = path.with_name(path.name + ".status-ui-" + str(os.getpid()))
    try:
        temporary.write_bytes(data)
        os.chmod(temporary, row["mode"])
        os.chown(temporary, row["uid"], row["gid"])
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def install(folder):
    assert run("findmnt", "-no", "UUID", "/mnt/data-disk") == "3e8ac4e8-7770-456d-9e89-2ec5dd405fa8"
    assert not run("git", "-C", str(SOURCE), "status", "--porcelain"), "source must be clean"
    commit = run("git", "-C", str(SOURCE), "rev-parse", "HEAD")
    assert run("git", "-C", str(SOURCE), "branch", "-r", "--contains", commit), "commit must come from GitHub"
    run("node", "--check", str(SOURCE / "static/fb-auto-publish-templates.js"))
    runtime = Path("/opt/fb-auto-post/current").resolve()
    roots = [Path("/root/drama_material_service/static").resolve(), runtime / "static", Path("/usr/share/nginx/html")]
    info = {"commit": commit, "runtime": str(runtime), "services_before": services(), "files": {}, "status": "prepared"}
    for name, expected in BASELINES.items():
        source = SOURCE / "static" / name
        for root in roots:
            dest = root / name
            before = digest(dest)
            assert before in expected, "preserve concurrent edits: " + str(dest)
            stat = dest.stat() if dest.exists() else None
            info["files"][str(dest)] = {
                "source": name, "before": before, "after": digest(source),
                "mode": stat.st_mode & 0o777 if stat else 0o644,
                "uid": stat.st_uid if stat else 0, "gid": stat.st_gid if stat else 0,
                "backup": str(folder / (str(len(info["files"])) + ".before")),
            }
    folder.mkdir(parents=True, exist_ok=False)
    os.chmod(folder, 0o700)
    for dest, row in info["files"].items():
        if row["before"] is not None:
            shutil.copy2(dest, row["backup"])
            assert digest(Path(row["backup"])) == row["before"]
    manifest = folder / "manifest.json"
    manifest.write_text(json.dumps(info, indent=2))
    assert str(Path("/opt/fb-auto-post/current").resolve()) == info["runtime"], "runtime changed"
    # Publish JS/CSS first, then the HTML that references their new cache version.
    for dest, row in sorted(info["files"].items(), key=lambda entry: entry[0].endswith(".html")):
        path = Path(dest)
        assert digest(path) == row["before"], "preserve concurrent edits: " + dest
        atomic(path, (SOURCE / "static" / row["source"]).read_bytes(), row)
    assert all(digest(Path(dest)) == row["after"] for dest, row in info["files"].items())
    info.update(status="active", services_after=services(), restarted=False)
    manifest.write_text(json.dumps(info, indent=2))
    print(json.dumps({"commit": commit, "files": len(info["files"]), "backup": str(folder),
                      "restarted": False, "service_pids_unchanged": info["services_after"] == info["services_before"]}))


def rollback(folder):
    info = json.loads((folder / "manifest.json").read_text())
    assert str(Path("/opt/fb-auto-post/current").resolve()) == info["runtime"], "check the newer runtime before rollback"
    for dest, row in info["files"].items():
        assert digest(Path(dest)) in {row["before"], row["after"]}, "preserve newer edits: " + dest
    # Restore HTML first so it no longer references the newly added stylesheet.
    for dest, row in sorted(info["files"].items(), key=lambda entry: not entry[0].endswith(".html")):
        path = Path(dest)
        if row["before"] is None:
            path.unlink(missing_ok=True)
        else:
            data = Path(row["backup"]).read_bytes()
            assert hashlib.sha256(data).hexdigest() == row["before"]
            atomic(path, data, row)
    assert all(digest(Path(dest)) == row["before"] for dest, row in info["files"].items())
    print("Static rollback complete; no service restart or business data change")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["install", "rollback"])
    parser.add_argument("--folder", type=Path, required=True)
    args = parser.parse_args()
    {"install": install, "rollback": rollback}[args.action](args.folder.resolve())
