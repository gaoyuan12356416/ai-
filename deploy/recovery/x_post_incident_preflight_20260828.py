#!/usr/bin/env python3
"""Checkpoint CPU media proofs for the exact 2026-08-28 X drama incident.

The CLI can only prepare runs 348/350 against ARTIFACT_DIR/rehearsal.sqlite3.
It calls the existing recovery orchestrator with apply=False and its normal
process lock and transaction guards. It never publishes or reconciles X writes.

For a later independently authorized apply, import IncidentPreflight with
reuse_only=True, the pinned index SHA, and an explicit db_path. The caller must
hold process_lock for the entire phase and retain execute_recovery's guards.
Missing or conflicting checkpoints never fall back to network repair in that
mode. Checkpoints contain private URLs; stdout contains only safe progress.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
import re
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from deploy.recovery.x_post_verified_drama_report import (  # noqa: E402
    EXPECTED_QUEUES,
    INCIDENT_PROFILE,
    OUTPUT_PREFIX,
    prepare_from_gpu_manifest,
)
from features.x_posts.service import (  # noqa: E402
    DEFAULT_MAX_MEDIA_BYTES,
    PREMIUM_MAX_DURATION_SECONDS,
    XPostStore,
)
from scripts.x_post_bound_drama_media_recovery import execute_recovery  # noqa: E402
from scripts.x_post_daily_runner import (  # noqa: E402
    _plan_candidate,
    _preflight_candidate,
    _repair_job_key,
    _validate_repair_probe,
    process_lock,
)
from scripts.x_post_drama_media_repair_backfill import load_drama_environment_files  # noqa: E402
from scripts.x_post_media_repair_backfill import BackfillError, configured_environment  # noqa: E402
from scripts.x_post_schedule_runner import ScheduleConfig  # noqa: E402


INCIDENT = "x-post-pool-blockers-20260828"
FROZEN_SHA256 = "a5343590f87b5f0890e2e5e3ade68404c41b976e520926949f9652914468491f"
GPU_BUNDLE_SHA256 = "78e5830d4d6303b906d5402ee87285e54031846d1e5200e7198221d8f4fca08e"
GPU_HOST = "43.154.250.89"
GPU_RELEASE = "/data/x-post-media-repair/releases/fba8ff603e979b443339108cb2ce45c975fbd39f"
GPU_MANIFEST_ROOT = "/data/x-post-media-repair/state/manifests/"
CHECKPOINT_TTL_SECONDS = 4 * 60 * 60
MAX_JSON_BYTES = 2 * 1024 * 1024
HEX_64 = re.compile(r"[a-f0-9]{64}\Z")
HEX_40 = re.compile(r"[a-f0-9]{40}\Z")
ALL_QUEUES = frozenset(queue for values in EXPECTED_QUEUES.values() for queue in values)
PROOF_FIELDS = {
    "material_url", "original_material_url", "media_repair_trigger_code",
    "media_repair_job_key", "media_repair_profile", "media_repair_source_sha256",
    "preflight_sha256", "preflight_size", "preflight_duration",
    "preflight_width", "preflight_height",
}
TABLES = {
    "queues": "x_post_queue", "logs": "x_post_publish_log",
    "relays": "x_post_repost_ledger", "pools": "x_post_drama_pool",
    "runs": "x_post_schedule_run",
}


def _require(condition, code="x_post_incident_evidence_invalid"):
    if not condition:
        raise BackfillError("事故预检证据不完整、已变化或不在授权范围，未继续操作", code=code)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _encoded(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _now():
    return datetime.now(timezone.utc)


def _iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _time(value):
    _require(isinstance(value, str) and value.endswith("Z"))
    try:
        result = datetime.fromisoformat(value[:-1] + "+00:00")
    except (TypeError, ValueError):
        _require(False)
    return result


def _require_root():
    _require(sys.platform.startswith("linux") and os.geteuid() == 0,
             "x_post_incident_private_root_required")


def _private_owner(info, mode):
    _require(info.st_uid == 0 and stat.S_IMODE(info.st_mode) == mode,
             "x_post_incident_private_permissions_invalid")


def _trusted_ancestor(info):
    _require(info.st_uid == 0 and (not stat.S_IMODE(info.st_mode) & 0o022
                                   or info.st_mode & stat.S_ISVTX),
             "x_post_incident_private_permissions_invalid")


def _directory(path, *, create=False):
    path = Path(path)
    _require(path.is_absolute(), "x_post_incident_private_path_invalid")
    if create and not path.exists():
        path.mkdir(mode=0o700)
    _require(path.resolve(strict=True) == path and not path.is_symlink(),
             "x_post_incident_private_path_invalid")
    for parent in (path,) + tuple(path.parents):
        ancestor = parent.lstat()
        _require(not stat.S_ISLNK(ancestor.st_mode), "x_post_incident_private_path_invalid")
        _trusted_ancestor(ancestor)
    info = path.lstat()
    _require(stat.S_ISDIR(info.st_mode), "x_post_incident_private_path_invalid")
    _private_owner(info, 0o700)
    return path


def _private_file(path):
    try:
        info = Path(path).lstat()
    except OSError:
        _require(False, "x_post_incident_private_file_unavailable")
    _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1,
             "x_post_incident_private_path_invalid")
    _private_owner(info, 0o600)
    return info


def _read_private(path, expected_sha=None):
    before = _private_file(path)
    descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as handle:
        opened = os.fstat(handle.fileno())
        _require((before.st_dev, before.st_ino) == (opened.st_dev, opened.st_ino))
        _private_owner(opened, 0o600)
        raw = handle.read(MAX_JSON_BYTES + 1)
        after = os.fstat(handle.fileno())
        _private_owner(after, 0o600)
        _require(opened.st_size == after.st_size and opened.st_mtime_ns == after.st_mtime_ns)
    _require(len(raw) <= MAX_JSON_BYTES)
    digest = _sha(raw)
    if expected_sha is not None:
        _require(digest == expected_sha, "x_post_incident_evidence_hash_mismatch")
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, ValueError):
        _require(False)
    _require(isinstance(document, dict))
    return document, digest


def _fsync_directory(path):
    if os.name == "nt":  # Local offline tests only; the CLI requires Linux root.
        return
    descriptor = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_private(path, document):
    path = Path(path)
    _directory(path.parent)
    if path.exists() or path.is_symlink():
        _private_file(path)
    raw = _encoded(document)
    _require(len(raw) <= MAX_JSON_BYTES)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".%s." % path.name, dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
        _private_file(path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
    return _sha(raw)


def _runtime_commit():
    if (REPO_ROOT / ".git").exists():
        result = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip()
    _require(HEX_40.fullmatch(REPO_ROOT.name), "x_post_incident_runtime_commit_invalid")
    return REPO_ROOT.name


def _file_fingerprint(path, maximum):
    path = Path(path)
    info = path.lstat()
    _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1)
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            size += len(chunk)
            _require(size <= maximum)
            digest.update(chunk)
    _require(size > 0)
    return digest.hexdigest(), size


class IncidentPreflight:
    """Callable preflight injection; no ledger mutation or X/OAuth operation."""

    def __init__(
        self, artifact_dir, run_id, *, deployed_commit, reuse_only=False,
        expected_index_sha256=None, db_path=None, progress=None,
    ):
        _require_root()
        _require(type(run_id) is int and run_id in EXPECTED_QUEUES)
        _require(type(reuse_only) is bool)
        _require(isinstance(deployed_commit, str) and HEX_40.fullmatch(deployed_commit))
        _require(_runtime_commit() == deployed_commit, "x_post_incident_runtime_commit_invalid")
        if reuse_only:
            _require(isinstance(expected_index_sha256, str) and HEX_64.fullmatch(expected_index_sha256),
                     "x_post_incident_checkpoint_index_required")
        if db_path is not None:
            _require(reuse_only, "x_post_incident_prepare_copy_required")
        self.artifact_dir = _directory(artifact_dir)
        self.checkpoint_dir = _directory(self.artifact_dir / "cpu-verified", create=not reuse_only)
        self.index_path = self.checkpoint_dir / "index.json"
        self.db_path = Path(db_path) if db_path is not None else self.artifact_dir / "rehearsal.sqlite3"
        _require(self.db_path.is_absolute() and self.db_path.is_file())
        _require(self.db_path.resolve(strict=True) == self.db_path and not self.db_path.is_symlink())
        _require(self.db_path.stat().st_nlink == 1, "x_post_incident_prepare_copy_required")
        if db_path is None:
            _private_file(self.db_path)
        self.run_id, self.deployed_commit = run_id, deployed_commit
        self.reuse_only, self.expected_index_sha256 = reuse_only, expected_index_sha256
        self.progress = progress
        self.tool_sha256 = _sha(Path(__file__).read_bytes())
        self.host = socket.gethostname()
        self.items, self.checkpoints = {}, {}
        self.counts = {"checkpoint_reused_count": 0, "gpu_manifest_verified_count": 0,
                       "normal_preflight_verified_count": 0}
        self.repair_state = {}
        self.frozen_document, _ = _read_private(self.artifact_dir / "frozen-inputs.json", FROZEN_SHA256)
        self.gpu_bundle, _ = _read_private(self.artifact_dir / "gpu-ready-manifests.json", GPU_BUNDLE_SHA256)
        self.frozen = self.frozen_document.get("frozen")
        _require(isinstance(self.frozen, dict) and set(self.frozen) == set(TABLES) | {"protected"})
        self.frozen_queues = {row["id"]: row for row in self.frozen["queues"]}
        _require(set(self.frozen_queues) == ALL_QUEUES and len(self.frozen["queues"]) == len(ALL_QUEUES))
        self.logs = {row["queue_id"]: row for row in self.frozen["logs"]}
        _require(set(self.logs) == ALL_QUEUES and len(self.logs) == len(self.frozen["logs"]))
        self.manifest = {"run_id": run_id, "queues": []}
        for queue_id in EXPECTED_QUEUES[run_id]:
            queued, log = self.frozen_queues[queue_id], self.logs[queue_id]
            _require(queued["schedule_run_id"] == run_id and queued["status"] == "failed")
            _require(queued["source_type"] == "drama" and queued["media_validation_mode"] == "deferred")
            _require(log["status"] == "failed" and log["attempt_count"] == 0 and log["unknown_outcome"] == 0)
            _require(log["error_code"] == "invalid_media_dimensions")
            _require(not any(log[name] for name in ("x_media_id", "x_post_id", "x_post_url", "started_at", "published_at")))
            self.manifest["queues"].append({
                "queue_id": queue_id, "pool_item_id": queued["drama_pool_item_id"],
                "content_id": queued["content_id"], "episode_number": queued["episode_number"],
                "expected_error_code": log["error_code"],
            })
        self.expected = {item["queue_id"]: item for item in self.manifest["queues"]}
        self.gpu_records = self._gpu_records()

    def _gpu_records(self):
        bundle = self.gpu_bundle
        _require(bundle.get("origin_host") == GPU_HOST and bundle.get("origin_release") == GPU_RELEASE)
        captured = _time(bundle.get("captured_at"))
        _require(captured >= _time(self.frozen_document.get("captured_at")))
        records, matched = bundle.get("records"), {}
        _require(isinstance(records, list) and len(records) == 7)
        for record in records:
            _require(isinstance(record, dict) and set(record) == {"path", "sha256", "uid", "mode", "manifest"})
            _require(type(record["uid"]) is int and record["uid"] == 0 and record["mode"] == "0o600")
            manifest = record["manifest"]
            _require(isinstance(manifest, dict) and manifest.get("status") == "ready")
            _require(isinstance(record["sha256"], str) and HEX_64.fullmatch(record["sha256"]))
            _require(_sha(_encoded(manifest)) == record["sha256"])
            request = manifest.get("request")
            _require(isinstance(request, dict))
            _require(record["path"] == GPU_MANIFEST_ROOT + str(request.get("job_key")) + ".json")
            _require(_time(manifest.get("completed_at")) <= captured)
            matches = [queue_id for queue_id, queued in self.frozen_queues.items() if (
                request.get("material_id") == queued["material_id"]
                and request.get("source_url") == queued["material_url"]
                and request.get("pool_item_id") == str(queued["drama_pool_item_id"])
            )]
            _require(len(matches) == 1 and matches[0] not in matched)
            matched[matches[0]] = record
        _require(set(matched) == set(range(635, 642)))
        return matched

    def assert_database(self):
        """Compare complete selected rows and all protected outcomes, read-only."""
        queue_ids = set(EXPECTED_QUEUES[self.run_id])
        pool_ids = {self.frozen_queues[key]["drama_pool_item_id"] for key in queue_ids}
        selected = {
            "queues": [row for row in self.frozen["queues"] if row["id"] in queue_ids],
            "logs": [row for row in self.frozen["logs"] if row["queue_id"] in queue_ids],
            "relays": [row for row in self.frozen["relays"] if row["queue_id"] in queue_ids],
            "pools": [row for row in self.frozen["pools"] if row["id"] in pool_ids],
            "runs": [row for row in self.frozen["runs"] if row["id"] == self.run_id],
        }
        _require(len(selected["runs"]) == 1)
        protected = self.frozen["protected"]
        _require(isinstance(protected, dict) and set(protected) == {"queues", "logs", "relays"})
        _require({row["id"] for row in protected["queues"]} == {533, 719, 726})
        with contextlib.closing(sqlite3.connect(self.db_path.as_uri() + "?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            for group in (selected, protected):
                for key, rows in group.items():
                    if not rows:
                        continue
                    placeholders = ",".join("?" for _ in rows)
                    actual = conn.execute("SELECT * FROM %s WHERE id IN (%s) ORDER BY id" % (TABLES[key], placeholders),
                                          tuple(row["id"] for row in rows)).fetchall()
                    _require([dict(row) for row in actual] == sorted(rows, key=lambda row: row["id"]),
                             "x_post_incident_frozen_ledger_changed")

    def _index(self):
        if not self.index_path.exists() and not self.index_path.is_symlink():
            _require(not self.reuse_only, "x_post_incident_checkpoint_missing")
            return {"version": 1, "incident": INCIDENT, "frozen_sha256": FROZEN_SHA256,
                    "gpu_bundle_sha256": GPU_BUNDLE_SHA256, "tool_commit": self.deployed_commit,
                    "deployed_commit": self.deployed_commit, "checkpoints": {}}, None
        index, digest = _read_private(self.index_path, self.expected_index_sha256)
        _require(set(index) == {"version", "incident", "frozen_sha256", "gpu_bundle_sha256",
                               "tool_commit", "deployed_commit", "checkpoints"})
        _require(type(index["version"]) is int and index["version"] == 1 and index["incident"] == INCIDENT)
        _require(index["frozen_sha256"] == FROZEN_SHA256 and index["gpu_bundle_sha256"] == GPU_BUNDLE_SHA256)
        _require(index["tool_commit"] == index["deployed_commit"] == self.deployed_commit)
        _require(isinstance(index["checkpoints"], dict))
        _require(set(index["checkpoints"]).issubset({str(value) for value in ALL_QUEUES}))
        return index, digest

    def _validate_item(self, config, candidate, account, rank, timestamp, item):
        _require(isinstance(item, dict))
        planned = _plan_candidate(account, candidate, rank, timestamp)
        _require(set(item) == set(planned) | PROOF_FIELDS)
        _require(all(item[key] == value for key, value in planned.items() if key not in PROOF_FIELDS))
        _require(item["original_material_url"] == candidate["material_url"])
        _require(item["media_repair_trigger_code"] == "invalid_media_dimensions")
        _require(item["media_repair_profile"] == config.repair_profile == INCIDENT_PROFILE)
        for key in ("media_repair_source_sha256", "media_repair_job_key", "preflight_sha256"):
            _require(isinstance(item[key], str) and HEX_64.fullmatch(item[key]))
        _require(item["media_repair_job_key"] == _repair_job_key(candidate, item["media_repair_source_sha256"], INCIDENT_PROFILE, "premium"))
        _require(type(item["preflight_size"]) is int and 0 < item["preflight_size"] <= config.max_media_bytes <= DEFAULT_MAX_MEDIA_BYTES)
        duration = item["preflight_duration"]
        _require(type(duration) in (int, float) and math.isfinite(duration) and 0 < duration <= PREMIUM_MAX_DURATION_SECONDS)
        _require(all(type(item[key]) is int and item[key] > 0 for key in ("preflight_width", "preflight_height")))
        _require(item["material_url"] == OUTPUT_PREFIX + "drama-resource-%s/source-%s/output-%s.mp4" % (
            candidate["material_id"], item["media_repair_source_sha256"], item["preflight_sha256"],
        ))

    def _validate_cpu(self, evidence, item, verified_at, method):
        _require(isinstance(evidence, dict) and set(evidence) == {
            "host", "uid", "verified_at", "output_download", "output_probe", "source_download",
        })
        _require(evidence["host"] == self.host and type(evidence["uid"]) is int and evidence["uid"] == 0)
        _require(evidence["verified_at"] == verified_at)
        download, probe = evidence["output_download"], evidence["output_probe"]
        _require(isinstance(download, dict) and set(download) == {"url", "sha256", "size"})
        _require(download == {"url": item["material_url"], "sha256": item["preflight_sha256"], "size": item["preflight_size"]})
        normalized = _validate_repair_probe(probe, item["preflight_size"], max_duration_seconds=PREMIUM_MAX_DURATION_SECONDS)
        _require(probe == normalized)
        _require(probe["duration"] == item["preflight_duration"] and probe["width"] == item["preflight_width"] and probe["height"] == item["preflight_height"])
        source = evidence["source_download"]
        if method == "gpu_manifest":
            _require(source is None)
        else:
            _require(isinstance(source, dict) and set(source) == {"url", "sha256", "size"})
            _require(source["url"] == item["original_material_url"] and source["sha256"] == item["media_repair_source_sha256"])
            _require(type(source["size"]) is int and 0 < source["size"] <= DEFAULT_MAX_MEDIA_BYTES)

    def _cached(self, index, queue_id, candidate, config, account, rank, timestamp):
        entry = index["checkpoints"].get(str(queue_id))
        path = self.checkpoint_dir / ("queue-%s.json" % queue_id)
        if entry is None:
            _require(not path.exists() and not path.is_symlink(), "x_post_incident_checkpoint_unindexed")
            _require(not self.reuse_only, "x_post_incident_checkpoint_missing")
            return None
        _require(isinstance(entry, dict) and set(entry) == {"file", "sha256", "verified_at"})
        _require(entry["file"] == path.name and isinstance(entry["sha256"], str) and HEX_64.fullmatch(entry["sha256"]))
        checkpoint, _ = _read_private(path, entry["sha256"])
        _require(set(checkpoint) == {"version", "incident", "status", "run_id", "queue_id", "expected",
            "frozen_sha256", "frozen_queue_sha256", "gpu_bundle_sha256", "gpu_manifest_sha256",
            "tool_commit", "deployed_commit", "tool_sha256", "verified_at", "expires_at", "method",
            "source_candidate_sha256", "source_candidate", "item_sha256", "item", "cpu_verification",
            "x_write_attempted", "unknown_outcome"})
        _require(type(checkpoint["version"]) is int and checkpoint["version"] == 1)
        _require(checkpoint["incident"] == INCIDENT and checkpoint["status"] == "cpu_verified")
        _require(checkpoint["x_write_attempted"] is False and checkpoint["unknown_outcome"] is False)
        _require(checkpoint["run_id"] == self.run_id and checkpoint["queue_id"] == queue_id)
        _require(checkpoint["expected"] == self.expected[queue_id])
        _require(checkpoint["frozen_sha256"] == FROZEN_SHA256 and checkpoint["gpu_bundle_sha256"] == GPU_BUNDLE_SHA256)
        _require(checkpoint["frozen_queue_sha256"] == _sha(_encoded(self.frozen_queues[queue_id])))
        _require(checkpoint["tool_commit"] == checkpoint["deployed_commit"] == self.deployed_commit)
        _require(checkpoint["tool_sha256"] == self.tool_sha256)
        verified, expires, current = _time(checkpoint["verified_at"]), _time(checkpoint["expires_at"]), _now()
        _require(entry["verified_at"] == checkpoint["verified_at"])
        _require(expires == verified + timedelta(seconds=CHECKPOINT_TTL_SECONDS) and verified <= current <= expires,
                 "x_post_incident_checkpoint_expired")
        _require(verified.astimezone(timezone(timedelta(hours=8))).date() == current.astimezone(timezone(timedelta(hours=8))).date(),
                 "x_post_incident_checkpoint_expired")
        _require(checkpoint["source_candidate"] == candidate and checkpoint["source_candidate_sha256"] == _sha(_encoded(candidate)),
                 "x_post_incident_source_changed")
        record = self.gpu_records.get(queue_id)
        _require(checkpoint["method"] == ("gpu_manifest" if record else "normal_preflight"))
        _require(checkpoint["gpu_manifest_sha256"] == (record["sha256"] if record else ""))
        item = checkpoint["item"]
        _require(checkpoint["item_sha256"] == _sha(_encoded(item)))
        self._validate_item(config, candidate, account, rank, timestamp, item)
        self._validate_cpu(checkpoint["cpu_verification"], item, checkpoint["verified_at"], checkpoint["method"])
        return checkpoint

    def __call__(self, config, candidate, account, rank, timestamp, destination, downloader, prober,
                 *, repair_client=None, repair_state=None):
        destination = Path(destination)
        _require(destination.suffix == ".mp4" and destination.stem in {str(value) for value in self.expected})
        queue_id = int(destination.stem)
        _require(rank == list(EXPECTED_QUEUES[self.run_id]).index(queue_id) + 1)
        expected, frozen = self.expected[queue_id], self.frozen_queues[queue_id]
        _require(candidate.get("source_type") == "drama" and candidate.get("content_id") == frozen["content_id"])
        _require(candidate.get("episode_number") == frozen["episode_number"])
        _require(candidate.get("pool_item_id") == candidate.get("drama_pool_item_id") == frozen["drama_pool_item_id"])
        _require(candidate.get("material_id") == frozen["material_id"] and candidate.get("material_url") == frozen["material_url"],
                 "x_post_incident_source_changed")
        self.repair_state = repair_state if isinstance(repair_state, dict) else {}
        self.assert_database()
        index, _ = self._index()
        checkpoint = self._cached(index, queue_id, candidate, config, account, rank, timestamp)
        if checkpoint is not None:
            self.counts["checkpoint_reused_count"] += 1
            return self._accepted(queue_id, checkpoint, "checkpoint_reused")
        downloads, probes, events = [], [], []

        def checked_download(url, path, *args, **kwargs):
            _require(Path(path) == destination)
            result = downloader(url, path, *args, **kwargs)
            digest, size = _file_fingerprint(path, config.max_media_bytes)
            _require(isinstance(result, dict) and result.get("sha256") == digest and result.get("size") == size)
            downloads.append({"url": url, "sha256": digest, "size": size})
            events.append("download")
            return result

        def checked_probe(path, *args, **kwargs):
            _require(Path(path) == destination)
            result = prober(path, *args, **kwargs)
            probes.append(result)
            events.append("probe")
            return result

        record = self.gpu_records.get(queue_id)
        method = "gpu_manifest" if record else "normal_preflight"
        try:
            if record:
                item = prepare_from_gpu_manifest(
                    config, candidate, account, rank, timestamp, destination, checked_download, checked_probe,
                    expected=expected, frozen_queue=frozen, manifest=record["manifest"],
                )
            else:
                item = _preflight_candidate(
                    config, candidate, account, rank, timestamp, destination, checked_download, checked_probe,
                    repair_client=repair_client, repair_state=repair_state,
                )
            self._validate_item(config, candidate, account, rank, timestamp, item)
            _require(events[-2:] == ["download", "probe"] and downloads and probes)
            completed = _now()
            verified_at = _iso(completed)
            evidence = {
                "host": self.host, "uid": 0, "verified_at": verified_at,
                "output_download": downloads[-1],
                "output_probe": _validate_repair_probe(probes[-1], item["preflight_size"], max_duration_seconds=PREMIUM_MAX_DURATION_SECONDS),
                "source_download": None if record else downloads[0],
            }
            self._validate_cpu(evidence, item, verified_at, method)
            checkpoint = {
                "version": 1, "incident": INCIDENT, "status": "cpu_verified", "run_id": self.run_id,
                "queue_id": queue_id, "expected": expected, "frozen_sha256": FROZEN_SHA256,
                "frozen_queue_sha256": _sha(_encoded(frozen)), "gpu_bundle_sha256": GPU_BUNDLE_SHA256,
                "gpu_manifest_sha256": record["sha256"] if record else "",
                "tool_commit": self.deployed_commit, "deployed_commit": self.deployed_commit,
                "tool_sha256": self.tool_sha256, "verified_at": verified_at,
                "expires_at": _iso(completed + timedelta(seconds=CHECKPOINT_TTL_SECONDS)),
                "method": method, "source_candidate_sha256": _sha(_encoded(candidate)),
                "source_candidate": candidate, "item_sha256": _sha(_encoded(item)), "item": item,
                "cpu_verification": evidence, "x_write_attempted": False, "unknown_outcome": False,
            }
            path = self.checkpoint_dir / ("queue-%s.json" % queue_id)
            digest = _write_private(path, checkpoint)
            index["checkpoints"][str(queue_id)] = {"file": path.name, "sha256": digest, "verified_at": verified_at}
            _write_private(self.index_path, index)
        except Exception:
            if self.progress:
                self.progress({"queue_id": queue_id, "status": "failed", "size": 0, "duration": 0.0})
            raise
        self.counts["gpu_manifest_verified_count" if record else "normal_preflight_verified_count"] += 1
        return self._accepted(queue_id, checkpoint, "cpu_verified")

    def _accepted(self, queue_id, checkpoint, status):
        item = checkpoint["item"]
        self.items[queue_id], self.checkpoints[queue_id] = item, checkpoint
        if self.progress:
            self.progress({"queue_id": queue_id, "status": status, "size": item["preflight_size"], "duration": item["preflight_duration"]})
        return dict(item)

    def report(self, result=None, *, error_code=""):
        index, digest = self._index()
        return {
            **(result or {}), "status": (result or {}).get("status", "failed"),
            "incident": INCIDENT, "run_id": self.run_id, "requested_count": len(self.expected),
            "prepared_count": len(self.items), "error_code": error_code,
            "tool_commit": self.deployed_commit, "deployed_commit": self.deployed_commit,
            "frozen_sha256": FROZEN_SHA256, "gpu_bundle_sha256": GPU_BUNDLE_SHA256,
            "checkpoint_index_sha256": digest, "checkpoint_index": index,
            "repair_attempted_count": int(self.repair_state.get("attempted", 0)),
            **self.counts, "x_write_attempted": False,
            "prepared": [{**self.expected[key], "item": value} for key, value in self.items.items()],
        }


def prepare(config, artifact_dir, run_id, *, deployed_commit, progress=None):
    """Only the private rehearsal copy may be passed by this CLI entry point."""
    preflight = IncidentPreflight(artifact_dir, run_id, deployed_commit=deployed_commit, progress=progress)

    @contextlib.contextmanager
    def guarded_lock(path):
        with process_lock(path) as acquired:
            if acquired is not None:
                preflight.assert_database()
            yield acquired

    report_path = preflight.checkpoint_dir / ("prepare-%s-report.json" % run_id)
    try:
        result = execute_recovery(
            config, preflight.db_path, preflight.manifest, deployed_commit=deployed_commit,
            apply=False, store=XPostStore(preflight.db_path), preflight_candidate=preflight,
            lock_factory=guarded_lock,
        )
        preflight.assert_database()
        report = preflight.report(result)
    except Exception as exc:
        code = str(getattr(exc, "code", "x_post_incident_prepare_failed"))
        if not re.fullmatch(r"[a-z0-9_]{1,64}", code):
            code = "x_post_incident_prepare_failed"
        report = preflight.report(error_code=code)
    _write_private(report_path, report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", required=True)
    parser.add_argument("--run-id", type=int, choices=tuple(EXPECTED_QUEUES), required=True)
    parser.add_argument("--phase", choices=("prepare",), required=True)
    parser.add_argument("--deployed-commit", required=True)
    args = parser.parse_args(argv)

    def progress(item):
        print(json.dumps(item, ensure_ascii=True, sort_keys=True), flush=True)

    try:
        values = load_drama_environment_files()
        with configured_environment(values):
            report = prepare(ScheduleConfig.from_env(), args.artifact_dir, args.run_id,
                             deployed_commit=args.deployed_commit, progress=progress)
        summary = {key: report[key] for key in (
            "status", "run_id", "requested_count", "prepared_count", "error_code",
            "repair_attempted_count", "checkpoint_reused_count", "gpu_manifest_verified_count",
            "normal_preflight_verified_count", "checkpoint_index_sha256", "x_write_attempted",
        )}
        print(json.dumps(summary, sort_keys=True), file=sys.stderr, flush=True)
        return 0 if report["status"] == "validated" else 2
    except Exception as exc:
        code = str(getattr(exc, "code", "x_post_incident_prepare_failed"))
        if not re.fullmatch(r"[a-z0-9_]{1,64}", code):
            code = "x_post_incident_prepare_failed"
        print(json.dumps({"status": "failed", "run_id": args.run_id, "error_code": code,
                          "x_write_attempted": False}), file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
