"""Read-only X plan/target-delivery reconciliation, with no publisher imports."""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from .common import in_window, json_value, parse_time, read_db


BJ = timezone(timedelta(hours=8))
LABELS = {
    "material_pool": "素材池自动排期", "drama_pool": "短剧池自动排期",
    "auto_template": "X Auto 模板", "manual_immediate": "手动立即发布",
    "manual_scheduled": "手动一次定时", "legacy_daily": "历史每日排期",
    "catchup": "人工补充批次", "unclassified": "来源待核对",
}
ERRORS = {
    "x_post_pool_fifo_conflict": ("素材池最新可用记录与冻结计划冲突", "核查候选快照、素材占用与顺序校验；确认原因后再决定补发"),
    "x_auto_no_eligible_material": ("模板未找到符合条件的可用素材", "检查同语言素材、上线时间、历史占用及模板筛选阈值"),
    "x_post_schedule_partial_capacity": ("该时点部分配置账号未匹配到可用内容", "补充对应语言的可用素材或短剧，并核查账号与内容绑定"),
    "x_post_schedule_material_preflight_shortage": ("可通过预检的素材不足", "检查候选素材、下载和媒体校验记录"),
    "x_post_schedule_drama_shortage": ("可用于配置账号的短剧不足", "补充同语言可用短剧并检查未完成短剧绑定"),
    "x_post_premium_relay_unavailable": ("缺少同语言可用 Premium 中继账号", "检查同语言 Premium 账号资格和授权"),
    "x_long_video_requires_premium": ("长视频缺少可用 Premium 发布能力", "核查目标账号及同语言中继账号的会员资格"),
    "x_post_account_locked": ("X 账号临时锁定", "登录对应 X 账号解锁后核对历史结果"),
    "x_account_not_publishable": ("账号未通过发布资格检查", "检查授权状态、发布开关与账号身份"),
    "x_token_invalid": ("账号授权不可用", "重新授权对应账号后再评估补发"),
    "media_download_failed": ("素材下载失败", "检查源文件可访问性及媒体下载服务"),
    "invalid_media_dimensions": ("视频尺寸未通过校验", "检查媒体修复结果及视频尺寸"),
    "invalid_media_codec": ("视频编码未通过校验", "检查媒体修复结果及视频编码"),
    "unknown_outcome": ("上游发布结果尚未确认", "先只读核对 X 平台与发布台账，避免重复发布"),
    "relay_pending": ("中继原帖已发布，目标账号转发尚未确认", "核对目标 Repost 状态及中继台账"),
    "schedule_not_created": ("计划时点没有对应运行记录", "检查定时器、执行门禁与该时点日志"),
    "task_not_created": ("冻结计划中的账号未生成执行任务", "检查任务创建阶段及账号预检日志"),
    "execution_pending": ("任务仍未完成", "检查当前队列、媒体准备与执行服务状态"),
    "publish_failed": ("发布未完成，需核对具体错误", "检查对应批次和发布日志"),
}


def _rows(db, table, fields=None):
    columns = [r[1] for r in db.execute('PRAGMA table_info("%s")' % table)]
    if not columns:
        return []
    names = [n for n in (fields or columns) if n in columns]
    return [dict(r) for r in db.execute('SELECT %s FROM "%s"' % (','.join('"%s"' % n for n in names), table))]


def _accounts(value):
    return list(dict.fromkeys(str(x) for x in json_value(value, []) if str(x)))


def _day(value):
    parsed = parse_time(value)
    return parsed.astimezone(BJ).date().isoformat() if parsed else ""


def _slot(day, time):
    try:
        return datetime.fromisoformat(day + "T" + time).replace(tzinfo=BJ).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _row(source):
    return {"source": source, "label": LABELS[source], "form": "直接发布／目标转发" if source.endswith("_pool") else "直接发布",
            "expected": 0, "published": 0, "late": 0, "pending": 0, "unknown": 0, "failed": 0,
            "reasons": [], "warnings": [], "form_counts": {}}


def _reason(row, code, count=1, confidence="confirmed"):
    if count <= 0:
        return
    text, suggestion = ERRORS.get(code, ("发布未完成，需核对批次错误记录", "检查对应批次的账号、媒体和平台返回记录"))
    for item in row["reasons"]:
        if item["code"] == code and item["confidence"] == confidence:
            item["count"] += count
            return
    row["reasons"].append({"code": code, "reason": text, "suggestion": suggestion, "count": count, "confidence": confidence})


