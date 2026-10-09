"""Read-only daily reporting for the AI backend FB template publisher.

Legacy MySQL publishers are deliberately outside this adapter's scope.
"""

from collections import Counter
import hashlib

from .common import BEIJING, in_window, json_value, parse_time, read_db


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
    "fb_graph_389": ("Meta 无法从 URL 抓取视频", "检查视频地址的外部访问与 Meta 抓取链路；换 Token 不能解决视频抓取问题"),
    "fb_graph_190": ("Meta 拒绝发布授权", "核查账户确认状态、Page 角色、双重验证及授权有效性"),
    "fb_graph_200": ("发布权限被 Meta 拒绝", "核查该授权的 Page 发布权限"),
    "fb_graph_368": ("Meta 拦截了发布操作", "核查平台限制与具体错误；不能仅凭错误码认定 Page 被封"),
    "fb_graph_network_outcome_unknown": ("Meta 请求连接中断，实际发布结果不明", "先核实平台结果，避免重复发布"),
    "fb_graph_response_outcome_unknown": ("Meta 响应无法解析，实际发布结果不明", "先核实平台结果，避免重复发布"),
    "fb_graph_id_missing": ("Meta 响应缺少发布 ID，结果不明", "先核实平台结果，避免重复发布"),
    "fb_auto_drama_cooldown_at_publish": ("同 Page 同剧触发发布前冷却规则", "按策略正常跳过，不计发布失败"),
    "fb_auto_material_cooldown_at_publish": ("同 Page 同素材触发发布前冷却规则", "按策略正常跳过，不计发布失败"),
    "fb_auto_page_frequency_limit": ("按 Page 发布频次策略正常跳过", "自动计划的频次外位置不计应发或失败"),
    "fb_auto_page_unknown_at_publish": ("Page 在发布前被结果不明任务阻塞", "先核实历史发布结果，避免重复发布"),
    "fb_report_late_unknown_block": ("视频已制作，但被同 Page 结果不明任务阻塞至超时", "先核实阻塞任务的发布结果，再恢复后续正常计划"),
    "fb_report_prepare_retries_expired": ("视频制作反复延后重试，超过发布宽限", "保留底层制作错误，定位失败并限制重复重试"),
}

_POLICY_SKIPS = {"fb_auto_page_frequency_limit", "fb_auto_drama_cooldown_at_publish",
                 "fb_auto_material_cooldown_at_publish"}


def _source(trigger):
    return "manual_template" if trigger == "manual" else "auto_template"


def _new_row(source):
    return {"source": source, "label": "手动触发模板" if source == "manual_template" else "自动模板",
            "form": "Page 视频（随机排重）", "expected": 0, "published": 0, "late": 0,
            "pending": 0, "unknown": 0, "failed": 0, "blocked": 0,
            "policy_skipped": 0, "frequency_excluded": 0, "raw_targets": 0,
            "reasons": [], "policy_reasons": [], "warnings": []}


def _allowed_pages(run, page_ids, plans):
    """Reconstruct frozen slot eligibility; never consult current Page settings."""
    if run.get("trigger_type") == "manual" or "config_json" not in run:
        return set(page_ids)
    config = json_value(run.get("config_json"))
    if not isinstance(config, dict):
        raise ValueError("missing frozen FB config")
    if not config.get("page_daily_limits") and "default_daily_count" not in config:
        return set(page_ids)
    schedule = config.get("schedule", {})
    times = schedule.get("times") if schedule.get("mode") == "fixed" else None
    if schedule.get("mode") == "random":
        planned = parse_time(run.get("planned_publish_at_utc"))
        local_date = planned.astimezone(BEIJING).date().isoformat() if planned else None
        matching = [p for p in plans if p.get("template_id") == run.get("template_id")
                    and p.get("template_version") == run.get("template_version")
                    and p.get("local_date") == local_date]
        if len(matching) == 1:
            times = json_value(matching[0].get("times_json"))
    if (not isinstance(times, list) or not times or len(set(times)) != len(times)
            or str(run.get("slot_key", ""))[-5:] not in times):
        raise ValueError("unproven frozen FB slots")
    limits = config.get("page_daily_limits", [])
    overrides = {}
    for item in limits:
        page_id, count = str(item["page_id"]), item["daily_count"]
        if type(count) is not int or count < 0 or page_id in overrides:
            raise ValueError("invalid frozen FB frequency")
        overrides[page_id] = count
    default = config.get("default_daily_count", len(times))
    if type(default) is not int or default < 0:
        raise ValueError("invalid frozen FB default frequency")
    index = times.index(str(run.get("slot_key", ""))[-5:])
    allowed = set()
    for page_id in page_ids:
        count = min(len(times), overrides.get(str(page_id), default))
        offset = int(hashlib.sha256(str(page_id).encode()).hexdigest()[:16], 16) % len(times)
        if count and index in {(int(i * len(times) / count) + offset) % len(times) for i in range(count)}:
            allowed.add(str(page_id))
    return allowed


def _quota(run, page_ids, plans):
    try:
        if _has_frequency(run) and len(set(page_ids)) != int(run.get("total_pages") or 0):
            raise ValueError("incomplete frozen FB Page scope")
        return _allowed_pages(run, page_ids, plans)
    except (ValueError, TypeError, KeyError, ZeroDivisionError):
        return None


