"""A native selection of publishable media is separate from a renderer receipt."""

from __future__ import annotations

import json
import hashlib
import math
import os
import re
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import requests

from .adapters import AdapterError, obj
from .media_decision import context_from_row
from .native_common import dump


SCHEMA = """CREATE TABLE IF NOT EXISTS pipeline_native_media_decision(
    source_key TEXT PRIMARY KEY, decision_id TEXT NOT NULL, kind TEXT NOT NULL,
    source_identity TEXT NOT NULL, producer_source_key TEXT NOT NULL, artifact_json TEXT NOT NULL,
    publish_at TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '')"""
KINDS = {"cross_platform_reuse", "deadline_reuse", "deadline_original"}
TERMINAL = {"published", "failed", "cancelled", "canceled", "skipped", "no_candidate"}


def existing(conn, source_key):
    conn.execute(SCHEMA)
    record = conn.execute(
        "SELECT * FROM pipeline_native_media_decision WHERE source_key=?", (source_key,)
    ).fetchone()
    return dict(record) if record else {}


def read_only_snapshot(conn, bridge, job):
    """Read the original owner row, schedule and frozen selection in one transaction."""
    key = (job["channel"], job["family"])
    table, field = {
        ("fb", "auto"): ("fb_auto_task", "id"),
        ("fb", "video"): ("fb_video_task", "task_id"),
        ("tt", "auto"): ("tt_auto_task", "id"),
        ("x", "queue"): ("x_post_queue", "id"),
    }[key]
    data = conn.execute(
        f"SELECT * FROM {table} WHERE {field}=?", (job["source_id"],)
    ).fetchone()
    if not data:
        return {}, "", ""
    row = dict(data)
    if key == ("fb", "auto"):
        return row, row["source_media_url"], row["planned_publish_at_utc"]
    if key == ("fb", "video"):
        from .media_decision import fb_video_appointment

        batch = conn.execute(
            "SELECT * FROM fb_video_batch WHERE batch_id=?", (row["batch_id"],)
        ).fetchone()
        return (
            row,
            obj(row["snapshot_json"]).get("material", {}).get("media_url", ""),
            fb_video_appointment(row, dict(batch) if batch else {}),
        )
    if key == ("tt", "auto"):
        material = obj(row.get("selection_json")).get("material") or {}
        source = (
            row["source_media_url"]
            or material.get("source_media_url")
            or material.get("media_url")
            or ""
        )
        row["source_media_url"] = source
        if row.get("material_id"):
            route = bridge.service.executor._video_route_for_task(
                bridge.store.get_task(int(job["source_id"]))
            )
            row["preparation_profile"] = route.media_profile_version
            row["source_trim_tail_seconds"] = route.source_trim_tail_seconds
        return row, source, row["scheduled_at_utc"]
    # X automatic child appointments may live in a separate planner DB.
    # The coordinator rereads that planner before applying its frozen appointment.
    due = ""
    for table, field in (
        ("x_post_manual_run", "manual_run_id"),
        ("x_post_schedule_run", "schedule_run_id"),
    ):
        if row.get(field):
            parent = conn.execute(
                f"SELECT * FROM {table} WHERE id=?", (row[field],)
            ).fetchone()
            parent = dict(parent) if parent else {}
            due = (
                parent.get("planned_publish_at_utc") or parent.get("scheduled_at") or ""
            )
            break
    return (
        row,
        row.get("original_material_url") or row["material_url"],
        job.get("observed_publish_at", due),
    )


