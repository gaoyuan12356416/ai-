"""Read-only TT publication accounting. Never construct either live publisher store."""
from __future__ import annotations

import hashlib
import json
import random
import sqlite3
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

UTC = timezone.utc
BEIJING = timezone(timedelta(hours=8))
FORMS = {"random_overlay": "随机叠加", "direct_outro": "拼引导片尾"}
ACTIVE = {"selecting", "reserved", "preparing", "retry_wait", "ready", "publishing", "reconciling", "unknown"}
TERMINAL = {"failed", "canceled", "missed", "blocked_compliance", "no_candidate", "skipped", "preflight_failed"}


def _time(value):
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else None
    try:
        value = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return value.astimezone(UTC) if value.tzinfo else None
    except (TypeError, ValueError):
        return None


def _json(value, default):
    try:
        result = json.loads(value)
        return result if isinstance(result, type(default)) else default
    except (TypeError, ValueError):
        return default


def _db(path):
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=2000")
    deadline = time.monotonic() + 25
    conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 2000)
    conn.execute("BEGIN")  # One consistent snapshot per publisher database.
    return conn


def _rows(conn, table, where="", args=()):
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if table not in tables:
        return []
    return [dict(row) for row in conn.execute("SELECT * FROM " + table + where, args)]


def _require(conn, tables):
    present = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not set(tables).issubset(present):
        raise sqlite3.OperationalError("required publisher schema unavailable")


def _slot(day, value):
    try:
        return datetime.fromisoformat(day + "T" + str(value)).replace(tzinfo=BEIJING).astimezone(UTC)
    except ValueError:
        return None


def _within(value, start, end):
    value = _time(value)
    return value is not None and start <= value < end


def _row(source, label, form):
    return {"source": source, "label": label, "form": form, "expected": 0,
            "published": 0, "late": 0, "pending": 0, "unknown": 0, "failed": 0,
            "reasons": [], "warnings": [], "_reasons": Counter()}


def _reason(row, code, reason, suggestion, confidence="confirmed", count=1):
    row["_reasons"][(code, reason, suggestion, confidence)] += count


def _error_reason(row, task, *, confidence="confirmed"):
    code = str(task.get("error_code") or "")
    message = str(task.get("error_message") or "").lower()
    combined = (code + " " + message).lower()
    if "spam_risk_user_banned_from_posting" in combined:
        reason = "同账号前序任务遇到发布限制，可能阻塞后续任务" if confidence == "inferred" else "TikTok 明确限制该账号发布"
        _reason(row, "account_posting_banned", reason, "核查账号发布禁令并申诉；评估暂停该账号无效重试", confidence)
    elif any(word in combined for word in ("token", "unauthorized", "credential", "authorization")):
        _reason(row, "account_authorization", "账号授权或凭据校验未通过", "核查账号授权状态，必要时重新授权", confidence)
    elif any(word in combined for word in ("no_candidate", "no_eligible", "pool_language_empty", "pool_empty")) or task.get("status") == "no_candidate":
        _reason(row, "material_unavailable", "未找到符合账号语言或筛选条件的可用素材", "补充对应语言素材，核对素材筛选条件与去重范围", confidence)
    elif any(word in combined for word in ("rate_limit", "too_many", "429")):
        _reason(row, "platform_rate_limit", "平台限流导致发布延后", "检查限流持续时间并调整该账号发布间隔", confidence)
    elif any(word in combined for word in ("gpu", "media", "prepare", "download", "ffmpeg")):
        _reason(row, "media_preparation", "视频准备、下载或校验未完成", "检查对应制作任务与源视频可用性，修复后由现有流程恢复", confidence)
    elif task.get("status") in {"canceled", "skipped"}:
        _reason(row, "canceled_or_skipped", "计划已取消或跳过", "核对操作记录与当前发布安排", confidence)
    elif task.get("status") == "missed":
        _reason(row, "schedule_missed", "超过排期允许执行时间", "检查调度触发记录与执行积压", confidence)
    elif task.get("status") == "blocked_compliance":
        _reason(row, "publishing_gate", "发布授权或合规开关阻止执行", "核对账号发布设置及授权确认", confidence)
    elif code:
        # Do not copy upstream messages, URLs, or arbitrary codes into the group.
        _reason(row, "publish_error", "发布流程记录了错误，需查看对应任务", "根据任务证据定位失败阶段并处理", confidence)
    else:
        _reason(row, "pending_without_error", "任务尚未完成，未记录明确错误", "检查该账号前序任务、制作进度与调度日志", "unknown")


