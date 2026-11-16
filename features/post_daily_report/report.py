from __future__ import annotations

import html
import importlib
import json
import re
from datetime import datetime, timezone


def collect_report(paths, window):
    from .accounting import load_dispatch, backlog
    paths = dict(paths)
    paths['_accounting'] = load_dispatch(paths, window.cutoff)
    channels = []
    for name in ("tt", "fb", "x"):
        try:
            module = importlib.import_module("features.post_daily_report." + name)
            result = module.collect(paths, window)
            validate_channel(result)
        except Exception as exc:
            # Do not include exception text: a failed config/driver can expose secrets.
            result = {"channel": name.upper(), "rows": [], "prior_completed": 0,
                      "warnings": ["数据读取失败（%s），本渠道数量未纳入合计。" % type(exc).__name__],
                      "error": type(exc).__name__}
        channels.append(result)
    # Historical report replay must not manufacture an old queue snapshot from
    # today's mutable projections. Cohort delivery cutoff remains unchanged.
    pending = backlog(paths, cutoff=datetime.now(timezone.utc), channels=('tt', 'fb', 'x'))
    return {"schema_version": 2, "date": window.date, "backlog": pending,
            "cutoff_utc": window.cutoff.isoformat(),
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "scope": "AI后台全部Post；不含旧版独立系统及平台端自行发帖", "channels": channels}


def validate_channel(channel):
    for row in channel.get("rows", []):
        for field in ("published", "late", "pending", "unknown", "failed", "blocked", "policy_skipped", "frequency_excluded", "superseded_slots"):
            value = row.get(field, 0)
            if not isinstance(value, int) or value < 0:
                raise ValueError("invalid report counter")
        expected = row.get("expected")
        if expected is not None and (not isinstance(expected, int) or expected < 0):
            raise ValueError("invalid expected counter")
        if expected is not None and row.get("published", 0) + row.get("late", 0) > expected:
            raise ValueError("completion exceeds planned targets")
        if channel.get("accounting") == "actual-fb-v2" and expected is not None:
            if sum(row.get(k, 0) for k in ("published", "late", "pending", "unknown", "failed", "blocked", "policy_skipped")) != expected:
                raise ValueError("FB outcome counters do not reconcile with actual targets")


def total(rows):
    available = all(r.get("data_available", True) for r in rows)
    known = available and all(r.get("expected") is not None for r in rows)
    expected = sum(r.get("expected") or 0 for r in rows)
    published = sum(r.get("published", 0) for r in rows)
    late = sum(r.get("late", 0) for r in rows)
    return {"expected": expected if known else None, "known_expected": expected,
            "published": published if available else None, "late": late if available else None,
            "missing": expected - published - late - sum(r.get('policy_skipped', 0) for r in rows) if known else None}


def _n(value):
    return "未知" if value is None else str(value)


def _md(text):
    return {"tag": "div", "text": {"tag": "lark_md", "content": text}}


def _cost_text(channel):
    cost = channel.get("cost")
    if not cost:
        return ""
    counts = cost.get("counts", {})
    amount = cost.get("display_usd")
    title = "统计日 X 发布 API 成本（估算）：**US$%s**" % amount if amount is not None else "统计日 X 发布 API 成本：**未知**（已核实操作估算 US$%s）" % cost.get("known_estimated_usd", "0")
    text = title + "\n带链接原帖 %s · 无链接原帖 %s · 目标转发 %s。" % tuple(counts.get(k, 0) for k in ("post_with_url", "post_without_url", "repost"))
    text += "\n按实际发布日计费，包含往期计划当日完成；次日补发计入次日报。"
    text += "\n" + cost.get("note", "")
    if cost.get("unpriced_posts") or cost.get("unconfirmed_attempts"):
        text += "\n另有缺少正文的原帖 %s 条、结果不明请求 %s 条待账单核对。" % (cost.get("unpriced_posts", 0), cost.get("unconfirmed_attempts", 0))
    return text


