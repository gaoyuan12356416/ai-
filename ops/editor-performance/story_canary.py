"""Render a read-only source receipt into a new, isolated studio and media ledger."""

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from features.ai_video_editing import render_worker, smart_api, studio
from features.ai_video_editing.runtime import Runtime, owner

ACTOR = {"tenant_key": "isolated-story-acceptance", "user_id": "blind-review"}


def wait(rt, sid, rid):
    while True:
        studio.tick(rt)
        run = next(r for r in studio.session(rt, ACTOR, sid)["runs"] if r["id"] == rid)
        if run["status"] == "completed" or run["tasks"]:
            return run
        if run["status"] not in studio.ACTIVE:
            raise RuntimeError(run["error"] or run["status"])
        time.sleep(0.3)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--analysis", required=True)
    parser.add_argument("--scratch", type=Path, required=True)
    args = parser.parse_args()
    root = args.source_root.resolve()
    folder = args.scratch.resolve()
    if folder == root or root in folder.parents:
        raise RuntimeError("Use a new scratch directory outside the source data root")
    folder.mkdir(mode=0o700, parents=True, exist_ok=False)
    rt = Runtime(folder / "private")
    studio.setup(rt)
    with closing(sqlite3.connect((root / "editor.sqlite3").as_uri() + "?mode=ro", uri=True)) as src:
        src.row_factory = sqlite3.Row
        receipt = src.execute(
            "SELECT owner,sources FROM ai_edit_smart_analysis WHERE id=? AND status='succeeded'",
            (args.analysis,),
        ).fetchone()
        if not receipt:
            raise RuntimeError("Completed source receipt missing")
        sources = json.loads(receipt["sources"])
        with rt.db(True) as db:
            db.execute("UPDATE studio_story_defaults SET version=2 WHERE singleton=1")
            for s in sources:
                aid = s["source_id"]
                a = src.execute(
                    "SELECT * FROM assets WHERE owner=? AND id=?", (receipt["owner"], aid)
                ).fetchone()
                original = root / "uploads" / (aid + ".mp4")
                if not a or render_worker.fingerprint(original) != a["sha256"] or a["sha256"] != s["_stamp"]:
                    raise RuntimeError("Expected verified, locally available source file")
                target = rt.root / "uploads" / original.name
                try:
                    os.link(original, target)
                except OSError:
                    shutil.copyfile(original, target)
                db.execute(
                    "INSERT INTO assets VALUES(?,?,?,?,?,?,?,?)",
                    (
                        aid,
                        owner(ACTOR),
                        a["name"],
                        a["series"],
                        a["episode"],
                        a["sha256"],
                        a["meta"],
                        time.time(),
                    ),
                )
    smart_api.assemble(rt, ACTOR, sources)
    item = studio.dispatch(
        rt, ACTOR, "POST", "/sessions", {"source_ids": [s["source_id"] for s in sources]}, {}
    )["item"]
    sid = item["id"]
    run = studio.submit(rt, ACTOR, sid, {"operation_id": "preview"}, "plan")
    pr = wait(rt, sid, run["run_id"])["edit_plan_id"]
    plan = studio.dispatch(rt, ACTOR, "GET", "/sessions/" + sid + "/edit-plans/" + pr, {}, {})["item"]
    if plan["conflicts"]:
        raise RuntimeError("Selected sources need attention: " + str(plan["conflicts"]))
    run = studio.submit(rt, ACTOR, sid, {"operation_id": "export", "edit_plan_id": pr})
    pending = wait(rt, sid, run["run_id"])
    assert pending["edit_plan_id"] == pr
    job = rt.claim()
    assert len(pending["tasks"]) == 1 and job["id"] == pending["tasks"][0]["id"]
    lookup = {
        c["source_id"]: rt.worker_asset(job["id"], job["lease"], c["source_id"])["path"]
        for c in job["unit"]["clips"]
    }
    cuts = lambda items: [(c["source_id"], c["start_ms"], c["end_ms"]) for c in items]
    assert cuts(job["unit"]["clips"]) == cuts(plan["outputs"][0]["segments"])
    print("rendering isolated timeline", pr, len(job["unit"]["clips"]), flush=True)
    output = folder / "blind-review.mp4"
    render_worker.render(
        job["unit"],
        lookup,
        output,
        folder / "encode",
        encoder="libx264",
        ffmpeg=os.environ.get("AI_VIDEO_EDITING_FFMPEG", "ffmpeg"),
        ffprobe=os.environ.get("AI_VIDEO_EDITING_FFPROBE", "ffprobe"),
    )
    digest = render_worker.fingerprint(output)
    rt.update_job(job["id"], job["lease"], "complete", output, digest)
    shutil.copyfile(rt.root / "outputs" / (job["id"] + ".mp4"), output)
    while studio.session(rt, ACTOR, sid)["runs"][-1]["status"] != "completed":
        studio.tick(rt)
        time.sleep(0.2)
    report = {
        "session": sid,
        "run": run["run_id"],
        "task": job["id"],
        "plan": plan,
        "sha256": digest,
        "media": rt.list_records("tasks", ACTOR, job["id"])["item"]["output"]["metadata"],
    }
    (folder / "acceptance.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8")
    print(json.dumps({k: v for k, v in report.items() if k != "plan"}))


if __name__ == "__main__":
    main()