def _account(row, task, published_at, window, blockers=()):
    """Count a planned item once; success always requires reconciled platform identity."""
    status = str(task.get("status") or "pending")
    published_at = _time(published_at)
    if status == "published" and task.get("publish_id") and published_at:
        if window.start <= published_at < window.end:
            row["published"] += 1
            return
        if window.end <= published_at < window.cutoff:
            row["late"] += 1
            _reason(row, "completed_after_day_end", "昨日计划在今日才确认发布", "检查耗时阶段与排期积压", "confirmed")
            return
    if status in {"unknown", "reconciling"} or task.get("unknown_outcome") or status == "published":
        row["unknown"] += 1
        _reason(row, "outcome_unconfirmed", "平台发布结果或成功时间尚无法确认", "先核对平台结果与现有发布账本，避免重复发布", "unknown")
    elif status in TERMINAL:
        row["failed"] += 1
        _error_reason(row, task)
    else:
        row["pending"] += 1
        task_time = _time(task.get("scheduled_at_utc") or task.get("created_at"))
        blockers = [b for b in blockers if b.get("id") != task.get("id")
                    and b.get("account_id") == task.get("account_id") and b.get("status") in ACTIVE
                    and (_time(b.get("scheduled_at_utc")) or window.cutoff) <= (task_time or window.cutoff)]
        if status == "pending" and blockers:
            blocker = min(blockers, key=lambda b: _time(b.get("scheduled_at_utc")) or window.cutoff)
            _error_reason(row, blocker, confidence="inferred")
            note = "待处理原因参考读取时同账号更早的活动任务，属于当前阻塞推断。"
            if note not in row["warnings"]:
                row["warnings"].append(note)
        else:
            _error_reason(row, task)


def _missing(row, count, *, pool_empty=False):
    if count <= 0:
        return
    row["pending"] += count
    if pool_empty:
        _reason(row, "material_pool_empty_now", "已有排期但未生成发布任务；当前对应素材池为空", "补充账号语言对应的素材；若排期已停用则核对并关闭配置", "inferred", count)
        note = "素材不足依据读取时素材池状态推断；该流程素材为空时不会留下运行记录。"
        if note not in row["warnings"]:
            row["warnings"].append(note)
    else:
        _reason(row, "planned_task_missing", "已有计划位但没有对应发布任务", "核查该时刻调度、发布开关及任务创建前的错误日志", "unknown", count)


def _template_intervals(template, versions, events, window):
    """Template updates disable publishing; enabled events lock the version."""
    relevant = []
    for event in events:
        detail = _json(event.get("details_json"), {})
        if str(detail.get("template_id")) == str(template["id"]):
            timestamp = _time(event.get("created_at"))
            if timestamp and timestamp < window.end:
                relevant.append((timestamp, event.get("id", 0), event["event_type"], detail))
    relevant.sort(key=lambda event: (event[0], event[1]))
    intervals, active = [], None
    for timestamp, _, kind, detail in relevant:
        if kind not in {"template_enabled", "template_disabled", "template_updated"}:
            continue
        if active is not None:
            intervals.append((active[0], timestamp, active[1]))
            active = None
        if kind == "template_enabled":
            active = (timestamp, int(detail.get("version") or template["current_version"]))
    if active is not None:
        intervals.append((active[0], window.end, active[1]))
    if relevant:
        return intervals, True
    changed = _time(template.get("updated_at"))
    enabled = _time(template.get("enabled_at_utc"))
    if template.get("enabled") and enabled and (not changed or changed < window.start):
        return [(enabled, window.end, template["current_version"])], True
    if not template.get("enabled") and changed and changed < window.start:
        return [], True
    return [], False