def _in_flight(conn, job, row):
    if (
        row.get("unknown_outcome")
        or row.get("graph_post_id")
        or row.get("publish_id")
        or row.get("execution_lease")
    ):
        return True
    ch, family = job["channel"], job["family"]
    q = lambda sql, args: conn.execute(sql, args).fetchone() is not None
    if q(
        "SELECT 1 FROM pipeline_canonical_hold WHERE source_key=?", (job["source_key"],)
    ):
        return True
    for suffix in ("claim", "operation"):
        table = f"pipeline_{ch}_{suffix}"
        if q(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ) and q(
            f"SELECT 1 FROM {table} WHERE source_key=? LIMIT 1", (job["source_key"],)
        ):
            return True
    if ch == "fb":
        if family == "video":
            return (
                row["status"] != "queued"
                or row.get("prepare_state") != "planned"
                or bool(row.get("prepare_lease"))
                or q(
                    "SELECT 1 FROM fb_video_attempt WHERE task_id=?",
                    (job["source_id"],),
                )
            )
        return (
            row["status"] != "planned"
            or bool(row.get("lease_owner") or row.get("attempt_count"))
            or q(
                "SELECT 1 FROM fb_auto_publish_attempt WHERE task_id=?",
                (job["source_id"],),
            )
        )
    if ch == "tt":
        return row["status"] not in {"pending", "reserved", "retry_wait"} or bool(
            row.get("claim_token")
            or row.get("publish_attempt_count")
            or row.get("preparation_attempt_count")
            or row.get("claim_phase") in {"publish", "reconcile"}
        )
    if row["status"] != "queued" or q(
        "SELECT 1 FROM x_post_publish_log WHERE queue_id=? AND (status<>'reserved' OR attempt_count>0 OR unknown_outcome<>0 OR COALESCE(x_post_id,'')<>'' OR COALESCE(x_media_id,'')<>'')",
        (job["source_id"],),
    ):
        return True
    return q(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='x_post_random_template_preparation'",
        (),
    ) and q(
        "SELECT 1 FROM x_post_random_template_preparation WHERE queue_id=?",
        (job["source_id"],),
    )


def _date(raw):
    return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone(
        timezone.utc
    )


def _scope_check(conn, job, row, due, context, decision):
    if not row or row.get("status") in TERMINAL:
        return {"skipped": True, "reason": row.get("status", "missing")}
    if _in_flight(conn, job, row):
        return {"already_started": True}
    if decision["source_identity"] != context["source_identity"]:
        return {"schedule_changed": True}
    target = job.get("media_target") or {}
    if any(str(row.get(k) or "") != str(v) for k, v in target.items()):
        return {"schedule_changed": True}
    if "observed_publish_at" in job and job["observed_publish_at"] != job.get(
        "publish_at", ""
    ):
        return {"schedule_changed": True}
    if (
        decision["kind"] == "cross_platform_reuse"
        and due
        and job.get("publish_at")
        and _date(due) <= datetime.now(timezone.utc) + timedelta(minutes=5)
    ):
        return {"schedule_changed": True}
    if decision["kind"].startswith("deadline_"):
        expected = job.get("publish_at")
        if (
            not expected
            or not due
            or _date(expected) != _date(due)
            or datetime.now(timezone.utc) < _date(due) - timedelta(minutes=5)
        ):
            return {"schedule_changed": True}
    return {}


def _checked_url(value):
    p = urlsplit(str(value or ""))
    if (
        p.scheme == "http"
        and p.hostname == "advertising-1306474899.cos.ap-hongkong.myqcloud.com"
        and p.port is None
        and not p.username
        and not p.password
        and not p.fragment
    ):
        return p._replace(scheme="https").geturl()
    if (
        p.scheme != "https"
        or not p.hostname
        or p.username
        or p.password
        or p.fragment
        or p.port not in (None, 443)
    ):
        raise InvalidMedia("media_url_invalid")
    return value


class InvalidMedia(Exception):
    pass


class NotVideo(Exception):
    pass


def _download_tt(url, path, size):
    from .original_media import MAX_BYTES

    if not 0 < size <= MAX_BYTES:
        raise InvalidMedia("tt_source_size_invalid")
    digest = hashlib.sha256()
    written = 0
    with requests.get(
        url, stream=True, allow_redirects=False, timeout=(5, 30)
    ) as response:
        response.raise_for_status()
        with path.open("wb") as output:
            for chunk in response.iter_content(chunk_size=1024**2):
                written += len(chunk)
                if written > size:
                    raise InvalidMedia("media_source_changed")
                output.write(chunk)
                digest.update(chunk)
    if written != size:
        raise OSError("incomplete_media_download")
    return {"size": written, "sha256": digest.hexdigest()}


