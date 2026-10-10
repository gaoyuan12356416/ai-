"""Frozen, operator-selected material batches; automatic selection is unchanged."""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import urlsplit, urlunsplit

from .languages import page_language
from .repositories import MaterialCandidate

KIND = "exact_material_round_robin_v1"
LANGUAGE_ORDER = ("en", "es", "zh-tw", "id", "th")
WAITING = ("planned", "preparing", "ready", "running")
COS_SOURCE_HOST = "advertising-1306474899.cos.ap-hongkong.myqcloud.com"


class BatchError(RuntimeError):
    code = "fb_manual_batch_invalid"


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def prepare_source_url(value):
    """The same known COS object over TLS; preserve the original source receipt."""
    parsed = urlsplit(str(value or ""))
    if (parsed.scheme not in {"http", "https"} or parsed.hostname != COS_SOURCE_HOST
            or parsed.port is not None or parsed.username or parsed.password or parsed.fragment
            or not parsed.path.lower().endswith(".mp4")):
        raise BatchError("Manual source must be an MP4 on the configured COS source host")
    return urlunsplit(("https", parsed.netloc, parsed.path, parsed.query, ""))


def build_manifest(conn, template, pages, materials, input_ids, operation_id,
                   *, waived_series, waive_drama_cooldown, now):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,70}", operation_id):
        raise BatchError("Invalid operation ID")
    config = json.loads(template["config_json"])
    if template["status"] != "enabled" or config["video_template"] != "random_overlay":
        raise BatchError("An enabled random-overlay template is required")
    if len(set(input_ids)) != len(input_ids) or set(input_ids) != set(materials):
        raise BatchError("Exact input material IDs are required")
    if len({p.page_id for p in pages}) != len(pages):
        raise BatchError("Ambiguous Page membership")
    unknown = {r[0] for r in conn.execute("SELECT DISTINCT page_id FROM fb_auto_task WHERE status='unknown' OR unknown_outcome=1")}
    cutoff = (now - timedelta(days=int(config["cooldown_days"]))).isoformat(timespec="seconds")
    reserved = defaultdict(set)
    for r in conn.execute("SELECT page_id,material_id FROM fb_auto_task WHERE status IN ('planned','preparing','ready','running','submitted','unknown') OR (status IN ('published','failed_without_retry') AND created_at_utc>=?)", (cutoff,)):
        reserved[r[0]].add(r[1])
    by_language = defaultdict(list)
    for mid in input_ids:
        m = materials[mid]
        if m["series_code"] != waived_series or not m["media_url"] or not 0 < float(m["duration_seconds"]) <= 3600:
            raise BatchError("Material identity, series or duration is invalid")
        by_language[page_language(m["language"])].append(mid)
    queues, skipped = {}, []
    for language in LANGUAGE_ORDER:
        choices = by_language.get(language, [])
        if not choices:
            continue
        pointer, queue = 0, []
        for p in sorted((p for p in pages if page_language(p.language) == language), key=lambda p: int(p.page_id)):
            reason = "page_unknown" if p.page_id in unknown else ("missing_eligible_token" if p.eligible_token_count <= 0 else "")
            selected = None
            if not reason:
                for offset in range(len(choices)):
                    index = (pointer + offset) % len(choices)
                    if choices[index] not in reserved[p.page_id]:
                        selected = choices[index]
                        pointer = (index + 1) % len(choices)
                        break
                if selected is None:
                    reason = "same_material_cooldown"
            if reason:
                skipped.append({"page_id": p.page_id, "language": language, "reason": reason})
                continue
            m = materials[selected]
            queue.append({"page_id": p.page_id, "group_id": p.group_id, "language": language,
                          "material_id": selected, "content_id": m["content_id"], "source_media_url": m["media_url"]})
        queues[language] = queue
    entries = []
    for offset in range(max((len(q) for q in queues.values()), default=0)):
        for language in LANGUAGE_ORDER:
            if offset < len(queues.get(language, [])):
                entries.append({"sequence": len(entries) + 1, **queues[language][offset]})
    if not entries:
        raise BatchError("No publishable Page/material assignments")
    page_languages = {page_language(p.language) for p in pages}
    unused = [mid for mid in input_ids if page_language(materials[mid]["language"]) not in page_languages]
    return {"kind": KIND, "operation_id": operation_id, "template_id": int(template["id"]),
            "template_version": int(template["current_version"]),
            "template_config_sha256": hashlib.sha256(template["config_json"].encode()).hexdigest(),
            "owner_user_id": template["owner_user_id"], "group_ids": config["group_ids"],
            "waived_series": waived_series, "waive_drama_cooldown": waive_drama_cooldown is True,
            "input_ids": input_ids, "materials": materials,
            "pages": [asdict(p) for p in sorted(pages, key=lambda p: int(p.page_id))],
            "entries": entries, "skipped_pages": skipped, "unmatched_material_ids": unused}


