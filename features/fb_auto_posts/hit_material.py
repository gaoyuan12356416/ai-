"""Audited one-material/all-Pages runs initiated by the authenticated UI."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict
from datetime import timedelta
from decimal import Decimal

from .core import ActorScope, BEIJING, StoreError, utc_iso
from .manual_batch import BatchError, canonical, digest
from .strategy import daily_capacity, daily_limit


KIND = "exact_material_all_pages_v1"
MARKER = "hit_material_publish"


def normalize_request(template_id, payload):
    if set(payload) != {"expected_version", "material_id", "operation_id"}:
        raise StoreError("invalid_request", "爆款素材发布请求字段无效", 400)
    version, material_id = payload["expected_version"], payload["material_id"]
    if (isinstance(version, bool) or not isinstance(version, (int, str))
            or not re.fullmatch(r"[1-9][0-9]{0,8}", str(version))):
        raise StoreError("invalid_request", "模板版本无效", 400)
    if (isinstance(material_id, bool) or not isinstance(material_id, (int, str))
            or not re.fullmatch(r"[1-9][0-9]{0,30}", str(material_id))):
        raise StoreError("invalid_request", "请输入单个正整数素材ID", 400)
    operation_id = payload["operation_id"]
    if not isinstance(operation_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,70}", operation_id):
        raise StoreError("invalid_request", "操作标识无效", 400)
    return {"template_id": int(template_id), "expected_version": int(version),
            "material_id": str(material_id), "operation_id": operation_id}


def _existing(conn, request):
    row = conn.execute("SELECT id,config_json FROM fb_auto_run WHERE template_id=? AND slot_key=?",
                       (request["template_id"], "manual:hit-material:" + request["operation_id"])).fetchone()
    if row is None:
        return None
    marker = json.loads(row["config_json"]).get(MARKER, {})
    manifest = marker.get("manifest", {})
    if not manifest or digest(manifest) != marker.get("sha256"):
        raise StoreError("fb_auto_hit_material_receipt_invalid", "爆款素材发布回执校验失败，请联系管理员核查", 409)
    if manifest.get("request") != request:
        raise StoreError("fb_auto_operation_conflict", "同一操作标识已用于不同素材或模板版本，请重新发起操作", 409)
    return {"ok": True, "run_id": int(row["id"]), "idempotent": True,
            "operation_id": request["operation_id"], **manifest["result"]}


def _validate_template(template, request):
    if int(template["current_version"]) != request["expected_version"]:
        raise StoreError("fb_auto_version_conflict", "模板版本已更新，请刷新列表后重试", 409)
    if template["status"] != "enabled":
        raise StoreError("fb_auto_template_disabled", "模板已停用，请启用后再发布爆款素材", 409)
    if json.loads(template["config_json"]).get("video_template") != "random_overlay":
        raise StoreError("fb_auto_video_template_required", "爆款素材发布需要随机排重视频模板", 409)


def _gates(runtime):
    if not runtime.executor.live_enabled:
        raise StoreError("fb_auto_live_gate_closed", "FB自动发布总开关关闭，未创建运行或调用GPU/Meta", 409)
    if not runtime.prebuild_enabled:
        raise StoreError("fb_auto_prebuild_gate_closed", "FB自动发布预制开关关闭，未创建运行或调用GPU", 409)


def _skip_reason(conn, store, config, page, material_id, stamp):
    if daily_limit(config, page.page_id) == 0:
        return "fb_auto_page_frequency_limit"
    if conn.execute("SELECT 1 FROM fb_auto_task WHERE page_id=? AND (status='unknown' OR unknown_outcome=1) LIMIT 1", (page.page_id,)).fetchone():
        return "fb_auto_page_unknown_block"
    if conn.execute("SELECT 1 FROM fb_auto_publish_ledger WHERE page_id=? AND (status='unknown' OR unknown_outcome=1) LIMIT 1", (page.page_id,)).fetchone():
        return "fb_auto_page_unknown_block"
    if page.eligible_token_count <= 0:
        return "fb_page_missing_eligible_token"
    if conn.execute("SELECT 1 FROM fb_auto_task WHERE page_id=? AND material_id=? AND status IN ('planned','preparing','ready','running','submitted','unknown') LIMIT 1", (page.page_id, material_id)).fetchone():
        return "fb_auto_material_active"
    if material_id in store._cooldown_material_ids(conn, page.page_id, int(config["cooldown_days"])):
        return "fb_auto_material_cooldown"
    if conn.execute("SELECT 1 FROM fb_auto_task WHERE page_id=? AND planned_publish_at_utc=? AND status IN ('planned','preparing','ready','running','submitted') LIMIT 1", (page.page_id, stamp)).fetchone():
        return "fb_auto_page_task_conflict"
    return ""


def publish_material(runtime, template_id, actor, payload):
    """Rehydrate server-owned inputs, then atomically reserve every Page assignment."""
    request = normalize_request(template_id, payload)
    store = runtime.store
    with store.connect() as conn:
        template = dict(store._template_row(conn, template_id, actor))
        existing = _existing(conn, request)
        if existing:
            return existing
    _validate_template(template, request)
    _gates(runtime)
    config = json.loads(template["config_json"])
    scope = ActorScope(actor.user_id, actor.name, bool(template["scope_is_admin"]), str(template["owner_user_id"]))
    runtime.resolve_source(config, scope)
    pages = runtime.pages.list_pages(config["group_ids"], is_admin=scope.is_admin, owner_user_id=scope.owner_user_id)
    if not pages:
        raise StoreError("fb_auto_page_pool_empty", "模板当前没有有效Page，未创建运行", 409)
    if len({p.page_id for p in pages}) != len(pages):
        raise StoreError("fb_auto_page_scope_invalid", "Page池返回重复Page，未创建运行", 409)
    if runtime.pages.legacy_conflicts(config["group_ids"]):
        raise StoreError("fb_auto_legacy_queue_conflict", "模板Page池仍被旧版自动发布队列占用，未创建运行", 409)
    enabled = store.enabled_template_sources(template_id)
    fingerprint = {(int(template_id), request["expected_version"])}
    automatic_daily_jobs = daily_capacity(config, pages)
    target_ids = {p.page_id for p in pages}
    for other in enabled:
        other_pages = runtime.pages.list_pages(other["config"]["group_ids"], is_admin=other["scope_is_admin"], owner_user_id=other["owner_user_id"])
        if target_ids.intersection(p.page_id for p in other_pages):
            raise StoreError("fb_auto_page_template_conflict", "模板Page与其他启用模板重叠，未创建运行", 409)
        automatic_daily_jobs += daily_capacity(other["config"], other_pages)
        fingerprint.add((int(other["template_id"]), int(other["template_version"])))
    material = runtime.materials.exact_material(config, request["material_id"])
    if str(material.material_id) != request["material_id"]:
        raise StoreError("fb_auto_material_identity_invalid", "素材仓库返回的ID与指定素材不一致", 409)
    # No remote writes or GPU calls occur above or inside the reservation.
    with store._lock, store.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = store._template_row(conn, template_id, actor)
        existing = _existing(conn, request)
        if existing:
            conn.commit()
            return existing
        _validate_template(current, request)
        _gates(runtime)
        if current["config_sha256"] != template["config_sha256"]:
            raise StoreError("fb_auto_version_conflict", "模板配置已变化，请刷新后重试", 409)
        current_enabled = {(int(r[0]), int(r[1])) for r in conn.execute("SELECT id,current_version FROM fb_auto_template WHERE status='enabled'")}
        if current_enabled != fingerprint:
            raise StoreError("fb_auto_capacity_snapshot_changed", "启用模板集合已变化，请重试容量复核", 409)
        now = store.now_fn()
        stamp = utc_iso(now)
        reasons = {p.page_id: _skip_reason(conn, store, config, p, material.material_id, stamp) for p in pages}
        skipped = [{"page_id": p.page_id, "reason": reasons[p.page_id]} for p in pages if reasons[p.page_id]]
        count = len(pages) - len(skipped)
        local_start = now.astimezone(BEIJING).replace(hour=0, minute=0, second=0, microsecond=0)
        manual_today = conn.execute("""SELECT COUNT(*) FROM fb_auto_task x JOIN fb_auto_run r ON r.id=x.run_id
            WHERE r.trigger_type='manual' AND x.status<>'skipped' AND x.created_at_utc>=? AND x.created_at_utc<?""",
            (utc_iso(local_start), utc_iso(local_start + timedelta(days=1)))).fetchone()[0]
        if (count > runtime.max_publishable_pages or count > runtime.max_jobs_per_slot
                or (count and automatic_daily_jobs + manual_today + count > runtime.max_daily_jobs)):
            raise StoreError("fb_auto_capacity_exceeded", "爆款素材发布超过当前单轮或每日容量，未创建运行", 409)
        slot = "manual:hit-material:" + request["operation_id"]
        result = {"queued": count, "skipped": len(skipped), "total_pages": len(pages), "skipped_pages": skipped}
        entries = []
        for page in pages:
            if not reasons[page.page_id]:
                entries.append({"sequence": len(entries) + 1, "page_id": page.page_id, "group_id": page.group_id,
                                "material_id": material.material_id, "content_id": material.content_id,
                                "source_media_url": material.media_url})
        frozen_material = {key: str(value) if isinstance(value, Decimal) else value for key, value in asdict(material).items()}
        manifest = {"kind": KIND, "request": request, "template_config_sha256": template["config_sha256"],
                    "actor": asdict(actor), "pages": [asdict(p) for p in pages], "material": frozen_material,
                    "entries": entries, "result": result, "cooldown_days": int(config["cooldown_days"])}
        sha = digest(manifest)
        frozen_config = {**config, MARKER: {"sha256": sha, "manifest": manifest}}
        cur = conn.execute("""INSERT INTO fb_auto_run(template_id,template_version,slot_key,trigger_type,status,config_json,
            total_pages,publishable_pages,missing_token_pages,queued_tasks,skipped_tasks,created_at_utc,completed_at_utc,
            planned_publish_at_utc,metric_generation_ids_json,video_template) VALUES(?,?,?,'manual',?,?,?,?,?,?,?,?,?,?,'[]','random_overlay')""",
            (template_id, request["expected_version"], slot, "queued" if count else "completed", canonical(frozen_config),
             len(pages), sum(p.eligible_token_count > 0 for p in pages), sum(p.eligible_token_count <= 0 for p in pages),
             count, len(skipped), stamp, "" if count else stamp, stamp))
        run_id = int(cur.lastrowid)
        entry_by_page = {e["page_id"]: e for e in entries}
        for page in pages:
            reason = reasons[page.page_id]
            conn.execute("""INSERT INTO fb_auto_run_page(run_id,page_id,group_id,group_ids_json,owner_user_id,timezone,
                language,eligible_token_count,snapshot_status,skip_reason) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (run_id, page.page_id, page.group_id, canonical(page.group_ids), page.owner_user_id, page.timezone,
                 page.language, page.eligible_token_count, "skipped" if reason else "eligible", reason))
            job = "fb-page-" + hashlib.sha256(f"{template_id}:{request['expected_version']}:{slot}:{page.page_id}".encode()).hexdigest()[:48]
            selection = {MARKER: sha, "sequence": entry_by_page.get(page.page_id, {}).get("sequence", 0)}
            cur = conn.execute("""INSERT INTO fb_auto_task(run_id,template_id,template_version,page_id,group_id,status,skip_reason,
                material_id,content_id,created_at_utc,completed_at_utc,planned_publish_at_utc,video_template,gpu_job_id,
                source_media_url,selection_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'random_overlay',?,?,?)""",
                (run_id, template_id, request["expected_version"], page.page_id, page.group_id, "skipped" if reason else "planned",
                 reason, material.material_id, material.content_id, stamp, stamp if reason else "", stamp, job,
                 material.media_url, canonical(selection)))
            if not reason:
                task_id = int(cur.lastrowid)
                short, long = (store._link_values(page, material, task_id, int(now.timestamp()))
                               if "{{url}}" in config["message_template"] else ("", ""))
                conn.execute("UPDATE fb_auto_task SET message_text=?,short_url=?,long_url=? WHERE id=?",
                             (store._message(config, material, short), short, long, task_id))
        conn.commit()
    return {"ok": True, "run_id": run_id, "idempotent": False, "operation_id": request["operation_id"], **result}