def _headers(url):
    response = requests.head(_checked_url(url), allow_redirects=False, timeout=(4, 8))
    try:
        if response.status_code in {401, 403, 404, 410}:
            raise InvalidMedia("media_object_missing")
        if response.status_code not in {200, 206}:
            raise AdapterError("media_object_unavailable")
        headers = response.headers
        mime = headers.get("Content-Type", "").split(";")[0].lower()
        if mime.startswith("image/"):
            raise NotVideo()
        if mime not in {"video/mp4", "application/octet-stream", "binary/octet-stream"}:
            raise InvalidMedia("media_object_not_mp4")
        return int(headers.get("Content-Length") or 0), str(
            headers.get("x-cos-meta-sha256") or ""
        ).lower()
    finally:
        response.close()


def _artifact(bridge, job, row, source, decision):
    channel = job["channel"]
    original = decision["kind"] == "deadline_original"
    art = (
        dict(decision.get("artifact") or {})
        if not original
        else {"url": source, "mime": "video/mp4", "profile": "source"}
    )
    if not original and not (
        art.get("mime") == "video/mp4"
        and re.fullmatch("[a-f0-9]{64}", str(art.get("sha256") or ""))
        and int(art.get("size") or 0) > 0
        and math.isfinite(float(art.get("duration_seconds") or 0))
        and float(art.get("duration_seconds") or 0) > 0
    ):
        raise InvalidMedia("media_receipt_invalid")
    url = _checked_url(art.get("url"))
    mirror = False
    if channel == "tt":
        prefix = (
            os.environ.get("TT_POST_GPU_VERIFIED_URL_PREFIX")
            or os.environ.get("PIPELINE_TT_VERIFIED_ORIGIN")
            or os.environ.get("TT_POST_URL_PROPERTY_VERIFIED_ORIGIN")
        )
        if not prefix:
            raise AdapterError("tt_verified_origin_unavailable")
        mirror = urlsplit(url).netloc != urlsplit(prefix).netloc or not url.startswith(
            prefix.rstrip("/") + "/"
        )
    if channel == "x":
        from features.x_posts import service as posts

        if not posts._allowed_host(
            urlsplit(url).hostname, bridge.r.POST_MEDIA_ALLOWED_HOSTS
        ):
            raise InvalidMedia("x_media_host_not_allowed")
    try:
        size, sha = _headers(url)
    except NotVideo:
        if channel == "x" and original:
            raise
        raise InvalidMedia("media_object_not_mp4") from None
    if not original:
        if not size or size != art["size"] or (sha and sha != art["sha256"]):
            raise InvalidMedia("media_object_changed")
        if not mirror and (sha or art["sha256"] in urlsplit(url).path):
            return art
        with tempfile.TemporaryDirectory(
            dir=os.environ.get("PIPELINE_MEDIA_PREFLIGHT_TMP") or None
        ) as temp:
            path = Path(temp) / "prepared.mp4"
            received = _download_tt(url, path, size)
            if received["sha256"] != art["sha256"]:
                raise InvalidMedia("media_object_changed")
            if mirror:
                from .original_media import copy_to_verified_origin

                art["url"] = copy_to_verified_origin(path, art["sha256"], size, prefix)
        return art
    if channel == "fb":
        art.update(size=size, identity_mode="remote_url")
        return art
    if (
        channel == "x"
        and row.get("media_validation_mode") == "preflight"
        and not row.get("original_material_url")
        and row.get("preflight_sha256")
        and row.get("preflight_size") == size
        and not row.get("media_repair_job_key")
        and row.get("preflight_duration")
    ):
        if sha and sha != row["preflight_sha256"]:
            raise InvalidMedia("x_source_file_changed")
        art.update(
            size=size,
            sha256=row["preflight_sha256"],
            duration_seconds=row["preflight_duration"],
            width=row.get("preflight_width"),
            height=row.get("preflight_height"),
        )
        return art
    # Existing source validators require actual bytes when the native task has
    # never verified them. No renderer, media repair or platform write is called.
    with tempfile.TemporaryDirectory(
        dir=os.environ.get("PIPELINE_MEDIA_PREFLIGHT_TMP") or None
    ) as temp:
        path = Path(temp) / "source.mp4"
        if channel == "x":
            from features.x_posts import service as posts

            try:
                limit = bridge.r.POST_MAX_MEDIA_BYTES
                meta = posts.download_media(
                    url,
                    path,
                    bridge.r.POST_MEDIA_ALLOWED_HOSTS,
                    max_bytes=limit,
                    timeout=30,
                )
                info = posts.probe_media(
                    path,
                    max_bytes=limit,
                    max_duration_seconds=posts.PREMIUM_MAX_DURATION_SECONDS,
                )
                duration, width, height = (
                    info["duration"],
                    info["width"],
                    info["height"],
                )
            except posts.XPostError as exc:
                if int(getattr(exc, "status", 500)) < 500:
                    raise InvalidMedia(exc.code) from exc
                raise
        else:
            meta = _download_tt(url, path, size)
            probe = subprocess.run(
                [
                    os.environ.get("PIPELINE_FFPROBE_BIN", "ffprobe"),
                    "-v",
                    "error",
                    "-show_format",
                    "-show_streams",
                    "-of",
                    "json",
                    str(path),
                ],
                capture_output=True,
                timeout=45,
            )
            if probe.returncode:
                raise InvalidMedia("tt_source_probe_invalid")
            info = json.loads(probe.stdout)
            video = next(
                (s for s in info["streams"] if s.get("codec_type") == "video"), {}
            )
            duration = float(info["format"].get("duration") or 0)
            if (
                video.get("codec_name") not in {"h264", "hevc"}
                or not math.isfinite(duration)
                or duration <= 0
            ):
                raise InvalidMedia("tt_source_incompatible")
            width, height = video["width"], video["height"]
            if sha and sha != meta["sha256"]:
                raise InvalidMedia("media_source_changed")
            if mirror:
                from .original_media import copy_to_verified_origin

                art["url"] = copy_to_verified_origin(path, meta["sha256"], size, prefix)
    if sha and sha != meta["sha256"]:
        raise InvalidMedia("media_source_changed")
    art.update(
        size=meta["size"],
        sha256=meta["sha256"],
        duration_seconds=duration,
        width=width,
        height=height,
    )
    return art