def _auto(conn, window, output):
    _require(conn, {"tt_auto_template", "tt_auto_template_version", "tt_auto_run", "tt_auto_task", "tt_auto_random_plan", "tt_auto_event"})
    templates = _rows(conn, "tt_auto_template")
    version_rows = _rows(conn, "tt_auto_template_version")
    versions = {(v["template_id"], v["version"]): v for v in version_rows}
    events = _rows(conn, "tt_auto_event", " WHERE event_type IN ('template_enabled','template_disabled','template_updated')")
    plans = _rows(conn, "tt_auto_random_plan", " WHERE shanghai_date=?", (window.date,))
    plans = {(p["template_id"], p["template_version"]): _json(p["publish_times_json"], []) for p in plans}
    runs = _rows(conn, "tt_auto_run", " WHERE shanghai_date=?", (window.date,))
    tasks = _rows(conn, "tt_auto_task", " WHERE julianday(scheduled_at_utc)>=julianday(?) AND julianday(scheduled_at_utc)<julianday(?)", (window.start.isoformat(), window.end.isoformat()))
    blockers = _rows(conn, "tt_auto_task", " WHERE status IN ('selecting','reserved','preparing','retry_wait','ready','publishing','reconciling','unknown') AND julianday(scheduled_at_utc)<julianday(?)", (window.end.isoformat(),))
    groups = {}

    def group(template_id, version, manual=False):
        value = versions.get((template_id, version), {})
        config = _json(value.get("config_json"), {})
        form = FORMS.get(config.get("video_template"), "其他模板视频")
        source = ("auto_template_manual:" if manual else "auto_template:") + str(config.get("video_template") or "unspecified")
        if source not in groups:
            groups[source] = _row(source, ("手动执行模板·" if manual else "自动模板·") + form, form)
        return groups[source], config

    # A source/version change at the same slot must not manufacture a second post.
    expected = {}
    uncertain = set()
    for template in templates:
        intervals, reliable = _template_intervals(template, versions, events, window)
        if not reliable:
            row, _ = group(template["id"], template["current_version"])
            uncertain.add(row["source"])
            row["warnings"].append("模板启停历史不足，无法完整确认昨日预期；已存在任务仍逐条核对。")
        for begin, finish, version in intervals:
            if finish <= window.start or begin >= window.end:
                continue
            row, config = group(template["id"], version)
            schedule = config.get("schedule") or {}
            if schedule.get("mode") == "fixed":
                times = schedule.get("times") or []
            else:
                times = plans.get((template["id"], version))
                if times is None:
                    frozen = versions.get((template["id"], version), {})
                    if not frozen.get("config_sha256"):
                        uncertain.add(row["source"])
                        row["warnings"].append("随机日计划及配置摘要缺失，无法完整还原预期。")
                        continue
                    seed = f"{template['id']}|{version}|{frozen['config_sha256']}|{window.date}"
                    generator = random.Random(int.from_bytes(hashlib.sha256(seed.encode()).digest()[:16], "big"))
                    count = min(max(int(schedule.get("daily_count") or 0), 0), 1440)
                    times = ["%02d:%02d" % divmod(m, 60) for m in sorted(generator.sample(range(1440), count))]
            for value in times:
                instant = _slot(window.date, value)
                if instant is None or not (window.start <= instant < window.end and begin <= instant < finish):
                    continue
                for account_id in config.get("account_ids") or []:
                    expected[(template["id"], instant, str(account_id))] = row

    run_map = {r["id"]: r for r in runs}
    handled = set()
    manual_count = Counter()
    for task in tasks:
        run = run_map.get(task["run_id"], {})
        manual = run.get("trigger_type") == "manual"
        row, _ = group(task["template_id"], task["template_version"], manual)
        if manual:
            key = ("manual", task["id"])
            manual_count[row["source"]] += 1
        else:
            key = (task["template_id"], _time(task.get("scheduled_at_utc")), str(task["account_id"]))
            # The frozen run is authoritative for a task already created ahead.
            expected[key] = row
        if key in handled:
            row["warnings"].append("检测到重复账号计划位，按唯一计划位计数。")
            continue
        handled.add(key)
        _account(row, task, task.get("published_at_utc"), window, blockers)
    # A run can exist after preflight but before all per-account tasks were inserted.
    for run in runs:
        row, config = group(run["template_id"], run["template_version"], run.get("trigger_type") == "manual")
        for account_id in config.get("account_ids") or []:
            if run.get("trigger_type") == "manual":
                if not any(t["run_id"] == run["id"] and str(t["account_id"]) == str(account_id) for t in tasks):
                    manual_count[row["source"]] += 1
                    _missing(row, 1)
            else:
                expected[(run["template_id"], _time(run["scheduled_at_utc"]), str(account_id))] = row
    for key, row in expected.items():
        row["expected"] += 1
        if key not in handled:
            _missing(row, 1)
    for source, count in manual_count.items():
        groups[source]["expected"] += count
    for source in uncertain:
        groups[source]["expected"] = None
    prior = _rows(conn, "tt_auto_task", " WHERE status='published' AND publish_id<>'' AND julianday(scheduled_at_utc)<julianday(?) AND julianday(published_at_utc)>=julianday(?) AND julianday(published_at_utc)<julianday(?)", (window.start.isoformat(), window.start.isoformat(), window.end.isoformat()))
    output["prior_completed"] += len(prior)
    output["rows"].extend(groups.values())
    output["evidence"].append({"source": "tt_auto", "tables": "tt_auto_template_version,tt_auto_random_plan,tt_auto_run,tt_auto_task,tt_auto_event", "date": window.date, "run_count": len(runs), "task_count": len(tasks)})


