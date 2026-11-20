"""Release the FB automatic-template original-video option using the existing FB switch."""
import argparse
from contextlib import closing
import difflib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
from scripts import deploy_hit_drama as release, deploy_fb_skipped_recheck as fb
from scripts.deploy_fb_profile_priority import external, owner_active
from scripts.deploy_pipeline_runtime import preserve_tree_owners, verify_source

BASE = "7a1cb33957ef8d7adc45e34f368d6a88192bb43c"
STATIC = ["static/fb-auto-publish-" + page + ext for page in ("template", "templates") for ext in (".html", ".js")]
TEST = "tests.test_fb_original_template"


def update_static(path, old, new):
    current = path.read_bytes()
    if current == old:
        return new
    # The public HTML has its own navigation and shared-script versions.
    before, after = old.splitlines(True), new.splitlines(True)
    for op, i, j, k, end in difflib.SequenceMatcher(a=before, b=after, autojunk=False).get_opcodes():
        if op != "equal":
            block = b"".join(before[i:j])
            if not block or current.count(block) != 1:
                raise RuntimeError("static context changed: " + str(path))
            current = current.replace(block, b"".join(after[k:end]), 1)
    return current


def prepare(folder):
    commit = verify_source(SOURCE, "HEAD")
    current, _, _ = release.settings("fb")
    previous = current.resolve()
    candidate = folder / "candidate"
    folder.mkdir(parents=True, exist_ok=False)
    folder.chmod(0o755)
    shutil.copytree(previous, candidate, symlinks=True, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
    preserve_tree_owners(previous, candidate)
    info = dict(platform="fb", source=str(SOURCE), commit=commit, previous=str(previous),
                candidate=str(candidate), files={}, static={}, status="prepared")
    changed = release.run("git", "-C", SOURCE, "diff", "--name-only", BASE, commit, "--",
                          "features/fb_auto_posts", "features/pipeline_dispatch/fb_native.py", *STATIC).splitlines()
    for name in changed:
        dest = candidate / name
        assert dest.resolve().is_relative_to(candidate) and not dest.is_symlink()
        prior = subprocess.run(["git", "-C", str(SOURCE), "show", BASE + ":" + name], capture_output=True)
        assert (prior.returncode == 0) == dest.is_file(), "file presence changed: " + name
        old = prior.stdout.replace(b"\r\n", b"\n")
        new = (SOURCE / name).read_bytes().replace(b"\r\n", b"\n")
        if dest.is_file() and dest.read_bytes().replace(b"\r\n", b"\n") != old:
            raise RuntimeError("FB runtime changed: " + name)
        if name.endswith(".py"):
            compile(new, name, "exec")
        release.atomic(dest, new)
        info["files"][name] = dict(before=release.sha(previous / name), after=release.sha(dest))
        if name in STATIC:
            for public_root in (release.MAIN, release.WEB):
                path = public_root / dest.name
                external(folder, info, path, update_static(path, old, new))
    test_file = TEST.replace(".", "/") + ".py"
    shutil.copy2(SOURCE / test_file, candidate / test_file)
    with tempfile.TemporaryDirectory(dir=folder) as temp:
        env = dict(os.environ, STORY_COPY_ROOT=temp, FB_AUTO_PROFILE_ROOT=temp, FB_AUTO_POST_SHORT_LINK_ROOT=temp)
        proof = subprocess.run([sys.executable, "-B", "-m", "unittest", "-q", TEST, "scripts.test_fb_auto_validation",
                                "scripts.test_fb_hit_drama", "tests.test_fb_hit_material_batch.BatchTests",
                                "tests.test_fb_mobo_launch_window", "tests.test_fb_skipped_recovery"],
                               cwd=candidate, env=env, capture_output=True, text=True, timeout=180)
    (folder / "tests.txt").write_text(proof.stdout + proof.stderr)
    proof.check_returncode()
    release.save(folder / "manifest.json", info)
    print(json.dumps(dict(status="prepared", commit=commit, folder=str(folder))), flush=True)


def rollback_ready():
    _, _, db = release.settings("fb")
    with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as conn:
        in_use = conn.execute("""SELECT EXISTS(SELECT 1 FROM fb_auto_task WHERE video_template='original'
            AND status IN ('planned','preparing','ready','running','queued')) OR EXISTS(
            SELECT 1 FROM fb_auto_template t JOIN fb_auto_template_version v ON v.template_id=t.id
            AND v.version=t.current_version WHERE t.status<>'deleted' AND json_extract(v.config_json,'$.video_template')='original')""").fetchone()[0]
    if in_use:
        raise RuntimeError("旧版本无法执行或编辑原视频模板。请先处理使用该选项的模板，并等待其在途发布落账。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "apply", "rollback"))
    parser.add_argument("--folder", type=Path, required=True)
    args = parser.parse_args()
    folder = args.folder.resolve()
    assert folder.is_relative_to("/mnt/data-disk")
    assert release.run("findmnt", "-rn", "-o", "UUID", "--mountpoint", "/mnt/data-disk") == "3e8ac4e8-7770-456d-9e89-2ec5dd405fa8"
    if args.action == "prepare":
        prepare(folder)
    else:
        if args.action == "rollback":
            rollback_ready()
        fb.native_active = owner_active
        release.activate("fb", folder, args.action == "rollback", defer_plan_read=False)