def summary(manifest):
    return {"operation_id": manifest["operation_id"], "sha256": digest(manifest),
            "queued": len(manifest["entries"]), "skipped": len(manifest["skipped_pages"]),
            "language_counts": dict(Counter(x["language"] for x in manifest["entries"])),
            "material_counts": dict(Counter(x["material_id"] for x in manifest["entries"])),
            "skipped_pages": manifest["skipped_pages"], "unmatched_material_ids": manifest["unmatched_material_ids"]}


def candidate(raw):
    return MaterialCandidate(raw["material_id"], raw["content_id"], raw["media_url"],
                             raw["material_name"], raw["drama_name"], raw["language"],
                             Decimal(str(raw["duration_seconds"])), Decimal(0), None, Decimal(0), None,
                             "", raw["drama_description"], raw.get("material_tag") or "FBmanual")


def reserve_batch(store, manifest, *, max_jobs, max_daily_jobs, automatic_daily_jobs):
    """Only the trusted operator CLI invokes this, after live source revalidation."""
    from .core import utc_iso
    stamp, sha = utc_iso(store.now_fn()), digest(manifest)
    slot = "manual:materials:" + manifest["operation_id"]
    with store._lock, store.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute("SELECT id,config_json FROM fb_auto_run WHERE template_id=? AND slot_key=?", (manifest["template_id"], slot)).fetchone()
        if existing:
            marker = json.loads(existing["config_json"]).get("operator_material_batch", {})
            if marker.get("sha256") != sha:
                raise BatchError("Operation ID already exists with a different frozen scope")
            conn.commit()
            return {"run_id": existing["id"], "idempotent": True, **summary(manifest)}
        template = conn.execute("SELECT t.*,v.config_json FROM fb_auto_template t JOIN fb_auto_template_version v ON v.template_id=t.id AND v.version=t.current_version WHERE t.id=?", (manifest["template_id"],)).fetchone()
        if not template or template["status"] != "enabled" or template["current_version"] != manifest["template_version"] or hashlib.sha256(template["config_json"].encode()).hexdigest() != manifest["template_config_sha256"]:
            raise BatchError("Template changed after preview")
        live = build_manifest(conn, dict(template), [__import__("features.fb_auto_posts.repositories", fromlist=["PageTarget"]).PageTarget(**p) for p in manifest["pages"]], manifest["materials"], manifest["input_ids"], manifest["operation_id"], waived_series=manifest["waived_series"], waive_drama_cooldown=manifest["waive_drama_cooldown"], now=store.now_fn())
        if digest(live) != sha:
            raise BatchError("Page holds or material reservations changed after preview")
        count = len(manifest["entries"])
        if count > max_jobs:
            raise BatchError("Batch exceeds the existing per-run capacity")
        local = store.now_fn().astimezone(timezone(timedelta(hours=8)))
        start = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        end = start + timedelta(days=1)
        manual_today = conn.execute("SELECT COUNT(*) FROM fb_auto_task x JOIN fb_auto_run r ON r.id=x.run_id WHERE r.trigger_type='manual' AND x.created_at_utc>=? AND x.created_at_utc<?", (utc_iso(start), utc_iso(end))).fetchone()[0]
        if automatic_daily_jobs + manual_today + count > max_daily_jobs:
            raise BatchError("Batch exceeds the existing daily capacity")
        config = json.loads(template["config_json"])
        config["operator_material_batch"] = {"sha256": sha, "manifest": manifest}
        cur = conn.execute("INSERT INTO fb_auto_run(template_id,template_version,slot_key,trigger_type,status,config_json,total_pages,publishable_pages,missing_token_pages,queued_tasks,skipped_tasks,created_at_utc,planned_publish_at_utc,metric_generation_ids_json,video_template) VALUES(?,?,?,'manual','queued',?,?,?,?,?,0,?,?, '[]','random_overlay')", (manifest["template_id"], manifest["template_version"], slot, canonical(config), count, count, 0, count, stamp, stamp))
        rid = cur.lastrowid
        page_map = {p["page_id"]: p for p in manifest["pages"]}
        for entry in manifest["entries"]:
            p, m = page_map[entry["page_id"]], candidate(manifest["materials"][entry["material_id"]])
            conn.execute("INSERT INTO fb_auto_run_page(run_id,page_id,group_id,group_ids_json,owner_user_id,timezone,language,eligible_token_count,snapshot_status,skip_reason) VALUES(?,?,?,?,?,?,?,?,'eligible','')", (rid, p["page_id"], p["group_id"], json.dumps(p["group_ids"]), p["owner_user_id"], p["timezone"], entry["language"], p["eligible_token_count"]))
            job = "fb-page-" + hashlib.sha256(f"{manifest['template_id']}:{manifest['template_version']}:{slot}:{p['page_id']}".encode()).hexdigest()[:48]
            selection = canonical({"operator_material_batch": sha, "sequence": entry["sequence"]})
            cur = conn.execute("INSERT INTO fb_auto_task(run_id,template_id,template_version,page_id,group_id,status,material_id,content_id,created_at_utc,planned_publish_at_utc,video_template,gpu_job_id,source_media_url,selection_json) VALUES(?,?,?,?,?,'planned',?,?,?,?,'random_overlay',?,?,?)", (rid, manifest["template_id"], manifest["template_version"], p["page_id"], p["group_id"], m.material_id, m.content_id, stamp, stamp, job, m.media_url, selection))
            tid = cur.lastrowid
            from .repositories import PageTarget
            page = PageTarget(**p)
            short, long = store._link_values(page, m, tid, int(store.now_fn().timestamp()))
            conn.execute("UPDATE fb_auto_task SET message_text=?,short_url=?,long_url=? WHERE id=?", (store._message(config, m, short), short, long, tid))
        conn.commit()
    return {"run_id": rid, "idempotent": False, **summary(manifest)}