def _legacy(conn, window, output):
    _require(conn, {"tt_post_daily_schedule", "tt_post_random_daily_plan", "tt_post_schedule_run", "tt_post_queue", "tt_post_event", "tt_post_direct_test"})
    recurring = _row("manual_material_schedule", "人工素材·每日排期", "素材视频")
    manual = _row("manual_material_once", "人工素材·立即/单次发布", "素材视频")
    queue = _rows(conn, "tt_post_queue", " WHERE julianday(scheduled_at_utc)<julianday(?)", (window.end.isoformat(),))
    runs = _rows(conn, "tt_post_schedule_run", " WHERE shanghai_date=?", (window.date,))
    run_by_queue = {r["queue_id"]: r for r in runs if r.get("queue_id") is not None}
    plans = _rows(conn, "tt_post_random_daily_plan", " WHERE shanghai_date=?", (window.date,))
    schedules = _rows(conn, "tt_post_daily_schedule")
    audit = _rows(conn, "tt_post_daily_schedule_audit")
    events = _rows(conn, "tt_post_event", " WHERE to_status='published' AND event_type='publish_reconciled'")
    success = {}
    for event in events:
        stamp = _time(event.get("created_at"))
        if stamp and event.get("queue_id") is not None:
            success[event["queue_id"]] = min(stamp, success.get(event["queue_id"], stamp))
    available = _rows(conn, "tt_post_recurring_pool", " WHERE status='available'")
    settings = {str(s["account_id"]): s for s in _rows(conn, "tt_post_account_setting")}
    expected = set()
    unknown_history = False
    schedule_map = {str(s["account_id"]): s for s in schedules}
    planned_accounts = {str(p["account_id"]) for p in plans}
    periods_by_account = {}
    for schedule in schedules:
        account_id = str(schedule["account_id"])
        history = []
        for item in audit:
            snapshot = _json(item.get("snapshot_json"), {})
            if str(snapshot.get("account_id", item.get("account_id"))) == account_id:
                timestamp = _time(item.get("created_at"))
                if timestamp and timestamp < window.end:
                    history.append((timestamp, snapshot))
        history.sort(key=lambda item: item[0])
        if history:
            if history[0][0] > window.start:
                unknown_history = True
            periods = [(stamp, history[index + 1][0] if index + 1 < len(history) else window.end, snapshot)
                       for index, (stamp, snapshot) in enumerate(history)]
        else:
            changed = _time(schedule.get("updated_at"))
            if changed and changed >= window.start:
                unknown_history = True
                continue
            periods = [(window.start, window.end, schedule)]
        periods_by_account[account_id] = periods
        for begin, finish, config in periods:
            if not config.get("enabled"):
                continue
            if config.get("schedule_mode") == "random":
                if account_id not in planned_accounts:
                    unknown_history = True
                continue
            for value in _json(config.get("publish_times_json"), []):
                stamp = _slot(window.date, value)
                if stamp and window.start <= stamp < window.end and begin <= stamp < finish:
                    expected.add((account_id, stamp))
    for plan in plans:
        account_id = str(plan["account_id"])
        periods = periods_by_account.get(account_id)
        if not periods:
            unknown_history = True
        for value in _json(plan.get("publish_times_json"), []):
            stamp = _slot(window.date, value)
            if stamp is None or not (window.start <= stamp < window.end):
                continue
            # Persisted plans survive changes; only count enabled intervals.
            active = [config for begin, finish, config in (periods or []) if begin <= stamp < finish]
            if active:
                config = active[-1]
                if not config.get("enabled") or config.get("schedule_mode") != "random":
                    continue
                if config.get("version") != plan.get("config_version"):
                    unknown_history = True
            elif periods:
                continue
            expected.add((account_id, stamp))
    handled = set()
    for task in queue:
        stamp = _time(task.get("scheduled_at_utc"))
        if stamp is None:
            continue
        published_at = success.get(task["id"])
        if stamp < window.start:
            if task.get("status") == "published" and task.get("publish_id") and published_at and window.start <= published_at < window.end:
                output["prior_completed"] += 1
            continue
        run = run_by_queue.get(task["id"])
        # A queue without a recurring run is an operator one-time schedule.
        target = recurring if run and run.get("trigger_type") == "auto" else manual
        key = (str(task["account_id"]), stamp)
        if target is recurring:
            expected.add(key)
            handled.add(key)
        else:
            target["expected"] += 1
        _account(target, task, published_at, window)
    queue_ids = {q["id"] for q in queue}
    for run in runs:
        if run.get("queue_id") in queue_ids:
            continue
        key = (str(run["account_id"]), _time(run.get("scheduled_at_utc")))
        target = recurring if run.get("trigger_type") == "auto" else manual
        if target is recurring:
            expected.add(key)
            if key in handled:
                continue
            handled.add(key)
        else:
            target["expected"] += 1
        _account(target, run, None, window)
    for account_id, stamp in expected - handled:
        language = settings.get(account_id, {}).get("drama_language")
        eligible_pool = [p for p in available if (p.get("routing_language") == language if language else str(p.get("account_id")) == account_id)]
        _missing(recurring, 1, pool_empty=not eligible_pool)
    recurring["expected"] = None if unknown_history else len(expected)
    if unknown_history:
        recurring["warnings"].append("每日排期历史或随机日计划不完整，昨日预期无法完整确认；不以今日配置或已生成任务数代替历史预期。")
    direct = _rows(conn, "tt_post_direct_test", " WHERE julianday(created_at)<julianday(?)", (window.end.isoformat(),))
    for task in direct:
        if _within(task.get("created_at"), window.start, window.end):
            manual["expected"] += 1
            _account(manual, task, task.get("published_at_utc"), window)
        elif task.get("status") == "published" and task.get("publish_id") and _within(task.get("published_at_utc"), window.start, window.end):
            output["prior_completed"] += 1
    output["rows"].extend([recurring, manual])
    output["evidence"].append({"source": "tt", "tables": "tt_post_random_daily_plan,tt_post_schedule_run,tt_post_queue,tt_post_event,tt_post_direct_test", "date": window.date, "plan_accounts": len(plans), "run_count": len(runs)})


def collect(paths: dict, window) -> dict:
    """Report yesterday's plan cohort and confirmed results at a bounded cutoff."""
    output = {"channel": "TT", "rows": [], "prior_completed": 0, "warnings": [], "evidence": []}
    for key, collector, label in (("tt_auto", _auto, "自动模板"), ("tt", _legacy, "人工素材")):
        conn = None
        try:
            conn = _db(paths[key])
            collector(conn, window, output)
        except (OSError, sqlite3.Error, KeyError, TypeError, ValueError) as exc:
            output["warnings"].append(f"TT {label} 数据读取不完整（{type(exc).__name__}），不可视为零发布。")
            missing = _row("unavailable:" + key, label + "·数据不可用", "未知")
            missing["expected"] = None
            output["rows"].append(missing)
        finally:
            if conn is not None:
                conn.close()
    for row in output["rows"]:
        reasons = row.pop("_reasons")
        row["reasons"] = [{"code": code, "reason": reason, "count": count,
                           "suggestion": suggestion, "confidence": confidence}
                          for (code, reason, suggestion, confidence), count in sorted(reasons.items())]
        row["warnings"] = list(dict.fromkeys(row["warnings"]))
    return output