def _delivery(queue, log, relay):
    form = "目标转发" if queue.get("delivery_mode") == "premium_relay_repost" or relay else "直接发布"
    if log.get("unknown_outcome") or relay.get("unknown_outcome"):
        return "unknown", None, form, "unknown_outcome"
    if log.get("status") == "published":
        if form == "目标转发":
            if relay.get("status") == "reposted" and relay.get("source_post_id"):
                stamp = parse_time(relay.get("reposted_at"))
                if stamp:
                    return "published", stamp, form, ""
        elif log.get("x_post_id") and parse_time(log.get("published_at")):
            return "published", parse_time(log["published_at"]), form, ""
        return "unknown", None, form, "unknown_outcome"
    if log.get("status") in {"post_creating", "repost_creating", "needs_review", "unknown_outcome"} or queue.get("status") == "needs_review":
        return "unknown", None, form, "unknown_outcome"
    if log.get("status") == "failed" or queue.get("status") in {"failed", "stopped", "cancelled"} or relay.get("status") == "failed":
        return "failed", None, form, relay.get("error_code") or log.get("error_code") or "publish_failed"
    code = "relay_pending" if relay.get("source_post_id") or log.get("status") == "source_published" else "execution_pending"
    return "pending", None, form, code


def _record(row, outcome, window):
    state, stamp, form, code = outcome
    if state == "published":
        if stamp and stamp < window.end:
            row["published"] += 1
            row["form_counts"][form] = row["form_counts"].get(form, 0) + 1
        elif in_window(stamp, window.end, window.cutoff):
            row["late"] += 1
        else:
            row["pending"] += 1
            _reason(row, "execution_pending")
        return
    row[state] += 1
    _reason(row, code or "publish_failed")


def _best(outcomes):
    published = [x for x in outcomes if x[0] == "published"]
    if published:
        return min(published, key=lambda x: x[1])
    for state in ("unknown", "pending", "failed"):
        for value in outcomes:
            if value[0] == state:
                return value
    return "pending", None, "直接发布", "execution_pending"


def _missing(row, run, count):
    if count <= 0:
        return
    code = run.get("error_code") or ""
    if run.get("status") in {"failed_preflight", "failed", "stopped", "completed_with_errors"}:
        row["failed"] += count
        _reason(row, code or "publish_failed", count)
    elif run.get("status") == "completed":
        row["failed"] += count
        _reason(row, "x_post_schedule_partial_capacity", count)
    else:
        row["pending"] += count
        _reason(row, "task_not_created" if run else "schedule_not_created", count)


def _root_ids(db):
    parents = {}
    for table in ("x_post_schedule_codefix_compensation_audit", "x_post_schedule_drama_scope_compensation_audit"):
        for item in _rows(db, table, ["original_schedule_run_id", "compensation_schedule_run_id"]):
            parents[item["compensation_schedule_run_id"]] = item["original_schedule_run_id"]
    for item in _rows(db, "x_post_operator_gap_recovery_audit", ["identity", "evidence_json"]):
        evidence = json_value(item.get("evidence_json"), {})
        if evidence.get("child_run_id") and item.get("identity"):
            parents[evidence["child_run_id"]] = item["identity"]
    def root(value):
        seen = set()
        while value in parents and value not in seen:
            seen.add(value)
            value = parents[value]
        return value
    return root


