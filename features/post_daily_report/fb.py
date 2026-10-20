"""Read-only daily reporting for the AI backend FB template publisher.

Legacy MySQL publishers are deliberately outside this adapter's scope.
"""

from collections import Counter

from .common import in_window, json_value, parse_time, read_db


_REASONS = {
    "fb_page_missing_eligible_token": ("Page 授权不可用", "补充或更新该 Page 的发布授权"),
    "fb_auto_no_eligible_video": ("没有满足筛选和冷却规则的视频", "检查素材储备、筛选条件及冷却周期"),
    "fb_auto_previous_run_backlog": ("上一时隙仍有任务积压", "检查视频制作、上传和发布耗时"),
    "fb_auto_due_slot_too_late": ("计划时隙超过迟到宽限", "检查调度运行情况及制作积压"),
    "fb_auto_task_too_late": ("任务超过迟到宽限", "检查制作吞吐与提前准备时间"),
    "fb_auto_page_pool_empty": ("Page 池为空", "检查模板选择的 Page 组及成员"),
    "fb_auto_page_pool_unpublishable": ("Page 池没有可发布账号", "检查 Page 成员及有效发布授权"),
    "fb_auto_capacity_exceeded": ("计划超出容量限制", "核对 Page 数量与每日发布频次"),
    "fb_auto_page_unknown_block": ("Page 存在结果不明的历史任务", "先核实历史发布结果，避免重复发布"),
    "fb_auto_template_version_changed": ("模板更新后旧版任务已取消", "确认新模板的后续发布计划"),
    "fb_auto_due_slot_template_changed": ("时隙对应的模板版本已变化", "确认新模板的后续发布计划"),
    "fb_auto_manual_template_disabled": ("模板停用后手动任务已取消", "确认是否需要重新安排发布"),
    "fb_graph_video_processing_failed": ("Meta 视频处理失败", "检查视频编码和平台返回的处理结果"),
    "fb_graph_video_processing": ("Meta 仍在处理视频", "等待并核查后续平台对账结果"),
    "fb_graph_reconcile_transient": ("暂时无法确认 Meta 处理结果", "检查后续平台对账结果"),
    "fb_graph_reconcile_all_credentials_rejected": ("全部授权均无法查询发布结果", "更新授权后先对账，避免直接重发"),
    "fb_auto_worker_interrupted": ("发布执行中断，结果不明", "先核实平台结果，避免直接重发"),
    "submitted": ("Meta 已接收，尚未确认发布", "等待并核查平台对账结果"),
    "unknown": ("发布结果不明", "先对账确认平台结果，避免重复发布"),
    "planned": ("视频尚未开始制作", "检查制作队列和调度积压"),
    "preparing": ("视频仍在制作", "检查 GPU 制作进度及耗时"),
    "ready": ("视频已就绪，等待发布", "检查发布调度及 Page 前序任务"),
    "running": ("发布请求仍在执行", "核查执行状态及后续对账结果"),
    "fb_report_confirmation_mismatch": ("任务与发布账本的成功凭证不一致", "核对任务和账本后再确认成功"),
    "fb_report_confirmation_time_missing": ("缺少发布确认时间", "检查发布账本时间字段"),
    "fb_report_after_cutoff": ("成功确认时间晚于本次统计截止", "在后续日报查看补发结果"),
    "fb_report_missing_tasks": ("计划包含的部分 Page 缺少任务记录", "核查计划和任务生成记录"),
}


def _source(trigger):
    return "manual_template" if trigger == "manual" else "auto_template"


def _new_row(source):
    return {"source": source, "label": "手动触发模板" if source == "manual_template" else "自动模板",
            "form": "Page 视频（随机排重）", "expected": 0, "published": 0, "late": 0,
            "pending": 0, "unknown": 0, "failed": 0, "reasons": [], "warnings": []}


def _rows(conn, table):
    return [dict(row) for row in conn.execute("SELECT * FROM " + table)]


