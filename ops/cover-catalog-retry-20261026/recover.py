"""Audited callback-only recovery for the operator's exact, fully generated job.

No image generation, broad queue recovery, or direct material-database writes.
"""
import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from features import cover_material_source as material

JOB_ID = "e2a511bd8a664b6b8279e7c3a42f42d8"
STATE = Path("/mnt/data-disk/codex/tasks/cover-catalog-retry-20261026")
DB = Path(material.SOURCE_DB)


def save(name, data):
    path = STATE / name
    with path.open("x", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())


def job():
    with contextlib.closing(material.readonly(DB)) as conn:
        row = conn.execute("SELECT * FROM drama_screenshot_job WHERE job_id=?", (JOB_ID,)).fetchone()
    if not row:
        raise RuntimeError("target_job_missing")
    return dict(row)


def uploads():
    with contextlib.closing(material.readonly(Path(material.ROOT) / "upload.sqlite3")) as conn:
        rows = conn.execute("SELECT * FROM uploads WHERE source_json LIKE ? ORDER BY key", ("%" + JOB_ID + "%",)).fetchall()
    return [dict(row) for row in rows]


def preflight(current):
    if (current["job_id"] != JOB_ID or current["app_id"] != "3360"
            or current["content_id"] != "132516419" or current["status"] != "failed"
            or not current["error_message"].startswith("catalog_read_failed_2013\n")):
        raise RuntimeError("target_state_changed")
    if not all(current[size + "_url"] for size, _ in material.SPECS):
        raise RuntimeError("callback_only_requires_all_images")


def environment():
    pid = subprocess.check_output(["systemctl", "show", "drama-material-api.service", "-p", "MainPID", "--value"], text=True).strip()
    entries = Path("/proc/" + pid + "/environ").read_bytes().split(b"\0")
    return {k.decode(): v.decode() for k, v in (entry.split(b"=", 1) for entry in entries if b"=" in entry)}


def freeze():
    current = job()
    preflight(current)
    before = uploads()
    if sorted((row["state"], row["attempts"]) for row in before) != [("confirmed", 1), ("confirmed", 1), ("prepared", 0)]:
        raise RuntimeError("target_upload_states_changed")
    source = dict(current, assets=json.loads(current["assets_json"]))
    images = material.screenshot_sources(source)
    if {material.identity(image) for image in images} != {row["key"] for row in before}:
        raise RuntimeError("image_identity_changed")
    save("before.json", {"job": current, "uploads": before, "images": images, "at": material.utcnow()})
    for label, original in (("source", DB), ("uploads", Path(material.ROOT) / "upload.sqlite3")):
        dest = STATE / (label + "-before.sqlite3")
        if dest.exists():
            raise RuntimeError("backup_already_exists")
        with contextlib.closing(material.readonly(original)) as src, sqlite3.connect(dest) as dst:
            src.backup(dst)
            if dst.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError("backup_integrity_failed")
    print(json.dumps({"frozen": JOB_ID, "confirmed": 2, "unattempted": 1, "images_verified": 3}))


def recover():
    frozen = json.loads((STATE / "before.json").read_text())
    current = job()
    preflight(current)
    if current != frozen["job"] or uploads() != frozen["uploads"]:
        raise RuntimeError("frozen_state_drifted_reconcile_before_retry")
    start = time.monotonic()
    # This tested callback skips confirmed signatures and submits only prepared ones.
    # Durable unknown/accepted outcomes can only reconcile, never repeat a POST.
    result = material.upload_screenshot(dict(current, assets=json.loads(current["assets_json"])), environment())
    after = uploads()
    if len(after) != 3 or any(row["state"] != "confirmed" or row["attempts"] != 1 for row in after):
        raise RuntimeError("three_unique_receipts_required")
    for row in after:
        previous = next(item for item in frozen["uploads"] if item["key"] == row["key"])
        if previous["state"] == "confirmed" and row != previous:
            raise RuntimeError("existing_receipt_changed")
    public = material.screenshot_sources(dict(current, assets=json.loads(current["assets_json"])))
    if [(x["source_key"], x["sha256"]) for x in public] != [(x["source_key"], x["sha256"]) for x in frozen["images"]]:
        raise RuntimeError("public_images_changed")
    save("callback-result.json", {"result": result, "uploads": after, "images": public, "at": material.utcnow()})
    # Compare the complete row under SQLite's writer lock. A concurrent operator
    # action must not be overwritten, even after successful callback completion.
    with sqlite3.connect(DB, timeout=15) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        latest = conn.execute("SELECT * FROM drama_screenshot_job WHERE job_id=?", (JOB_ID,)).fetchone()
        if dict(latest) != current:
            raise RuntimeError("job_changed_after_callback_no_status_overwrite")
        now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("UPDATE drama_screenshot_job SET status='done', progress=100, progress_detail=?, error_message='', "
                     "finished_at=?, updated_at=?, elapsed_seconds=? WHERE job_id=? AND status='failed'",
                     ("三种截图素材已全部生成并回传；已恢复素材库连接中断的回传", now, now,
                      int(current["elapsed_seconds"] or 0) + round(time.monotonic() - start), JOB_ID))
    save("recovered.json", {"job": job(), "result": result, "at": material.utcnow(), "code_sha256": hashlib.sha256(Path(material.__file__).read_bytes()).hexdigest()})
    print(json.dumps({"job_id": JOB_ID, "status": job()["status"], "confirmed_ids": result["confirmed_ids"],
                      "new_posts": 1, "preserved_images": 3}))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("freeze", "recover"))
    args = parser.parse_args()
    mounted = subprocess.check_output(["findmnt", "-n", "-o", "UUID", "--target", "/mnt/data-disk"], text=True).strip()
    if mounted != "3e8ac4e8-7770-456d-9e89-2ec5dd405fa8":
        raise RuntimeError("data_disk_unavailable")
    os.umask(0o077)
    STATE.mkdir(parents=True, exist_ok=True)
    with material.file_lock(STATE / "operation.lock"):
        freeze() if args.action == "freeze" else recover()


if __name__ == "__main__":
    main()