def _pool_plans(db, window, runs, row_by_source):
    plans = {}
    available_sources = set()
    for plan in _rows(db, "x_post_schedule_random_plan", ["source_type", "run_date", "account_ids_json", "publish_times_json"]):
        if plan.get("run_date") != window.date:
            continue
        source = plan.get("source_type")
        if source not in {"material", "drama"}:
            continue
        available_sources.add(source)
        for time in json_value(plan.get("publish_times_json"), []):
            plans[(source, time)] = _accounts(plan.get("account_ids_json"))

    # New deployments retain config snapshots. Historical fixed settings are only
    # usable when an audit interval covers the slot; current config is not history.
    audits = _rows(db, "x_post_schedule_config_audit", ["source_type", "config_version", "snapshot_json", "created_at"])
    for source in ("material", "drama"):
        ordered = sorted([a for a in audits if a.get("source_type") == source and parse_time(a.get("created_at"))], key=lambda a: parse_time(a["created_at"]))
        audit_covers_day = bool(ordered and parse_time(ordered[0]["created_at"]) <= window.start)
        missing_random = False
        for index, audit in enumerate(ordered):
            config = json_value(audit.get("snapshot_json"), {})
            began = parse_time(audit["created_at"])
            until = parse_time(ordered[index + 1]["created_at"]) if index + 1 < len(ordered) else window.cutoff
            if config.get("enabled") and config.get("schedule_mode") == "random" and began < window.end and until > window.start and source not in available_sources:
                missing_random = True
            if not config.get("enabled") or config.get("schedule_mode", "fixed") != "fixed":
                continue
            for time in json_value(config.get("publish_times_json"), []):
                scheduled = _slot(window.date, time)
                if scheduled and began <= scheduled < until:
                    plans[(source, time)] = _accounts(config.get("account_ids_json"))
        if audit_covers_day and not missing_random:
            available_sources.add(source)

    for run in runs.values():
        if run.get("run_date") == window.date and run.get("source_type") in {"material", "drama"}:
            key = (run["source_type"], run.get("publish_time", ""))
            plans.setdefault(key, _accounts(run.get("account_ids_json")))
    for source in ("material", "drama"):
        if source not in available_sources:
            row = row_by_source[source + "_pool"]
            row["expected"] = None
            row["warnings"].append("缺少覆盖昨日的冻结排期或配置审计；仅统计已记录任务，原计划总数待核对")
    return plans


def _collect_primary(db, window, output, row_by_source):
    if not list(db.execute('PRAGMA table_info(x_post_queue)')):
        raise sqlite3.OperationalError("missing X target ledger")
    root = _root_ids(db)
    all_runs = {r["id"]: r for r in _rows(db, "x_post_schedule_run", ["id", "source_type", "run_date", "publish_time", "account_ids_json", "status", "error_code", "schedule_mode"])}
    runs = {key: r for key, r in all_runs.items() if root(key) == key}
    manuals = {r["id"]: r for r in _rows(db, "x_post_manual_run", ["id", "trigger_source", "run_date", "created_at", "publish_mode", "scheduled_at", "account_ids_json", "expected_count", "status", "error_code"])}
    logs = {r["queue_id"]: r for r in _rows(db, "x_post_publish_log", ["id", "queue_id", "status", "x_post_id", "published_at", "unknown_outcome", "error_code"])}
    relays = {r["queue_id"]: r for r in _rows(db, "x_post_repost_ledger", ["queue_id", "status", "source_post_id", "reposted_at", "unknown_outcome", "error_code"])}
    queues = _rows(db, "x_post_queue", ["id", "account_id", "run_date", "source_type", "status", "delivery_mode", "schedule_run_id", "manual_run_id", "run_id", "catchup_run_id"])
    by_schedule, by_manual, by_daily, by_catchup = (defaultdict(list) for _ in range(4))
    queue_outcomes, counted_prior = {}, set()
    for queue in queues:
        outcome = _delivery(queue, logs.get(queue["id"], {}), relays.get(queue["id"], {}))
        queue_outcomes[queue["id"]] = outcome
        manual = manuals.get(queue.get("manual_run_id"), {})
        plan_date = queue.get("run_date", "")
        if manual:
            plan_date = _manual_date(manual)
        if queue.get("schedule_run_id"):
            parent = root(queue["schedule_run_id"])
            plan_date = all_runs.get(parent, {}).get("run_date", plan_date)
            by_schedule[parent].append(queue)
        elif queue.get("manual_run_id"):
            by_manual[queue["manual_run_id"]].append(queue)
        elif queue.get("catchup_run_id"):
            by_catchup[queue["catchup_run_id"]].append(queue)
        elif queue.get("run_id"):
            by_daily[queue["run_id"]].append(queue)
        elif plan_date == window.date:
            row = row_by_source.setdefault("unclassified", _row("unclassified"))
            row["expected"] = None
            _record(row, outcome, window)
        if plan_date and plan_date < window.date and outcome[0] == "published" and in_window(outcome[1], window.start, window.end):
            output["prior_completed"] += 1
            counted_prior.add(queue["id"])

    plans = _pool_plans(db, window, runs, row_by_source)
    by_slot = {(r["source_type"], r.get("publish_time", "")): r for r in runs.values() if r.get("run_date") == window.date}
    for (source, time), accounts in plans.items():
        row = row_by_source[source + "_pool"]
        if row["expected"] is not None:
            row["expected"] += len(accounts)
        run = by_slot.get((source, time), {})
        by_account = defaultdict(list)
        for queue in by_schedule.get(run.get("id"), []):
            by_account[str(queue["account_id"])].append(queue_outcomes[queue["id"]])
        for account in accounts:
            if by_account.get(account):
                _record(row, _best(by_account[account]), window)
            else:
                _missing(row, run, 1)
        extra = set(by_account) - set(accounts)
        if extra:
            row["warnings"].append("存在冻结计划以外的目标账号，请核对补偿批次")
            for account in extra:
                _record(row, _best(by_account[account]), window)

    for run in manuals.values():
        if run.get("trigger_source", "manual") != "manual" or _manual_date(run) != window.date:
            continue
        source = "manual_scheduled" if run.get("publish_mode") == "scheduled" else "manual_immediate"
        row = row_by_source[source]
        accounts = _accounts(run.get("account_ids_json"))
        row["expected"] += len(accounts) or int(run.get("expected_count") or 0)
        rows = by_manual.get(run["id"], [])
        for queue in rows:
            _record(row, queue_outcomes[queue["id"]], window)
        _missing(row, run, max(0, (len(accounts) or int(run.get("expected_count") or 0)) - len(rows)))

    for table, source, grouped in (("x_post_daily_run", "legacy_daily", by_daily), ("x_post_catchup_run", "catchup", by_catchup)):
        for run in _rows(db, table, ["id", "run_date", "expected_count", "account_ids_json", "status", "error_code"]):
            if run.get("run_date") != window.date:
                continue
            row = row_by_source.setdefault(source, _row(source))
            expected = len(_accounts(run.get("account_ids_json"))) or int(run.get("expected_count") or 0)
            row["expected"] += expected
            rows = grouped.get(run["id"], [])
            for queue in rows:
                _record(row, queue_outcomes[queue["id"]], window)
            _missing(row, run, max(0, expected - len(rows)))
    output["evidence"]["x_schedule_run_ids"] = sorted(r["id"] for r in runs.values() if r.get("run_date") == window.date)
    output["evidence"]["x_manual_run_ids"] = sorted(r["id"] for r in manuals.values() if _manual_date(r) == window.date)
    return queue_outcomes, counted_prior


