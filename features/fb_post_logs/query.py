"""Read one row per native Facebook task. Only hydrate the selected page."""

import json
import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

BJ = timezone(timedelta(hours=8))
STATES = {
    "planned": "待准备",
    "preparing": "准备中",
    "ready": "已就绪",
    "queued": "已入队",
    "running": "正在执行",
    "submitted": "已提交 Meta",
    "published": "已发布",
    "unknown": "结果待确认",
    "failed": "失败",
    "failed_without_retry": "失败（禁止重试）",
    "skipped": "已跳过",
    "cancelled": "已取消",
    "stopped": "已停止",
}
SOURCES = {
    "auto": "自动模板",
    "template_manual": "模板手动触发",
    "manual": "手动视频",
    "excel": "Excel 导入",
    "pool": "Page 素材池",
    "group_pool": "组素材池",
    "profile": "画像选材",
}
STOP = "fb_post_log_operator_stopped"
FAMILIES = {
    "auto": ("fb_auto_task", "id", "planned_publish_at_utc"),
    "video": ("fb_video_task", "task_id", "planned_at_utc"),
}


def obj(value):
    try:
        data = json.loads(value or "{}")
        return data if isinstance(data, dict) else {}
    except (TypeError, ValueError):
        return {}


def one(conn, sql, args):
    row = conn.execute(sql, args).fetchone()
    return dict(row) if row else {}


def filtered(family, params):
    table, pk, planned = FAMILIES[family]
    joins, clauses, args = [], [], []
    if params.get("template_id"):
        if family != "auto":
            return None
        clauses.append("t.template_id=?")
        args.append(params["template_id"])
    source = params.get("task_source")
    if source:
        if family == "auto":
            if source not in ("auto", "template_manual", "profile"):
                return None
            if source == "profile":
                clauses.append("json_type(t.selection_json,'$.tag_profile')='object'")
            else:
                joins.append("JOIN fb_auto_run b ON b.id=t.run_id")
                clauses.append("b.trigger_type=?")
                args.append("manual" if source == "template_manual" else "auto")
        else:
            if source in ("auto", "template_manual", "profile"):
                return None
            clauses.append("t.source_kind=?")
            args.append("manual" if source == "excel" else source)
            if source in ("manual", "excel"):
                joins.append("JOIN fb_video_batch b ON b.batch_id=t.batch_id")
                clauses.append(
                    "json_type(b.request_json,'$.excel') IS "
                    + ("'object'" if source == "excel" else "NOT 'object'")
                )
    for name in ("page_id", "material_id"):
        if params.get(name):
            clauses.append("t." + name + "=?")
            args.append(params[name])
    if params.get("run_date"):
        day = date.fromisoformat(params["run_date"])
        start = datetime.combine(day, datetime.min.time(), tzinfo=BJ).astimezone(timezone.utc)
        stamp = "coalesce(nullif(t." + planned + ",''),t.created_at_utc)"
        clauses.extend([stamp + ">=?", stamp + "<?"])
        args.extend(x.strftime("%Y-%m-%dT%H:%M:%S") for x in (start, start + timedelta(days=1)))
    if params.get("unknown_outcome") in ("0", "1"):
        clauses.append("(t.unknown_outcome<>0 OR t.status='unknown')=?")
        args.append(int(params["unknown_outcome"]))
    status = params.get("status")
    if status:
        held = (
            "EXISTS (SELECT 1 FROM pipeline_canonical_hold h WHERE h.source_key='fb:"
            + family
            + ":'||t."
            + pk
            + " AND h.reason=?)"
        )
        if status != "stopped":
            clauses.append("t.status=?")
            args.append(status)
        clauses.append(held if status == "stopped" else "NOT " + held)
        args.append(STOP)
    tail = " FROM " + table + " t " + " ".join(joins)
    if clauses:
        tail += " WHERE " + " AND ".join(clauses)
    return tail, args


