from __future__ import annotations

import html
import importlib
import json
import re
from datetime import datetime, timezone


def collect_report(paths, window):
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
    return {"schema_version": 1, "date": window.date,
            "cutoff_utc": window.cutoff.isoformat(),
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "scope": "AI后台全部Post；不含旧版独立系统及平台端自行发帖", "channels": channels}


def validate_channel(channel):
    for row in channel.get("rows", []):
        for field in ("published", "late", "pending", "unknown", "failed"):
            value = row.get(field, 0)
            if not isinstance(value, int) or value < 0:
                raise ValueError("invalid report counter")
        expected = row.get("expected")
        if expected is not None and (not isinstance(expected, int) or expected < 0):
            raise ValueError("invalid expected counter")
        if expected is not None and row.get("published", 0) + row.get("late", 0) > expected:
            raise ValueError("completion exceeds planned targets")


def total(rows):
    known = all(r.get("expected") is not None for r in rows)
    expected = sum(r.get("expected") or 0 for r in rows)
    published = sum(r.get("published", 0) for r in rows)
    late = sum(r.get("late", 0) for r in rows)
    return {"expected": expected if known else None, "known_expected": expected,
            "published": published, "late": late,
            "missing": expected - published - late if known else None}


def _n(value):
    return "未知" if value is None else str(value)


def _md(text):
    return {"tag": "div", "text": {"tag": "lark_md", "content": text}}


def _columns(values, header=False):
    return {"tag": "column_set", "flex_mode": "none", "background_style": "grey" if header else "default",
            "columns": [{"tag": "column", "width": "weighted", "weight": 3 if i == 0 else 1,
                         "elements": [{"tag": "div", "text": {"tag": "plain_text", "content": str(v)}}]}
                        for i, v in enumerate(values)]}


def build_card(report):
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
        elements.append(_md("**%s｜应发 %s · 昨日已发 %s · 补发 %s · 未完成 %s**" %
                            (channel["channel"], _n(sums["expected"]), sums["published"], sums["late"], _n(sums["missing"]))))
        elements.append(_columns(["发布来源", "应发", "已发", "补发", "未完成"], True))
        for row in rows[:12]:
            counts = total([row])
            elements.append(_columns([row.get("label", row["source"]), _n(counts["expected"]), counts["published"], counts["late"], _n(counts["missing"])]))
            forms = row.get("form_counts") or {}
            form_text = "、".join("%s %s" % (k, v) for k, v in forms.items()) if forms else row.get("form", "")
            states = []
            for key, label in (("pending", "待处理"), ("unknown", "结果待确认"), ("failed", "失败/受阻")):
                if row.get(key): states.append("%s %s" % (label, row[key]))
            note = "；".join(x for x in [form_text, "、".join(states)] if x)
            if note: elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content": note[:200]}]})
        if len(rows) > 12: elements.append(_md("另有 %s 个来源，已计入渠道汇总；完整明细保存在日报归档。" % (len(rows) - 12)))
        elements.append(_md("往期计划昨日确认完成：**%s** 条（不计入上表昨日计划）。" % channel.get("prior_completed", 0)))
        reasons = []
        for row in rows:
            for reason in row.get("reasons", []):
                if not reason.get("count"): continue
                qualifier = "（推测）" if reason.get("confidence") == "inferred" else "（待核实）" if reason.get("confidence") == "unknown" else ""
                reasons.append("• **%s：%s 条** %s%s\n  建议：%s" % (row.get("label", row["source"])[:60], reason["count"], reason["reason"][:150], qualifier, reason.get("suggestion", "核对任务记录。")[:150]))
        if reasons: elements.append(_md("**缺口原因与建议**\n" + "\n".join(reasons[:8]) + ("\n另有 %s 项原因，完整明细见日报归档。" % (len(reasons) - 8) if len(reasons) > 8 else "")))
        warnings = list(dict.fromkeys(channel.get("warnings", []) + [w for r in rows for w in r.get("warnings", [])]))
        if warnings: elements.append(_md("**口径说明**\n" + "\n".join("• " + w[:220] for w in warnings[:4]) + ("\n另有 %s 条说明，完整内容保存在日报归档。" % (len(warnings) - 4) if len(warnings) > 4 else "")))
    elements.append({"tag": "hr"})
    elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content": "已发以账本确认成功为准；无平台发布时间时使用系统确认时间。已提交/制作完成不等于已发布。仅播报与建议，不自动补发或调整发布任务。"}]})
    return {"config": {"wide_screen_mode": True},
            "header": {"template": "orange" if incomplete else "blue", "title": {"tag": "plain_text", "content": "%s｜TT / FB / X Post 发布日报" % report["date"]}},
            "elements": elements}