def _columns(values, header=False):
    columns = []
    for i, value in enumerate(values):
        # Source labels are literal text. The other cells contain fixed headers or counts.
        cell = ({"tag": "div", "text": {"tag": "plain_text", "content": str(value)}} if i == 0
                else {"tag": "markdown", "content": str(value)})
        columns.append({"tag": "column", "width": "weighted", "weight": 3 if i == 0 else 1, "elements": [cell]})
    row = {"tag": "column_set", "flex_mode": "none", "columns": columns}
    if header:
        row["background_style"] = "grey"
    return row


def _source_table(headers, entries, compact):
    if compact and entries:
        keys = ["c%d" % i for i in range(len(headers))]
        columns = [{"name": key, "display_name": label, "data_type": "text"} for key, label in zip(keys, headers)]
        rows = [dict(zip(keys, map(str, values))) for values, _ in entries]
        elements = [{"tag": "table", "page_size": min(len(rows), 10), "columns": columns, "rows": rows}]
    else:
        elements = [_columns(headers, header=True)]
    for values, note in entries:
        if not compact:
            elements.append(_columns(values))
        if note:
            caption = "%s　%s" % (values[0], note) if compact else note
            elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content": caption}]})
    return elements


def _backlog_elements(report):
    pending = report.get('backlog')
    if not pending:
        return []
    from .common import parse_time, BEIJING
    as_of = parse_time(pending.get('as_of'))
    label = as_of.astimezone(BEIJING).strftime('%m-%d %H:%M:%S') if as_of else '未知'
    elements = [_md('**核对时刻的跨日期未完成（北京时间 %s）**\n覆盖全部历史计划；未来预约、已取消/策略关闭单列，不计入待处理。与上方统计日计划不可相加。' % label)]
    if not pending.get('available'):
        return elements + [_md('跨日期未完成：**未知**，调度台账读取不完整。')]
    for row in pending['channels']:
        states = row['states']
        elements.append(_md('**%s** 未完成 %s＝已有任务 %s＋未入队计划 %s；另有未来预约 %s、已关闭任务 %s、已取消未入队计划 %s。\n待执行 %s · 执行中 %s · 受阻 %s · 失败 %s · 待核对 %s · 历史暂停 %s。' %
            (row['channel'].upper(), _n(row['unfinished']), row['instantiated_unfinished'], _n(row['unqueued']), row['scheduled'], row['closed'], row['closed_unqueued'],
             *(states.get(k, 0) for k in ('waiting', 'executing', 'blocked', 'failed', 'unknown', 'paused')))))
        reasons = sorted(row['reasons'] + row['plan_reasons'], key=lambda r: -r['count'])[:3]
        if reasons:
            elements.append(_md('；'.join('%s %s 条' % (r['reason'], r['count']) for r in reasons)))
    if pending['warnings']:
        elements.append(_md('**跨日期口径说明**\n' + '\n'.join(dict.fromkeys(pending['warnings']))))
    return elements


def _fb_elements(channel, compact=False):
    rows = channel["rows"]
    sums = total(rows)
    states = {key: sum(r.get(key, 0) for r in rows) for key in
              ("failed", "pending", "blocked", "unknown", "policy_skipped", "frequency_excluded", "superseded_slots")}
    success = None if sums["published"] is None else sums["published"] + sums["late"]
    elements = [_md("**FB｜实际应发 %s · 确认成功 %s · 明确失败 %s**\n"
                    "受阻/待处理 %s · 结果待确认 %s · 策略跳过 %s。" %
                    (_n(sums["expected"]), _n(success), states["failed"], states["blocked"] + states["pending"],
                     states["unknown"], states["policy_skipped"]))]
    entries = []
    for row in rows[:12]:
        confirmed = row.get("published", 0) + row.get("late", 0)
        values = [row.get("label", row["source"]), _n(row.get("expected")), confirmed,
                  row.get("failed", 0), row.get("blocked", 0) + row.get("pending", 0), row.get("unknown", 0)]
        note = "当日确认成功 %s，次日补发 %s；策略跳过 %s。" % (
            row.get("published", 0), row.get("late", 0), row.get("policy_skipped", 0))
        entries.append((values, note))
    elements.extend(_source_table(["发布来源", "实际应发", "成功", "失败", "受阻/待处理", "待确认"], entries, compact))
    elements.append(_md("**统计口径**\n实际应发按冻结的 Page 频次及所选时隙计算；已排除频次外 %s 个位置、执行前已失效的旧计划 %s 个时隙。\n"
                        "成功为当日确认成功 %s + 次日 10:00 前补发 %s；结果待确认不计成功或失败。\n"
                        "策略跳过包含于实际应发，单列且不计失败；受阻/待处理不等同于平台发布失败。" %
                        (states["frequency_excluded"], states["superseded_slots"], _n(sums["published"]), _n(sums["late"]))))
    return elements