def task_policy(conn, task):
    """Return a policy only for a task matching its immutable manual manifest."""
    run = conn.execute("SELECT template_id,template_version,trigger_type,config_json FROM fb_auto_run WHERE id=?", (task["run_id"],)).fetchone()
    config = json.loads(run["config_json"])
    marker = config.get("operator_material_batch")
    if marker is None:
        from .hit_material import task_policy as hit_material_policy
        return hit_material_policy(run, task, config)
    if "hit_material_publish" in config:
        raise BatchError("Conflicting immutable manual policies")
    manifest = marker.get("manifest", {}) if isinstance(marker, dict) else {}
    entries = manifest.get("entries", [])
    if (run["trigger_type"] != "manual" or manifest.get("kind") != KIND or digest(manifest) != marker.get("sha256")
            or manifest.get("template_id") != run["template_id"] or manifest.get("template_version") != run["template_version"]
            or len({x["page_id"] for x in entries}) != len(entries)):
        raise BatchError("Invalid immutable manual batch receipt")
    selection = json.loads(task["selection_json"])
    seq = selection.get("sequence", 0)
    if not isinstance(seq, int) or not 1 <= seq <= len(entries) or selection.get("operator_material_batch") != marker["sha256"]:
        raise BatchError("Invalid task batch sequence")
    entry = entries[seq - 1]
    if entry.get("sequence") != seq or any(str(task[k]) != str(entry[k]) for k in ("page_id", "group_id", "material_id", "content_id", "source_media_url")):
        raise BatchError("Task differs from its frozen material/Page assignment")
    ids = [x[0] for x in conn.execute("SELECT id FROM fb_auto_task WHERE run_id=? ORDER BY id", (task["run_id"],))]
    if len(ids) != len(entries) or ids[seq - 1] != task["id"]:
        raise BatchError("Batch task set or order changed")
    earlier = conn.execute("SELECT 1 FROM fb_auto_task WHERE run_id=? AND id<? AND status IN ('planned','preparing','ready','running') LIMIT 1", (task["run_id"], task["id"])).fetchone()
    return {"waive_drama_cooldown": manifest.get("waive_drama_cooldown") is True, "waiting_for_previous": bool(earlier)}