def _manual_date(run):
    if run.get("publish_mode") == "scheduled":
        return _day(run.get("scheduled_at"))
    return _day(run.get("created_at")) or run.get("run_date", "")


def _auto_plans(db, window, runs, versions, row):
    plans = {}
    for run in runs.values():
        if run.get("shanghai_date") == window.date:
            config = versions.get((run["template_id"], run["template_version"]))
            if config is None:
                row["expected"] = None
                row["warnings"].append("X Auto 批次缺少冻结模板版本，原计划总数待核对")
                config = {}
            plans[(run["template_id"], run.get("publish_time"), run.get("trigger_type", "auto"), run.get("id") if run.get("trigger_type") == "manual" else None)] = (run, _accounts(config.get("account_ids")))
    events = _rows(db, "x_auto_event", ["event_type", "details_json", "created_at"])
    history = defaultdict(list)
    for event in events:
        if event.get("event_type", "").startswith("template_") and parse_time(event.get("created_at")):
            detail = json_value(event.get("details_json"), {})
            if detail.get("template_id"):
                history[detail["template_id"]].append((parse_time(event["created_at"]), event["event_type"], detail))
    templates = _rows(db, "x_auto_template", ["id", "enabled", "created_at"])
    for template in templates:
        if not history.get(template["id"]) and (not parse_time(template.get("created_at")) or parse_time(template["created_at"]) < window.end):
            row["expected"] = None
            row["warnings"].append("X Auto 缺少模板启停历史，不能排除未建运行的计划时点")
    random_plans = {(r["template_id"], r["template_version"]): json_value(r.get("publish_times_json"), []) for r in _rows(db, "x_auto_random_plan", ["template_id", "template_version", "shanghai_date", "publish_times_json"]) if r.get("shanghai_date") == window.date}
    for template_id, events in history.items():
        events.sort(key=lambda item: item[0])
        for index, (began, kind, detail) in enumerate(events):
            if kind != "template_enabled":
                continue
            until = events[index + 1][0] if index + 1 < len(events) else window.cutoff
            version = detail.get("version")
            config = versions.get((template_id, version), {})
            schedule = config.get("schedule", {})
            times = schedule.get("times", []) if schedule.get("mode") == "fixed" else random_plans.get((template_id, version))
            if times is None and began < window.end and until > window.start:
                row["expected"] = None
                row["warnings"].append("X Auto 随机计划缺失，原计划总数待核对")
            for time in times or []:
                stamp = _slot(window.date, time)
                if stamp and began <= stamp < until:
                    key = (template_id, time, "auto", None)
                    plans.setdefault(key, ({}, _accounts(config.get("account_ids"))))
    return plans


