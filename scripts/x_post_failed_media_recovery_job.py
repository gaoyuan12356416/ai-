#!/usr/bin/env python3
"""Finish repaired-media preparation, re-arm exact runs, and dispatch once."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path


def _load_env_file(path):
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, value = line.split("=", 1)
        parsed = shlex.split(value, posix=True)
        if len(parsed) != 1:
            raise RuntimeError("invalid environment entry: %s" % key.strip())
        os.environ[key.strip()] = parsed[0]


def _run(command, *, check=True):
    return subprocess.run(
        command,
        check=check,
        stdin=subprocess.DEVNULL,
        close_fds=True,
        text=True,
        capture_output=True,
    )


def _emit(stage, **payload):
    payload["stage"] = stage
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)


def _persist(path, items):
    payload = {"items": [items[key] for key in sorted(items)]}
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    temporary.replace(path)
    os.chmod(path, 0o600)


def _source_account_id(row):
    if str(row["delivery_mode"] or "") == "premium_relay_repost":
        return int(row["relay_account_id"] or 0)
    return int(row["account_id"])


def _prepare(args, conn):
    from features.x_posts.service import download_media, probe_media
    from scripts.x_post_daily_runner import _preflight_candidate
    from scripts.x_post_schedule_runner import (
        ScheduleConfig,
        _repair_client,
        _retrying_media_downloader,
    )

    config = replace(ScheduleConfig.from_env(), repair_timeout=3600)
    config.validate()
    repair = _repair_client(config)
    if repair is None:
        raise RuntimeError("repair client unavailable")
    placeholders = ",".join("?" for _item in args.run_id)
    rows = conn.execute(
        "SELECT q.*,l.error_code AS failed_error_code FROM x_post_queue q "
        "JOIN x_post_publish_log l ON l.queue_id=q.id "
        "WHERE q.schedule_run_id IN (%s) AND q.status='failed' "
        "AND l.status='failed' ORDER BY q.id" % placeholders,
        tuple(args.run_id),
    ).fetchall()
    image_entry = json.loads(args.image_entry.read_text(encoding="utf-8"))
    image_queue_id = int(image_entry["queue_id"])
    video_rows = [row for row in rows if int(row["id"]) != image_queue_id]
    if len(rows) != args.expected_count or len(video_rows) + 1 != len(rows):
        raise RuntimeError("exact recovery scope drifted")
    if args.checkpoint.exists():
        saved = json.loads(args.checkpoint.read_text(encoding="utf-8"))
        prepared = {int(item["queue_id"]): item for item in saved.get("items", [])}
    else:
        prepared = {}
    source_ids = []
    for row in video_rows:
        account_id = _source_account_id(row)
        if account_id not in source_ids:
            source_ids.append(account_id)
    accounts = {}
    for account_id in source_ids:
        row = conn.execute(
            "SELECT id,x_user_id,username,display_name,status,publish_approved,"
            "subscription_type,drama_language FROM x_authorized_account WHERE id=?",
            (account_id,),
        ).fetchone()
        if not row or row["status"] != "active" or int(row["publish_approved"] or 0) != 1:
            raise RuntimeError("source account is not publishable: %s" % account_id)
        subscription = str(row["subscription_type"] or "unknown").lower()
        accounts[account_id] = {
            "id": account_id,
            "x_user_id": str(row["x_user_id"]),
            "username": str(row["username"]),
            "display_name": str(row["display_name"] or row["username"]),
            "subscription_type": subscription,
            "premium_subscriber": subscription == "premium",
            "long_video_eligible": subscription == "premium",
            "drama_language": str(row["drama_language"] or "en"),
        }
    _emit("prepare_start", existing=len(prepared), total=len(video_rows))
    downloader = _retrying_media_downloader(download_media)
    with tempfile.TemporaryDirectory(
        prefix="failed-media-background-", dir=config.work_dir
    ) as temporary:
        for position, row in enumerate(video_rows, 1):
            queue_id = int(row["id"])
            if queue_id in prepared:
                continue
            queue = dict(row)
            account_id = _source_account_id(row)
            candidate = dict(queue)
            candidate["media_kind"] = "video"
            item = _preflight_candidate(
                config,
                candidate,
                accounts[account_id],
                int(queue["candidate_rank"] or position),
                int(time.time()),
                Path(temporary) / ("queue-%s.media" % queue_id),
                downloader,
                probe_media,
                repair_client=repair,
                repair_state={"attempted": 0},
            )
            if (
                item.get("media_repair_trigger_code") != queue["failed_error_code"]
                or item.get("original_material_url") != queue["material_url"]
            ):
                raise RuntimeError("repaired evidence mismatch: %s" % queue_id)
            prepared[queue_id] = {
                "queue_id": queue_id,
                "material_url": item["material_url"],
                "media_repair_trigger_code": item["media_repair_trigger_code"],
                "media_repair_job_key": item["media_repair_job_key"],
                "media_repair_profile": item["media_repair_profile"],
                "media_repair_source_sha256": item["media_repair_source_sha256"],
                "preflight_sha256": item["preflight_sha256"],
                "preflight_size": int(item["preflight_size"]),
                "preflight_duration": float(item["preflight_duration"]),
            }
            _persist(args.checkpoint, prepared)
            _emit("video_checkpointed", queue_id=queue_id, completed=len(prepared), total=len(video_rows))
    if len(prepared) != len(video_rows):
        raise RuntimeError("video preparation incomplete")
    prepared[image_queue_id] = image_entry
    queue_run = {int(row["id"]): int(row["schedule_run_id"]) for row in rows}
    manifest = {
        "runs": [
            {
                "run_id": run_id,
                "queues": [
                    prepared[queue_id]
                    for queue_id in sorted(prepared)
                    if queue_run[queue_id] == run_id
                ],
            }
            for run_id in args.run_id
        ]
    }
    args.manifest.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    os.chmod(args.manifest, 0o600)
    _emit("manifest_ready", queue_count=len(prepared), run_count=len(args.run_id))
    return manifest


def _wait_oneshots(units, timeout=180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        active = [unit for unit in units if _run(["systemctl", "is-active", "--quiet", unit], check=False).returncode == 0]
        if not active:
            return
        time.sleep(1)
    raise RuntimeError("X oneshot services did not drain")


def _recover(args, manifest):
    from features.x_posts.service import (
        FAILED_MEDIA_PREFLIGHT_RECOVERY_REASON,
        XPostStore,
    )

    timers = (
        "x-post-schedule.timer",
        "x-post-schedule-claim.timer",
        "x-post-manual.timer",
        "x-auto-post-runner.timer",
        "x-auto-post-scheduler.timer",
    )
    oneshots = (
        "x-post-schedule.service",
        "x-post-schedule-claim.service",
        "x-post-manual.service",
        "x-auto-post-runner.service",
        "x-auto-post-scheduler.service",
    )
    for timer in timers:
        _run(["systemctl", "stop", timer])
    try:
        _wait_oneshots(oneshots)
        backup = args.output_dir / (
            "accounts-before-apply-%s.sqlite3" % datetime.now().strftime("%Y%m%dT%H%M%S")
        )
        source = sqlite3.connect(args.db_path)
        target = sqlite3.connect(backup)
        source.backup(target)
        target.close()
        source.close()
        os.chmod(backup, 0o600)
        store = XPostStore(args.db_path)
        for run in manifest["runs"]:
            store.recover_failed_material_schedule_queues(
                run["run_id"],
                run["queues"],
                reason=FAILED_MEDIA_PREFLIGHT_RECOVERY_REASON,
                actor=args.actor,
                deployed_commit=args.deployed_commit,
                validate_only=True,
            )
        _emit("recovery_validated", run_count=len(manifest["runs"]), backup=str(backup))
        for run in manifest["runs"]:
            store.recover_failed_material_schedule_queues(
                run["run_id"],
                run["queues"],
                reason=FAILED_MEDIA_PREFLIGHT_RECOVERY_REASON,
                actor=args.actor,
                deployed_commit=args.deployed_commit,
            )
        _emit("recovery_applied", queue_count=args.expected_count)
        _run(["systemctl", "start", "--no-block", "x-post-schedule.service"])
        _emit("publish_dispatched", service="x-post-schedule.service")
    finally:
        for timer in timers:
            _run(["systemctl", "start", timer], check=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--db-path", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--image-entry", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--run-id", action="append", type=int, required=True)
    parser.add_argument("--expected-count", type=int, required=True)
    parser.add_argument("--deployed-commit", required=True)
    parser.add_argument("--actor", default="codex_operator")
    parser.add_argument("--env-file", action="append", type=Path, required=True)
    args = parser.parse_args(argv)
    sys.path.insert(0, str(args.repo_root.resolve()))
    args.output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    for path in args.env_file:
        _load_env_file(path)
    lock_path = args.output_dir / "background-job.lock"
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        conn = sqlite3.connect(args.db_path)
        conn.row_factory = sqlite3.Row
        try:
            manifest = _prepare(args, conn)
        finally:
            conn.close()
        _recover(args, manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