def page_keys(conn, params):
    if set(params) - {
        "page",
        "page_size",
        "run_date",
        "page_id",
        "material_id",
        "template_id",
        "task_source",
        "status",
        "unknown_outcome",
    }:
        raise ValueError("查询参数无效")
    if params.get("template_id") and not re.fullmatch(r"[1-9][0-9]{0,17}", params["template_id"]):
        raise ValueError("模板 ID 无效")
    for name, allowed in (("task_source", SOURCES), ("status", STATES), ("unknown_outcome", ("0", "1"))):
        if params.get(name) and params[name] not in allowed:
            raise ValueError("筛选值无效")
    for name in ("page_id", "material_id"):
        if params.get(name) and not re.fullmatch(r"[0-9]{1,40}", params[name]):
            raise ValueError("Page ID 和素材 ID 须为数字")
    page, size = int(params.get("page", 1)), int(params.get("page_size", 20))
    if page < 1 or not 1 <= size <= 100:
        raise ValueError("分页参数无效")
    branches, bindings, total = [], [], 0
    for family, (_, pk, _) in FAMILIES.items():
        result = filtered(family, params)
        if result is None:
            continue
        tail, args = result
        total += conn.execute("SELECT count(*)" + tail, args).fetchone()[0]
        branches.append("SELECT '" + family + "' family,t." + pk + " task_id,t.created_at_utc created" + tail)
        bindings.extend(args)
    page = min(page, max(1, (total + size - 1) // size))
    if not branches:
        return [], {"page": 1, "page_size": size, "total": 0}
    sql = " UNION ALL ".join(branches) + " ORDER BY created DESC,family,task_id DESC LIMIT ? OFFSET ?"
    return list(conn.execute(sql, [*bindings, size, (page - 1) * size])), {
        "page": page,
        "page_size": size,
        "total": total,
    }


def display(conn, family, task, evidence):
    auto = family == "auto"
    key = task[FAMILIES[family][1]]
    source = task.get("source_kind") or "manual"
    proof = obj(evidence["publication"].get("proof_json"))
    snap = obj(task.get("selection_json") if auto else task.get("snapshot_json"))
    story = snap.get("story_copy") or {}
    material, page = snap.get("material") or {}, snap.get("page") or {}
    attrs = snap.get("attribution") or {}
    pool_id = None
    template = ""
    if auto:
        batch = one(
            conn,
            """SELECT r.trigger_type,t.name FROM fb_auto_run r LEFT JOIN
            fb_auto_template t ON r.template_id=t.id WHERE r.id=?""",
            (task["run_id"],),
        )
        source = "template_manual" if batch.get("trigger_type") == "manual" else "auto"
        label = SOURCES[source] + (" · 画像选材" if isinstance(snap.get("tag_profile"), dict) else "")
        batch_label = "批次 " + str(task["run_id"])
        template = batch.get("name")
    else:
        if (
            source == "manual"
            and one(
                conn,
                """SELECT json_type(request_json,'$.excel') AS imported
                    FROM fb_video_batch WHERE batch_id=?""",
                (task["batch_id"],),
            ).get("imported")
            == "object"
        ):
            source = "excel"
        label = SOURCES.get(source, source)
        batch_label = "批次 " + task["batch_id"]
        pool_id = (snap.get("group_pool") or snap.get("pool") or {}).get("item_id")
        if source == "group_pool" and not pool_id:
            pool_id = one(
                conn,
                "SELECT item_id FROM fb_group_pool_reservation WHERE tenant_key=? AND material_id=? AND task_id=?",
                (task["tenant_key"], task["material_id"], key),
            ).get("item_id")
    try:
        query = parse_qs(urlsplit(task.get("long_url") or "").query)
    except ValueError:
        query = {}

    def attr(name):
        return next(iter(query.get(name, [])), "")

    planned = task[FAMILIES[family][2]] or task["created_at_utc"]
    run_date = datetime.fromisoformat(planned.replace("Z", "+00:00")).astimezone(BJ).date().isoformat()
    status = "stopped" if evidence["hold"].get("reason") == STOP else task["status"]
    css = {
        "failed_without_retry": "failed",
        "submitted": "unknown",
        "preparing": "queued",
        "ready": "queued",
    }.get(status, status)
    return dict(
        queue_id=evidence["key"],
        run_date=run_date,
        batch_label=batch_label,
        source_label=label,
        template_name=template,
        page_id=task["page_id"],
        page_name=page.get("page_name") or attrs.get("page_name") or attr("af_adset"),
        material_id=task["material_id"],
        content_id=task["content_id"],
        material_name=material.get("material_name")
        or attrs.get("material_name")
        or attr("af_ad").split("_contentid[")[0],
        drama_name=material.get("drama_name") or attrs.get("drama_name") or story.get("title"),
        language=material.get("language") or story.get("language") or snap.get("language"),
        pool_sequence=task.get("pool_sequence"),
        pool_item_id=pool_id,
        status=status,
        status_label=STATES.get(status, status),
        status_class=css,
        short_url=task["short_url"],
        graph_post_id=task["graph_post_id"] or evidence["publication"].get("publish_id"),
        post_url=(proof.get("permalink_url") or proof.get("post_url")) if status == "published" else "",
        planned_at=planned,
        published_at=evidence["publication"].get("published_at"),
        finished_at=task.get("completed_at_utc"),
        updated_at=task.get("updated_at_utc"),
        error_code=task["error_code"] or task.get("skip_reason"),
        error_message=task["error_message"],
        attempt_count=evidence["attempt_count"],
    )