def _has_frequency(run):
    if run.get("trigger_type") == "manual" or "config_json" not in run:
        return False
    config = json_value(run.get("config_json"))
    return not isinstance(config, dict) or bool(config.get("page_daily_limits")) or "default_daily_count" in config


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
    policy_reasons = {key: Counter() for key in groups}
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
        run_pages = _rows(conn, "fb_auto_run_page") if "fb_auto_run_page" in tables else []
        versions = {(v["template_id"], v["version"]): v["config_json"]
                    for v in _rows(conn, "fb_auto_template_version")} if "fb_auto_template_version" in tables else {}
        fetch_failures = ({r[0] for r in conn.execute(
            "SELECT DISTINCT task_id FROM fb_auto_publish_attempt WHERE error_code='fb_graph_389'")}
            if "fb_auto_publish_attempt" in tables else set())

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

    page_scope = {}
    for item in run_pages or tasks:
        page_scope.setdefault(item.get("run_id"), set()).add(str(item.get("page_id")))
    unknown_by_page = {}
    for task in tasks:
        if task.get("status") == "unknown" or task.get("unknown_outcome"):
            unknown_by_page.setdefault(str(task.get("page_id")), []).append(task)
    cohort_runs, quotas, run_expected = {}, {}, {}
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
            row = groups[key]
            raw_count = int(run.get("total_pages") or 0)
            row["raw_targets"] += raw_count
            quotas[run_id] = _quota(run, page_scope.get(run_id, set()), schedule_plans)
            count = (len(quotas[run_id]) if _has_frequency(run) else raw_count) if quotas[run_id] is not None else None
            run_expected[run_id] = count
            if count is None:
                row["expected"] = None
                row["warnings"].append(f"运行 {run_id} 的冻结 Page 频次或时隙证据不完整，实际应发未知")
            else:
                row["frequency_excluded"] += raw_count - count
                if row["expected"] is not None:
                    row["expected"] += count
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
        raw_count = int(snapshot["expected_pages"]) if snapshot is not None else 0
        count = raw_count if snapshot is not None else None
        row["raw_targets"] += raw_count
        if snapshot is not None and raw_count and "fb_auto_template_version" in tables:
            frozen = {**due, "total_pages": raw_count,
                      "config_json": versions.get((due.get("template_id"), due.get("template_version")))}
            scope = json_value(snapshot.get("page_ids_json"), [])
            allowed = _quota(frozen, scope, schedule_plans)
            count = (len(allowed) if _has_frequency(frozen) else raw_count) if allowed is not None else None
        if count is None:
            row["expected"] = None
            row["warnings"].append(f"时隙 {due['id']} 未生成运行且无 Page 快照，预期条数未知")
        elif row["expected"] is not None:
            row["expected"] += count
        if count is not None:
            row["frequency_excluded"] += raw_count - count
        code = str(due.get("error_code") or "fb_report_no_run")
        # Known snapshots count Page posts; missing snapshots report slots separately.
        if count:
            row["pending" if due.get("status") in {"pending", "preparing"} else "blocked"] += count
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
        key = _source(run.get("trigger_type"))
        row = groups[key]
        status = str(task.get("status") or "unknown")
        allowed = quotas[run["id"]]
        if _has_frequency(run) and allowed is not None and str(task.get("page_id")) not in allowed:
            # Do not hide a real publication if the frozen plan is inconsistent.
            if status != "skipped":
                row["expected"] = None
                row["warnings"].append(f"运行 {run['id']} 存在频次外执行记录，计划与执行证据冲突")
            else:
                continue
        counts[run["id"]] += 1
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
            elif status == "skipped":
                if code in _POLICY_SKIPS:
                    row["policy_skipped"] += 1
                    policy_reasons[key][code] += 1
                    continue
                row["blocked"] += 1
                if code == "fb_auto_task_too_late":
                    blocked = any(h["id"] != task["id"] and parse_time(h.get("completed_at_utc")) is not None
                                  and completed is not None and parse_time(h.get("completed_at_utc")) <= completed
                                  for h in unknown_by_page.get(str(task.get("page_id")), []))
                    if task.get("prepared_at_utc") and blocked:
                        code = "fb_report_late_unknown_block"
                    elif not task.get("prepared_at_utc") and int(task.get("attempt_count") or 0) > 1:
                        code = "fb_report_prepare_retries_expired"
            else:
                row["failed"] += 1
                if task["id"] in fetch_failures and code in {"fb_graph_389", "fb_graph_190", "fb_graph_10", "fb_graph_102", "fb_graph_200"}:
                    code = "fb_graph_389"
        reasons[key][code] += 1

    for run_id, run in cohort_runs.items():
        key = _source(run.get("trigger_type"))
        expected = run_expected[run_id]
        gap = (expected - counts[run_id]) if expected is not None else 0
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
        for code, count in policy_reasons[key].most_common():
            reason, suggestion = _REASONS[code]
            row["policy_reasons"].append({"code": code, "reason": reason, "count": count,
                                           "suggestion": suggestion, "confidence": "confirmed"})
        row["warnings"] = list(dict.fromkeys(row["warnings"]))
    return {"channel": "FB", "accounting": "actual-fb-v2", "rows": list(groups.values()), "prior_completed": prior,
            "warnings": warnings, "evidence": evidence}
