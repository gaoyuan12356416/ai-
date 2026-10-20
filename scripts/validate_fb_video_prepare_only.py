"""One prepare-only GPU canary with an isolated SQLite ledger; never calls Meta.

Review the source URL/content before --execute. Re-running the same source uses
one stable GPU job. Only the explicit evidence directory and GPU cache change.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.fb_auto_posts.gpu import GPUPrepareClient, PROFILE
from features.fb_video_publish.core import Store, canonical, stamp
from features.fb_video_publish.sources import prepare_source_url
from features.fb_video_publish.worker import Worker


class Forbidden:
    def __getattr__(self, name):
        raise AssertionError("Platform, Page and link operations are prohibited in prepare-only validation")


def content_id(value):
    """Opaque source identity, bounded by the existing GPU API's 128 chars."""
    text = str(value or "").strip()
    if not 1 <= len(text) <= 128 or any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise ValueError("Content ID must be a non-empty source identity of at most 128 characters")
    try:
        text.encode("utf-8")
    except UnicodeError:
        raise ValueError("Content ID must be valid UTF-8") from None
    return text


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--content-id", required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    source, content = prepare_source_url(args.source_url), content_id(args.content_id)
    root = args.evidence_dir.resolve()
    prohibited = (Path("/mnt/data-disk/fb-auto-post-publisher"), Path("/mnt/data-disk/fb-auto-post-public"))
    if not args.evidence_dir.is_absolute() or any(root == p or p in root.parents for p in prohibited):
        raise ValueError("Use an absolute isolated evidence directory outside production state and links")
    identity = hashlib.sha256(("fb-video-prepare-canary-v1|" + content + "|" + source).encode()).hexdigest()
    job = "fb-page-" + identity[:48]
    print(json.dumps({"prepare_only": True, "job_id": job, "content_id": content,
                      "source_sha256": hashlib.sha256(source.encode()).hexdigest(), "execute": args.execute}), flush=True)
    if not args.execute:
        return
    root.mkdir(parents=True, exist_ok=True)
    db = root / "prepare-only.sqlite3"
    if db.is_symlink():
        raise ValueError("Evidence database must not be a symlink")
    store = Store(db)
    tid, now = identity[:32], stamp(datetime.now(timezone.utc))
    with store.connect() as conn:
        conn.execute("""INSERT OR IGNORE INTO fb_video_task(task_id,batch_id,tenant_key,owner_user_id,
            page_id,material_id,content_id,sequence,page_sequence,planned_at_utc,planned_bj_date,
            processing_mode,snapshot_json,gpu_job_id,prepare_state,created_at_utc,updated_at_utc)
            VALUES(?,?, 'prepare-only','prepare-only','1','1',?,1,1,?,?,'random_overlay',?,?,'planned',?,?)""",
            (tid, tid, content, now, now[:10], canonical({"material":{"media_url":source}}), job, now, now))
        row = conn.execute("SELECT prepare_state,prepared_json FROM fb_video_task WHERE task_id=?", (tid,)).fetchone()
        if conn.execute("SELECT count(*) FROM fb_video_task").fetchone()[0] != 1:
            raise ValueError("Canary evidence database contains unrelated manual tasks")
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='fb_auto_task'").fetchone():
            raise ValueError("Canary must not share an automatic operational database")
    if row["prepare_state"] != "ready":
        gpu = GPUPrepareClient(os.environ.get("FB_AUTO_GPU_PREPARE_INTERNAL_TOKEN", ""))
        worker = Worker(store, Forbidden(), gpu, Forbidden(), live_enabled=True, prepare_enabled=True,
                        link_writer=Forbidden(), readback=Forbidden())
        result = worker.prepare_next("prepare-only-canary")
        if result["status"] != "stage_complete":
            raise RuntimeError("Canary did not complete: " + result["status"])
    with store.connect() as conn:
        row = conn.execute("SELECT * FROM fb_video_task WHERE task_id=?", (tid,)).fetchone()
        assert conn.execute("SELECT count(*) FROM fb_video_attempt").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM fb_video_link").fetchone()[0] == 0
        assert row["status"] == "queued" and row["prepare_state"] == "ready"
    prepared = json.loads(row["prepared_json"])
    assert prepared["profile"] == PROFILE and prepared["sha256"] and prepared["media_url"] != source
    assert prepared.get("random_overlay_recipe"), "Missing frozen random recipe"
    evidence = {"prepare_only": True, "job_id": job, "task_state": row["status"],
                "prepare_state": row["prepare_state"], "platform_attempts": 0, "links": 0,
                "prepared": prepared, "verified_at_utc": stamp(datetime.now(timezone.utc))}
    (root / "evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(evidence, ensure_ascii=False))


if __name__ == "__main__":
    main()