def _plan_table(rows, compact):
    entries = []
    for row in rows[:12]:
        counts = total([row])
        values = [row.get("label", row["source"]), _n(counts["expected"]), _n(counts["published"]),
                  _n(counts["late"]), _n(counts["missing"])]
        if compact:
            entries.append((values, ""))
            continue
        forms = row.get("form_counts") or {}
        form_text = "、".join("%s %s" % (k, v) for k, v in forms.items()) if forms else row.get("form", "")
        states = []
        for key, label in (("pending", "待处理"), ("blocked", "受阻"), ("unknown", "结果待确认"), ("failed", "失败"), ("policy_skipped", "已取消/策略关闭")):
            if row.get(key):
                if key == "failed" and row.get("unqueued") == row[key]:
                    label = "未入队"
                states.append("%s %s" % (label, row[key]))
        note = "；".join(x for x in [form_text, "、".join(states)] if x)
        entries.append((values, note[:200]))
    return _source_table(["发布来源", "应发", "当日已发", "补发", "未完成"], entries, compact)


def build_card(report, *, compact=False):
    incomplete = any(c.get("error") or any(r.get("expected") is None for r in c.get("rows", [])) for c in report["channels"])
    elements = [_md("**统计日：%s（北京时间）**\n昨日 00:00–24:00；补发截至次日 10:00。\n范围：AI 后台全部自动、手动 Post。" % report["date"])]
    if incomplete:
        elements.append(_md("⚠️ **部分数据存在缺失或口径说明，详见各渠道备注；未知不计为零。**"))
    for channel in report["channels"]:
        rows = channel.get("rows", [])
        elements.append({"tag": "hr"})
        if channel.get("error"):
            elements.append(_md("**%s｜数据不完整**\n%s" % (channel["channel"], "；".join(channel["warnings"]))))
            continue
        sums = total(rows)
        actual_fb = channel.get("accounting") == "actual-fb-v2"
        if actual_fb:
            elements.extend(_fb_elements(channel, compact))
        else:
            elements.append(_md("**%s｜应发 %s · 当日计划已发 %s · 补发 %s · 未完成 %s**" %
                            (channel["channel"], _n(sums["expected"]), _n(sums["published"]), _n(sums["late"]), _n(sums["missing"]))))
            elements.extend(_plan_table(rows, compact))
        if len(rows) > 12: elements.append(_md("另有 %s 个来源，已计入渠道汇总；完整明细保存在日报归档。" % (len(rows) - 12)))
        elements.append(_md("往期计划昨日确认完成：**%s** 条（不计入上表昨日计划）。" % channel.get("prior_completed", 0)))
        if _cost_text(channel):
            elements.append(_md(_cost_text(channel)))
        reasons = []
        for row in rows:
            for reason in row.get("reasons", []):
                if not reason.get("count"): continue
                qualifier = "（推测）" if reason.get("confidence") == "inferred" else "（待核实）" if reason.get("confidence") == "unknown" else ""
                text = "• **%s：%s 条** %s%s\n  建议：%s" % (row.get("label", row["source"])[:60], reason["count"], reason["reason"][:150], qualifier, reason.get("suggestion", "核对任务记录。")[:150])
                reasons.append((reason["count"], text))
        if reasons:
            limit = 2 if compact else 8
            if compact:
                reasons.sort(key=lambda item: -item[0])
            heading = "**失败、受阻与待确认原因**" if actual_fb else "**缺口原因与建议**"
            lines = [heading] + [text for _, text in reasons[:limit]]
            if len(reasons) > limit:
                lines.append("另有 %s 项原因，完整明细见日报归档。" % (len(reasons) - limit))
            elements.append(_md("\n".join(lines)))
        if actual_fb:
            policies = ["• %s：%s 条 %s" % (row.get("label", row["source"]), reason["count"], reason["reason"])
                        for row in rows for reason in row.get("policy_reasons", [])]
            if policies:
                shown = policies[:2] if compact else policies
                lines = ["**正常策略跳过（不计失败）**"] + shown
                if len(policies) > len(shown):
                    lines.append("另有 %s 项策略说明，详见日报归档。" % (len(policies) - len(shown)))
                elements.append(_md("\n".join(lines)))
        warnings = list(dict.fromkeys(channel.get("warnings", []) + [w for r in rows for w in r.get("warnings", [])]))
        if warnings:
            limit = 1 if compact else 4
            lines = ["**口径说明**"] + ["• " + w[:220] for w in warnings[:limit]]
            if len(warnings) > limit:
                lines.append("另有 %s 条说明，完整内容保存在日报归档。" % (len(warnings) - limit))
            elements.append(_md("\n".join(lines)))
    elements.extend(_backlog_elements(report))
    elements.append({"tag": "hr"})
    elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content": "已发以账本确认成功为准；无平台发布时间时使用系统确认时间。已提交/制作完成不等于已发布。仅播报与建议，不自动补发或调整发布任务。"}]})
    return {"config": {"wide_screen_mode": True},
            "header": {"template": "orange" if incomplete else "blue", "title": {"tag": "plain_text", "content": "%s｜TT / FB / X Post 发布日报" % report["date"]}},
            "elements": elements}