def _connect(bridge, job):
    if job["channel"] == "fb":
        return (
            bridge.runtime.store
            if job["family"] == "auto"
            else bridge.runtime.manual_worker.store
        ).connect()
    return bridge.connect()


def _selected_tt(bridge, job, row):
    if (
        row.get("material_id")
        or row.get("status") not in {"pending", "retry_wait"}
        or row.get("claim_token")
    ):
        return None
    executor = bridge.service.executor
    if not executor.gates.is_open:
        return {"retry": True, "reason": "tt_live_gates_closed"}
    claim = bridge.store.claim_next_executable_task(
        worker_id="pipeline:media-selection",
        lease_seconds=180,
        prepare_ahead_seconds=getattr(executor, "prepare_ahead_seconds", 0),
        allowed_phases={"selection"},
        task_id=int(job["source_id"]),
    )
    if not claim:
        return {"retry": True, "reason": "tt_selection_not_claimable"}
    token = claim.reveal_claim_token()
    try:
        selected = executor._select(claim.task, token)
        # Existing selection ledger owns this material. A separate native
        # rendering claim may later take the reserved row if no reuse exists.
        bridge.store.transition_task(
            selected.id,
            "reserved",
            expected_statuses={"reserved"},
            claim_token=token,
            updates={
                "claim_worker": "",
                "claim_token": "",
                "lease_expires_at_utc": "",
                "claim_phase": "",
            },
            event_type="pipeline_media_selection_frozen",
        )
    except Exception as exc:
        current = bridge.store.get_task(claim.task.id)
        if current.material_id and current.status == "reserved":
            raise
        task = executor._retry_or_fail(current, "selection", exc, token)
        executor._update_run(task.run_id)
        return (
            {"skipped": True, "reason": task.status}
            if task.status in TERMINAL
            else {"retry": True, "reason": task.error_code}
        )
    return None


def _response(record):
    if record["status"] == "skipped":
        return {"skipped": True, "reason": record["reason"]}
    return {"applied": True, "artifact": obj(record["artifact_json"])}


