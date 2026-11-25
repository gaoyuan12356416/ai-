"""Install the central worker and the FB reader using their own scoped switches."""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from contextlib import closing

SOURCE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SOURCE))
from ops.story_copy import deploy as d, deploy_no_history as central
from features.story_copy import store

BASE = "6cf08e4a14febc36aa2970d74e3edb312c7fbef6"
BRIDGE = "features/story_copy/bridge.py"
central.FILES = tuple("features/story_copy/" + name + ".py" for name in
                      ("bridge", "generator", "sources", "store", "worker"))
central.SERVICES = ("story-copy-worker.service", "story-copy-settings.service",
                    "fb-auto-post-service.service", "fb-video-publish-worker.service")


def prepare_fb(folder):
    old = Path("/opt/fb-auto-post/current").resolve(strict=True)
    base_release = json.loads((old.parent / "manifest.json").read_text())
    # Apply just the reader delta. The FB bridge has independent supplier-link logic.
    folder.mkdir()
    candidate = folder / "candidate"
    subprocess.run(["cp", "-a", old, candidate], check=True)
    patch = subprocess.check_output(["git", "-C", SOURCE, "diff", BASE, "HEAD", "--", BRIDGE])
    for args in (["--check"], []):
        subprocess.run(["git", "apply", *args, "-"], cwd=candidate, input=patch, check=True)
    compile((candidate / BRIDGE).read_bytes(), BRIDGE, "exec")
    info = dict(platform="fb", previous=str(old), candidate=str(candidate), static={}, status="prepared",
                # Successive planner deployments use commit/source to identify their own base.
                commit=base_release["commit"], source=base_release["source"],
                caption_commit=d.read("git", "-C", SOURCE, "rev-parse", "HEAD"),
                files={BRIDGE: dict(before=d.sha((old / BRIDGE).read_bytes()),
                                    after=d.sha((candidate / BRIDGE).read_bytes()))})
    d.atomic(folder / "manifest.json", json.dumps(info, indent=2).encode())


def settled_before_handoff(c, job_id):
    # The native reservation can precede a dispatch failure. The GPU only
    # receives a descriptor after pipeline_fb_claim commits. All predicates
    # must still hold when the FB controller rechecks after draining handlers.
    return c.execute("""
        SELECT 1 FROM dispatch.pipeline_job j JOIN fb_auto_task t ON t.id=j.source_id
        WHERE j.id=? AND j.source_key='fb:auto:'||t.id AND j.channel='fb' AND j.family='auto'
        AND j.stage='prepare' AND j.status='failed' AND j.error_code='fb_auto_internal_error'
        AND j.finished_at=j.updated_at AND julianday(j.lease_expires_at)<julianday('now')
        AND j.external_intent=0 AND coalesce(j.canonical_receipt,'')=''
        AND t.status='preparing' AND t.lease_owner='pipeline:'||j.node_id||':'||j.generation
        AND t.unknown_outcome=0 AND coalesce(t.graph_post_id,'')='' AND coalesce(t.prepared_at_utc,'')=''
        AND EXISTS(SELECT 1 FROM dispatch.pipeline_event e WHERE e.job_id=j.id AND e.event_type='failed'
                   AND json_extract(e.details_json,'$.unknown')=0 AND json_extract(e.details_json,'$.code')=j.error_code)
        AND NOT EXISTS(SELECT 1 FROM dispatch.pipeline_job other WHERE other.source_key=j.source_key AND other.id<>j.id)
        AND NOT EXISTS(SELECT 1 FROM pipeline_fb_claim n WHERE n.source_key=j.source_key)
        AND NOT EXISTS(SELECT 1 FROM fb_auto_publish_attempt a WHERE a.task_id=t.id)
        AND NOT EXISTS(SELECT 1 FROM dispatch.pipeline_operation o WHERE o.job_id=j.id)
        AND NOT EXISTS(SELECT 1 FROM dispatch.pipeline_receipt r WHERE r.job_id=j.id)
    """, (job_id,)).fetchone() is not None


def switch_fb(folder, reverse, settled_prepare=None):
    info = json.loads((folder / "manifest.json").read_text())
    # Use the controller for the running FB base, including its native lease checks.
    sys.path.insert(0, info["source"])
    from scripts import deploy_hit_drama as release, deploy_fb_skipped_recheck as fb
    from scripts.deploy_fb_profile_priority import owner_active
    def active():
        count = owner_active()
        if count != 1 or settled_prepare is None:
            return count
        with closing(sqlite3.connect(fb.FB_DB.as_uri() + "?mode=ro", uri=True)) as c:
            c.execute("ATTACH DATABASE ? AS dispatch", (fb.DISPATCH_DB.as_uri() + "?mode=ro",))
            return count - int(settled_before_handoff(c, settled_prepare))
    fb.native_active = active
    if Path("/opt/fb-auto-post/current").resolve() != Path(info["previous" if reverse else "candidate"]):
        release.activate("fb", folder, reverse, defer_plan_read=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "prepare-central-update", "apply-central", "apply-fb", "rollback"))
    parser.add_argument("folder", type=Path)
    parser.add_argument("--fb-release", type=Path, help="FB reader release when a separate central deployment followed it")
    parser.add_argument("--settled-prepare", type=int, help="Verify one terminal dispatch before GPU handoff during FB activation")
    args = parser.parse_args()
    folder = args.folder.resolve()
    if (folder.parent != Path("/mnt/data-disk/story-copy-deploy") or not os.path.ismount("/mnt/data-disk")
            or d.read("findmnt", "-n", "-o", "UUID", "--mountpoint", "/mnt/data-disk") != "3e8ac4e8-7770-456d-9e89-2ec5dd405fa8"):
        raise SystemExit("unexpected deployment path or data disk")
    if args.action == "prepare":
        central.prepare(folder, base=BASE)
        backup = folder / "captions-before.sqlite3"
        with store.connection(readonly=True) as src, closing(sqlite3.connect(backup)) as dst:
            src.backup(dst)
        backup.chmod(0o600)
        prepare_fb(folder / "fb")
    elif args.action == "prepare-central-update":
        previous = d.CENTRAL.resolve(strict=True).parent
        previous_record = json.loads((previous / "record.json").read_text())
        d.run("git", "-C", SOURCE, "merge-base", "--is-ancestor", previous_record["commit"], "HEAD")
        fb_release = args.fb_release or Path(previous_record.get("fb_reliability_release", previous))
        fb_manifest = json.loads((fb_release / "fb" / "manifest.json").read_text())
        d.run("git", "-C", SOURCE, "merge-base", "--is-ancestor", fb_manifest["caption_commit"], "HEAD")
        central.FILES = ("features/story_copy/sources.py", "features/story_copy/store.py")
        central.prepare(folder, base=previous_record["commit"])
        record = json.loads((folder / "record.json").read_text())
        d.save(folder, dict(record, previous_reliability_release=str(previous),
                            fb_reliability_release=str(fb_release.resolve())))
    elif args.action == "apply-central":
        central.switch(folder, False)
    elif args.action == "apply-fb":
        switch_fb(folder / "fb", False, args.settled_prepare)
    else:
        current = folder
        record = json.loads((current / "record.json").read_text())
        fb_release = record.get("fb_reliability_release")
        while "previous_reliability_release" in record:
            if d.CENTRAL.resolve() == Path(record["candidate"]):
                central.switch(current, True)
            current = Path(record["previous_reliability_release"])
            record = json.loads((current / "record.json").read_text())
        fb_root = Path(fb_release) if fb_release else current
        switch_fb(fb_root / "fb", True, args.settled_prepare)
        if current == fb_root:
            central.switch(current, True)