def task_policy(run, task, config):
    """Only intact server-created assignments may bypass automatic drama selection."""
    try:
        return _task_policy(run, task, config)
    except (ValueError, KeyError, TypeError, IndexError, AttributeError):
        raise BatchError("Invalid immutable hit-material receipt") from None


def _task_policy(run, task, config):
    marker = config.get(MARKER)
    selection = json.loads(task["selection_json"])
    if marker is None and MARKER not in selection:
        return None
    manifest = marker.get("manifest", {}) if isinstance(marker, dict) else {}
    request, entries = manifest.get("request", {}), manifest.get("entries", [])
    if (run["trigger_type"] != "manual" or manifest.get("kind") != KIND or digest(manifest) != marker.get("sha256")
            or request.get("template_id") != run["template_id"] or request.get("expected_version") != run["template_version"]
            or len({entry["page_id"] for entry in entries}) != len(entries)
            or selection.get(MARKER) != marker["sha256"]):
        raise BatchError("Invalid immutable hit-material receipt")
    sequence = selection.get("sequence")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or not 1 <= sequence <= len(entries):
        raise BatchError("Invalid hit-material assignment")
    entry = entries[sequence - 1]
    if entry.get("sequence") != sequence or any(str(task[key]) != str(entry[key]) for key in ("page_id", "group_id", "material_id", "content_id", "source_media_url")):
        raise BatchError("Task differs from its frozen hit-material assignment")
    return {"waive_drama_cooldown": True, "waiting_for_previous": False, "material_cooldown_basis": "created_at"}