def _write(conn, bridge, job, row, source, kind, art, reason):
    key = (job["channel"], job["family"])
    now = (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )
    task_id = job["source_id"]
    if key == ("fb", "auto"):
        store = bridge.runtime.store
        if reason:
            conn.execute(
                "UPDATE fb_auto_task SET status='skipped',skip_reason=?,error_code=?,completed_at_utc=? WHERE id=?",
                (reason, reason, now, task_id),
            )
        else:
            conn.execute(
                """UPDATE fb_auto_task SET status='ready',media_url=?,prepared_media_url=?,prepared_sha256=?,
               prepared_size_bytes=?,prepared_duration_seconds=?,prepared_profile=?,prepared_at_utc=?,media_selection_kind=?,
               next_prepare_at_utc='',error_code='',error_message='' WHERE id=?""",
                (
                    art["url"],
                    art["url"],
                    art.get("sha256", ""),
                    art.get("size", 0),
                    str(art.get("duration_seconds") or ""),
                    art["profile"],
                    now,
                    kind,
                    task_id,
                ),
            )
        store._refresh_run(conn, int(row["run_id"]), now)
    elif key == ("fb", "video"):
        if reason:
            conn.execute(
                "UPDATE fb_video_task SET status='failed',prepare_state='failed',error_code=?,updated_at_utc=? WHERE task_id=?",
                (reason, now, task_id),
            )
        else:
            prepared = {
                "media_url": art["url"],
                "sha256": art.get("sha256", ""),
                "size_bytes": art.get("size", 0),
                "duration_seconds": art.get("duration_seconds", 0),
                "profile": art["profile"],
                "media_selection_kind": kind,
                "random_overlay_recipe": art.get("random_overlay_recipe")
                or art.get("recipe")
                or {},
            }
            conn.execute(
                "UPDATE fb_video_task SET prepare_state='ready',prepared_json=?,prepare_retry_at='',updated_at_utc=?,error_code='',error_message='' WHERE task_id=?",
                (dump(prepared), now, task_id),
            )
    elif key == ("tt", "auto"):
        target = "skipped" if reason else "ready"
        if reason:
            conn.execute(
                "UPDATE tt_auto_task SET status='skipped',error_code=?,updated_at=?,finished_at_utc=?,next_attempt_at_utc='' WHERE id=?",
                (reason, now, now, task_id),
            )
        else:
            from features.tt_auto_posts.publisher import _stable_gpu_job_id

            task = bridge.store.get_task(int(task_id))
            conn.execute(
                """UPDATE tt_auto_task SET status='ready',gpu_job_id=?,source_media_url=?,prepared_media_url=?,
               prepared_output_sha256=?,prepared_output_size=?,prepared_duration_sec=?,updated_at=?,prepared_at_utc=?,
               next_attempt_at_utc='',claim_phase='',error_code='',error_message='' WHERE id=?""",
                (
                    task.gpu_job_id or _stable_gpu_job_id(task),
                    source,
                    art["url"],
                    art["sha256"],
                    art["size"],
                    art["duration_seconds"],
                    now,
                    now,
                    task_id,
                ),
            )
        conn.execute(
            "UPDATE tt_auto_material_ledger SET last_task_status=?,updated_at=? WHERE task_id=?",
            (target, now, task_id),
        )
        bridge.store._event(
            conn,
            event_type="pipeline_media_selected",
            task_id=int(task_id),
            run_id=row["run_id"],
            from_status=row["status"],
            to_status=target,
            created_at=now,
            details={"kind": kind, "reason": reason},
        )
    elif key == ("x", "queue") and reason:
        # The parent observes the native terminal outcome through the original
        # queue/log, including its specific no-write reason.
        conn.execute(
            "UPDATE x_post_queue SET status='failed',updated_at=? WHERE id=?",
            (now, task_id),
        )
        conn.execute(
            "UPDATE x_post_publish_log SET status='failed',error_code=?,error_message=?,updated_at=? WHERE queue_id=? AND status='reserved' AND attempt_count=0 AND unknown_outcome=0",
            (reason, "原素材未通过发布前检查", now, task_id),
        )