def build_compact_card(report):
    elements = [_md("**北京时间 %s**｜补发截至次日 10:00。\n数据较多，本卡展示渠道汇总和主要原因；完整明细保存在服务器日报归档。" % report["date"])]
    for channel in report["channels"]:
        if channel.get("error"):
            elements.append(_md("**%s：数据读取失败，数量未知。**" % channel["channel"]))
            continue
        rows = channel.get("rows", [])
        sums = total(rows)
        elements.append(_md("**%s** 应发 %s｜已发 %s｜补发 %s｜未完成 %s；往期昨日完成 %s。" % (channel["channel"], _n(sums["expected"]), sums["published"], sums["late"], _n(sums["missing"]), channel.get("prior_completed", 0))))
        for row in rows[:10]:
            elements.append(_md("%s：%s / %s / %s（应发 / 已发 / 补发）" % (row.get("label", row["source"])[:60], _n(row.get("expected")), row.get("published", 0), row.get("late", 0))))
        reasons = [r for row in rows for r in row.get("reasons", []) if r.get("count")]
        for r in sorted(reasons, key=lambda r: r["count"], reverse=True)[:2]:
            elements.append(_md("%s 条：%s%s；建议：%s" % (r["count"], r["reason"][:100], "（推测/待核实）" if r.get("confidence") != "confirmed" else "", r.get("suggestion", "核查发布记录")[:120])))
    return {"config": {"wide_screen_mode": True}, "header": {"template": "orange", "title": {"tag": "plain_text", "content": "%s｜TT / FB / X Post 发布日报" % report["date"]}}, "elements": elements}


def preview_html(report, card):
    def render_text(value):
        escaped = html.escape(value).replace("\n", "<br>")
        return re.sub(r"\*\*(.*?)\*\*", r"<strong>\1</strong>", escaped)
    lines = []
    for element in card["elements"]:
        if element["tag"] == "hr": lines.append("<hr>")
        elif element["tag"] == "div": lines.append("<p>" + render_text(element["text"]["content"]) + "</p>")
        elif element["tag"] == "column_set":
            values = [col["elements"][0]["text"]["content"] for col in element["columns"]]
            lines.append('<div class="row">' + "".join("<span>" + html.escape(v) + "</span>" for v in values) + "</div>")
        elif element["tag"] == "note": lines.append("<small>" + html.escape(element["elements"][0]["content"]) + "</small>")
    return '<!doctype html><html lang="zh"><meta charset="utf-8"><title>Post 发布日报预览</title><style>body{font:15px/1.65 system-ui;background:#f3f5f8;color:#182534;margin:30px auto;max-width:900px;padding:25px;background:white}h1{font-size:23px}.row{display:grid;grid-template-columns:3fr repeat(4,1fr);gap:12px;border-bottom:1px solid #eee;padding:8px}small{display:block;color:#687586}hr{border:0;border-top:1px solid #ccc;margin:24px 0}</style><h1>' + html.escape(card["header"]["title"]["content"]) + '</h1>' + "\n".join(lines) + '</html>'