def build_compact_card(report):
    return build_card(report, compact=True)


def preview_html(report, card):
    def render_text(value):
        escaped = html.escape(value).replace("\n", "<br>")
        return re.sub(r"\*\*(.*?)\*\*", r"<strong>\1</strong>", escaped)
    def render_row(values):
        return ('<div class="row" style="grid-template-columns:3fr repeat(%s,1fr)">' % (len(values) - 1)
                + "".join("<span>" + html.escape(v) + "</span>" for v in values) + "</div>")
    lines = []
    for element in card["elements"]:
        if element["tag"] == "hr": lines.append("<hr>")
        elif element["tag"] == "div": lines.append("<p>" + render_text(element["text"]["content"]) + "</p>")
        elif element["tag"] == "column_set":
            cells = [col["elements"][0] for col in element["columns"]]
            lines.append(render_row([cell.get("text", cell)["content"] for cell in cells]))
        elif element["tag"] == "table":
            columns = element["columns"]
            lines.append(render_row([col["display_name"] for col in columns]))
            lines.extend(render_row([row[col["name"]] for col in columns]) for row in element["rows"])
        elif element["tag"] == "note": lines.append("<small>" + html.escape(element["elements"][0]["content"]) + "</small>")
    return '<!doctype html><html lang="zh"><meta charset="utf-8"><title>Post 发布日报预览</title><style>body{font:15px/1.65 system-ui;background:#f3f5f8;color:#182534;margin:30px auto;max-width:900px;padding:25px;background:white}h1{font-size:23px}.row{display:grid;grid-template-columns:3fr repeat(4,1fr);gap:12px;border-bottom:1px solid #eee;padding:8px}small{display:block;color:#687586}hr{border:0;border-top:1px solid #ccc;margin:24px 0}</style><h1>' + html.escape(card["header"]["title"]["content"]) + '</h1>' + "\n".join(lines) + '</html>'