def handle(bridge, action, payload):
    job = dict(payload.get("job") or {})
    job.setdefault("generation", 0)
    if (
        job.get("source_key")
        != f"{job.get('channel')}:{job.get('family')}:{job.get('source_id')}"
        or job.get("stage") != "prepare"
        or (job.get("channel"), job.get("family"))
        not in {("fb", "auto"), ("fb", "video"), ("tt", "auto"), ("x", "queue")}
    ):
        raise AdapterError("media_source_not_supported")
    with bridge._source_lock(job):
        with _connect(bridge, job) as conn:
            row, source, due = read_only_snapshot(conn, bridge, job)
            record = existing(conn, job["source_key"])
        if action == "media-context":
            if (
                job["channel"] == "tt"
                and row
                and not row.get("material_id")
                and not record
            ):
                result = _selected_tt(bridge, job, row)
                if result:
                    return result
                with _connect(bridge, job) as conn:
                    row, source, due = read_only_snapshot(conn, bridge, job)
            return {
                "context": context_from_row(job["channel"], job["family"], row),
                "publish_at": due,
            }
        decision = payload.get("decision") or {}
        ident = str(payload.get("decision_id") or "")
        kind = decision.get("kind")
        if (
            not ident
            or kind not in KINDS
            or not isinstance(decision.get("source_identity"), str)
        ):
            raise AdapterError("media_decision_invalid")

        def replay(record):
            if record["decision_id"] != ident:
                raise AdapterError("media_decision_conflict")
            return _response(record)

        if record:
            return replay(record)
        with _connect(bridge, job) as conn:
            state = _scope_check(
                conn,
                job,
                row,
                due,
                context_from_row(job["channel"], job["family"], row),
                decision,
            )
        if state:
            return state
        try:
            art = _artifact(bridge, job, row, source, decision)
            if job["channel"] == "x":
                _prepare_x_route(bridge, job, row, source, art, decision)
            reason = ""
        except NotVideo:
            return {"already_started": True, "reason": "native_image_preflight"}
        except InvalidMedia as exc:
            if kind != "deadline_original":
                return {"candidate_invalid": True, "reason": str(exc)}
            art, reason = {}, str(exc)
        except (OSError, requests.RequestException, subprocess.TimeoutExpired) as exc:
            return {"retry": True, "reason": "media_preflight_unavailable"}
        with _connect(bridge, job) as conn:
            conn.execute("BEGIN IMMEDIATE")
            saved = existing(conn, job["source_key"])
            if saved:
                return replay(saved)
            row, source, due = read_only_snapshot(conn, bridge, job)
            state = _scope_check(
                conn,
                job,
                row,
                due,
                context_from_row(job["channel"], job["family"], row),
                decision,
            )
            if state:
                return state
            _write(conn, bridge, job, row, source, kind, art, reason)
            status = "skipped" if reason else "applied"
            conn.execute(
                "INSERT INTO pipeline_native_media_decision VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    job["source_key"],
                    ident,
                    kind,
                    decision["source_identity"],
                    decision.get("producer_source_key") or "",
                    dump(art),
                    due,
                    status,
                    reason,
                ),
            )
            conn.commit()
        if job["channel"] == "tt":
            bridge.service.executor._update_run(row["run_id"])
        return (
            {"skipped": True, "reason": reason}
            if reason
            else {"applied": True, "artifact": art}
        )


def _prepare_x_route(bridge, job, row, source, artifact, decision):
    from features.x_posts import duration_policy, service as posts
    from features.x_posts.media_limits import video_byte_limit

    if (
        row.get("delivery_mode") == "duration_pending"
        or row.get("media_validation_mode") == "deferred"
    ):
        original = (
            artifact
            if decision["kind"] == "deadline_original"
            else _artifact(bridge, job, row, source, {"kind": "deadline_original"})
        )
        request = {
            "source_url": source,
            "max_duration_seconds": posts.PREMIUM_MAX_DURATION_SECONDS,
            "max_media_bytes": bridge.r.POST_MAX_MEDIA_BYTES,
        }
        bridge.preflight(
            row,
            {"kind": "x_preflight", "request": request},
            {"source_url": source, "artifact": original},
        )
        row = bridge.store.get_queue(row["id"])
    _, _, account = bridge.account(row, target=False, verify_now=True)
    eligible = bool(account.get("long_video_publish_eligible"))
    byte_limit = min(bridge.r.POST_MAX_MEDIA_BYTES, video_byte_limit(eligible))
    duration_limit = (
        posts.PREMIUM_MAX_DURATION_SECONDS
        if eligible
        else duration_policy.standard_limit(row)
    )
    if artifact["size"] > byte_limit or artifact["duration_seconds"] > duration_limit:
        raise InvalidMedia("x_account_media_limit")