def _collect_auto(db, window, output, row, queue_outcomes, counted_prior):
    if not list(db.execute('PRAGMA table_info(x_auto_run)')):
        raise sqlite3.OperationalError("missing X Auto run ledger")
    runs = {r["id"]: r for r in _rows(db, "x_auto_run", ["id", "template_id", "template_version", "trigger_type", "shanghai_date", "publish_time", "status", "error_code"])}
    versions = {(r["template_id"], r["version"]): json_value(r.get("config_json"), {}) for r in _rows(db, "x_auto_template_version", ["template_id", "version", "config_json"])}
    tasks_by_run = defaultdict(list)
    for task in _rows(db, "x_auto_task", ["id", "run_id", "account_id", "status", "execution_queue_id", "publish_id", "published_at_utc", "unknown_outcome", "error_code"]):
        tasks_by_run[task["run_id"]].append(task)
    def outcome(task):
        queue_id = task.get("execution_queue_id")
        if queue_id in queue_outcomes:
            return queue_outcomes[queue_id]
        if task.get("unknown_outcome") or task.get("status") in {"needs_review", "unknown_outcome", "publishing"}:
            return "unknown", None, "直接发布", "unknown_outcome"
        if task.get("status") == "published" and task.get("publish_id") and parse_time(task.get("published_at_utc")):
            return "published", parse_time(task["published_at_utc"]), "直接发布", ""
        if task.get("status") in {"failed", "no_candidate", "cancelled", "skipped"}:
            return "failed", None, "直接发布", task.get("error_code") or "publish_failed"
        return "pending", None, "直接发布", "execution_pending"
    for run_id, tasks in tasks_by_run.items():
        if runs.get(run_id, {}).get("shanghai_date", "") < window.date:
            for task in tasks:
                result = outcome(task)
                if result[0] == "published" and in_window(result[1], window.start, window.end) and task.get("execution_queue_id") not in counted_prior:
                    output["prior_completed"] += 1
    plans = _auto_plans(db, window, runs, versions, row)
    for run, accounts in plans.values():
        tasks = {str(t["account_id"]): t for t in tasks_by_run.get(run.get("id"), [])}
        if row["expected"] is not None:
            row["expected"] += len(accounts)
        for account in accounts:
            if account in tasks:
                _record(row, outcome(tasks[account]), window)
            else:
                _missing(row, run, 1)
        for account in set(tasks) - set(accounts):
            _record(row, outcome(tasks[account]), window)
            row["warnings"].append("执行账号不在可读取的冻结模板中，请核对计划记录")
    output["evidence"]["x_auto_run_ids"] = sorted(r["id"] for r in runs.values() if r.get("shanghai_date") == window.date)


def collect(paths: dict, window) -> dict:
    """Report plan-cohort final target deliveries and separate prior-plan arrivals."""
    output = {"channel": "X", "rows": [], "prior_completed": 0, "warnings": [], "evidence": {}}
    row_by_source = {source: _row(source) for source in ("material_pool", "drama_pool", "auto_template", "manual_immediate", "manual_scheduled")}
    queue_outcomes, counted_prior = {}, set()
    try:
        with read_db(paths["x"]) as db:
            queue_outcomes, counted_prior = _collect_primary(db, window, output, row_by_source)
    except (OSError, sqlite3.Error, KeyError) as exc:
        output["warnings"].append("X 主发布台账读取失败：" + type(exc).__name__)
        for source in ("material_pool", "drama_pool", "manual_immediate", "manual_scheduled"):
            row_by_source[source]["expected"] = None
    try:
        with read_db(paths["x_auto"]) as db:
            _collect_auto(db, window, output, row_by_source["auto_template"], queue_outcomes, counted_prior)
    except (OSError, sqlite3.Error, KeyError) as exc:
        row_by_source["auto_template"]["expected"] = None
        row_by_source["auto_template"]["warnings"].append("X Auto 台账读取失败：" + type(exc).__name__)
    for row in row_by_source.values():
        row["warnings"] = list(dict.fromkeys(row["warnings"]))
        row["reasons"].sort(key=lambda item: (-item["count"], item["code"]))
    output["rows"] = list(row_by_source.values())
    return output