def _confirmed(task, ledger):
    return bool(ledger and task.get("status") == ledger.get("status") == "published"
                and str(task.get("graph_post_id") or "").strip()
                and str(task.get("graph_post_id")) == str(ledger.get("graph_post_id"))
                and not task.get("unknown_outcome") and not ledger.get("unknown_outcome")
                and str(task.get("page_id")) == str(ledger.get("page_id")))


def collect(paths, window):
    """Count planned Page posts, requiring task AND ledger success evidence."""
    groups = {key: _new_row(key) for key in ("auto_template", "manual_template")}
    reasons = {key: Counter() for key in groups}
    warnings, evidence, prior = [], [], 0
    seen_success = set()
    with read_db(paths["fb"]) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {"fb_auto_run", "fb_auto_task", "fb_auto_publish_ledger", "fb_auto_due_slot"}
        missing = required - tables
        if missing:
            raise ValueError("FB 报表缺少必要数据表: " + ",".join(sorted(missing)))
        runs = {row["id"]: row for row in _rows(conn, "fb_auto_run")}
        tasks = _rows(conn, "fb_auto_task")
        ledgers = {row["task_id"]: row for row in _rows(conn, "fb_auto_publish_ledger")}
        due_slots = _rows(conn, "fb_auto_due_slot")
        snapshots = ({row["due_slot_id"]: row for row in _rows(conn, "fb_auto_due_target_snapshot")}
                     if "fb_auto_due_target_snapshot" in tables else {})
        schedule_plans = (_rows(conn, "fb_auto_schedule_plan")
                          if "fb_auto_schedule_plan" in tables else [])

    # A frozen random schedule can exist even when the scheduler failed to create
    # its due rows. Preserve that evidence rather than reporting an empty day.
    observed_slots = {(r.get("template_id"), parse_time(r.get("planned_publish_at_utc")))
                      for r in list(runs.values()) + due_slots}
    for plan in schedule_plans:
        if plan.get("local_date") != window.date:
            continue
        for slot_time in json_value(plan.get("times_json"), []):
            instant = parse_time(f"{window.date}T{slot_time}+08:00")
            if instant is None:
                groups["auto_template"]["expected"] = None
                groups["auto_template"]["warnings"].append("已保存的 FB 随机计划包含无法解析的时刻")
            elif (plan.get("template_id"), instant) not in observed_slots:
                groups["auto_template"]["expected"] = None
                groups["auto_template"]["warnings"].append(
                    f"模板 {plan['template_id']} 已冻结的 {slot_time} 计划缺少时隙及运行记录，预期条数未知")

    cohort_runs = {}
    for run_id, run in runs.items():
        planned = parse_time(run.get("planned_publish_at_utc"))
        if planned is None:
            # Old rows have no actual schedule evidence; do not substitute creation date.
            if in_window(run.get("created_at_utc"), window.start, window.end):
                key = _source(run.get("trigger_type"))
                groups[key]["expected"] = None
                groups[key]["warnings"].append(f"运行 {run_id} 缺少计划时间，昨日归属与预期未确认")
            continue
        if in_window(planned, window.start, window.end):
            cohort_runs[run_id] = run
            key = _source(run.get("trigger_type"))
            if groups[key]["expected"] is not None:
                groups[key]["expected"] += int(run.get("total_pages") or 0)
            evidence.append(f"fb_auto_run:{run_id}")

    run_slots = {(r.get("template_id"), r.get("slot_key")) for r in runs.values()}
    for due in due_slots:
        if not in_window(due.get("planned_publish_at_utc"), window.start, window.end):
            continue
        if due.get("run_id") in runs or (due.get("template_id"), due.get("slot_key")) in run_slots:
            continue
        key = _source(due.get("trigger_type"))
        row = groups[key]
        snapshot = snapshots.get(due["id"])
        count = int(snapshot["expected_pages"]) if snapshot is not None else None
        if count is None:
            row["expected"] = None
            row["warnings"].append(f"时隙 {due['id']} 未生成运行且无 Page 快照，预期条数未知")
        elif row["expected"] is not None:
            row["expected"] += count
        code = str(due.get("error_code") or "fb_report_no_run")
        # Known snapshots count Page posts; missing snapshots report slots separately.
        if count:
            row["pending" if due.get("status") in {"pending", "preparing"} else "failed"] += count
            reasons[key][code] += count
        else:
            detail = _REASONS.get(code, ("尚未生成发布任务", "核查时隙错误及 Page 池配置"))[0]
            row["warnings"].append(f"未生成任务的时隙 {due['id']}：{detail}（{code}）")
        evidence.append(f"fb_auto_due_slot:{due['id']}")

    counts = Counter()
    for task in tasks:
        run = runs.get(task.get("run_id"))
        if not run:
            if in_window(task.get("planned_publish_at_utc"), window.start, window.end):
                warnings.append(f"任务 {task['id']} 缺少所属运行，未计入来源统计")
            continue
        planned = parse_time(task.get("planned_publish_at_utc") or run.get("planned_publish_at_utc"))
        ledger = ledgers.get(task["id"])
        confirmed = _confirmed(task, ledger)
        completed = parse_time(task.get("completed_at_utc"))
        identity = (str(task.get("page_id")), str(task.get("graph_post_id")))
        if run["id"] not in cohort_runs:
            if (confirmed and planned is not None and planned < window.start
                    and in_window(completed, window.start, window.end) and identity not in seen_success):
                prior += 1
                seen_success.add(identity)
            continue
        counts[run["id"]] += 1
        key = _source(run.get("trigger_type"))
        row = groups[key]
        status = str(task.get("status") or "unknown")
        if confirmed and completed is not None and completed < window.cutoff:
            if identity in seen_success:
                row["unknown"] += 1
                reasons[key]["fb_report_confirmation_mismatch"] += 1
                row["warnings"].append(f"任务 {task['id']} 与其他任务使用同一发布 ID，成功数已去重")
            else:
                row["published" if completed < window.end else "late"] += 1
                seen_success.add(identity)
            continue
        if status == "published":
            if not confirmed:
                code = "fb_report_confirmation_mismatch"
            elif completed is None:
                code = "fb_report_confirmation_time_missing"
            else:
                code = "fb_report_after_cutoff"
            row["unknown"] += 1
        else:
            code = str(task.get("skip_reason") or task.get("error_code") or status)
            if task.get("unknown_outcome") or status == "unknown":
                row["unknown"] += 1
            elif status in {"planned", "preparing", "ready", "queued", "running", "submitted"}:
                row["pending"] += 1
            else:
                row["failed"] += 1
        reasons[key][code] += 1

    for run_id, run in cohort_runs.items():
        key = _source(run.get("trigger_type"))
        gap = int(run.get("total_pages") or 0) - counts[run_id]
        if gap > 0:
            groups[key]["unknown"] += gap
            reasons[key]["fb_report_missing_tasks"] += gap
        elif gap < 0:
            groups[key]["expected"] = None
            groups[key]["warnings"].append(f"运行 {run_id} 的任务数大于 Page 快照数，预期需核实")
    for key, row in groups.items():
        for code, count in reasons[key].most_common():
            known = _REASONS.get(code)
            reason, suggestion = known or ("发布未完成，具体原因待核实", "检查对应任务的错误记录后处理")
            row["reasons"].append({"code": code, "reason": reason, "count": count,
                                   "suggestion": suggestion, "confidence": "confirmed" if known else "unknown"})
        row["warnings"] = list(dict.fromkeys(row["warnings"]))
    return {"channel": "FB", "rows": list(groups.values()), "prior_completed": prior,
            "warnings": warnings, "evidence": evidence}
