"""Compact owner-first Feishu cards with an exact transport-size check."""
from __future__ import annotations

import html
import json
import re

SAFE_PAYLOAD_BYTES = 29000
REPORT_URL = "https://ai.yingliangads.com/social-analytics.html"


def number(value):
    return "—" if value is None else format(value, ",")


def money(cents):
    if cents is None:
        return "—"
    sign = "-" if cents < 0 else ""
    return "%s$%s.%02d" % (sign, format(abs(cents) // 100, ","), abs(cents) % 100)


def safe_name(value):
    value = str(value).replace("\r", " ").replace("\n", " ")
    for old, new in (("<", "＜"), (">", "＞"), ("*", "＊"), ("[", "［"), ("]", "］")):
        value = value.replace(old, new)
    return value[:80]


def metrics_line(row):
    return "激活 **%s** · 点击 **%s** · 访问 **%s**" % (
        number(row.get("installs")), number(row.get("clicks")), number(row.get("views")))


def _available(quality):
    return quality.get("state") in ("ok", "complete", "available")


def incomplete_sources(report):
    failures = []
    for value in report["platforms"].values():
        quality = value["quality"]
        if _available(quality):
            continue
        sources = quality.get("sources", {})
        missing = ["%s（%s）" % (safe_name(v.get("label") or site), safe_name(site))
                   for site, v in sources.items() if not _available(v)]
        failures.append(safe_name(value["name"]) + ("：" + "、".join(missing) if missing else ""))
    if not failures and any(row.get("data_status") != "complete" for row in report["rows"]):
        failures.append("人员归属数据")
    return failures


def require_complete(report):
    """Never deliver a partial collection as a person's financial performance."""
    if incomplete_sources(report):
        raise ValueError("daily_report_sources_incomplete")
    required = ("revenue_cents", "refund_cents", "installs", "clicks", "views", "conversions", "published_count")
    values = [report["totals"]] + report["rows"] + [v["totals"] for v in report["platforms"].values()]
    values += [v for person in report["rows"] for v in person["platforms"].values()]
    if any(row.get(key) is None for row in values for key in required):
        raise ValueError("daily_report_metrics_incomplete")


def md(text):
    return {"tag": "div", "text": {"tag": "lark_md", "content": text}}


def blocks(report):
    failures = incomplete_sources(report)
    if failures:
        return [[md("**数据采集未完成，本次未生成业绩日报。**\n" + "\n".join(failures) +
                    "\n原始记录已保留；缺失数据不计为零。")]], False
    require_complete(report)
    totals = report["totals"]
    result = [[md("**%s · UTC 自然日**\n按 campaign ID 关联用户 · 全部历史内容的当日效果" % report["report_date"])],
              [{"tag": "div", "fields": [
                  {"is_short": True, "text": {"tag": "lark_md", "content": label + "\n**" + value + "**"}}
                  for label, value in (("💰 已归属收入", money(totals["revenue_cents"])),
                                       ("🎬 成功发布", number(totals["published_count"]) + " 条"),
                                       ("✨ 激活", number(totals["installs"])),
                                       ("💳 付费人数", number(totals["conversions"])))]}],
              [md("点击 **%s** · 访问 **%s** · %s 位发布人" % (
                  number(totals["clicks"]), number(totals["views"]), len(report["rows"])))]]
    order = {"facebook": 0, "youtube": 1, "x": 2}
    for index, person in enumerate(report["rows"], 1):
        text = "**%02d · %s**\n收入 **%s**　｜　成功发布 **%s 条**\n%s" % (
            index, safe_name(person["name"]), money(person["revenue_cents"]),
            number(person["published_count"]), metrics_line(person))
        for platform, row in sorted(person["platforms"].items(), key=lambda item: order.get(item[0], 99)):
            name = safe_name(report["platforms"][platform]["name"])
            text += "\n\n**%s**　%s · 发布 %s 条\n%s" % (
                name, money(row["revenue_cents"]), number(row["published_count"]), metrics_line(row))
            if platform == "x":
                text += " · Post %s / Repost %s" % (number(row["post_count"]), number(row["repost_count"]))
        result.append([{"tag": "hr"}, md(text)])
    channels = ["**渠道汇总**"]
    for platform, value in sorted(report["platforms"].items(), key=lambda item: order.get(item[0], 99)):
        row = value["totals"]
        channels.append("%s　**%s** · 发布 %s 条 · 激活 %s" % (
            safe_name(value["name"]), money(row["revenue_cents"]), number(row["published_count"]),
            number(row["installs"])))
    result.append([{"tag": "hr"}, md("\n".join(channels))])
    exceptions = []
    for value in report["platforms"].values():
        for key, label in (("unmatched", "未匹配发布记录"), ("excluded", "已排除测试记录")):
            row = value[key]
            count = value["quality"].get(key + "_campaigns", 0)
            if count or any(row.get(k) not in (0, None) for k in (
                    "revenue_cents", "refund_cents", "installs", "clicks", "views", "conversions")):
                exceptions.append("%s · %s：%s 组，收入 %s，激活 %s，点击 %s，访问 %s，退款 %s。" % (
                    safe_name(value["name"]), label, number(count), money(row["revenue_cents"]),
                    number(row["installs"]), number(row["clicks"]), number(row["views"]), money(row["refund_cents"])))
    if exceptions:
        result.append([{"tag": "hr"}, md("**未计入个人业绩**\n" + "\n".join(exceptions) +
                                          "\n仅将 campaign ID 唯一匹配到用户的记录计入上方数据。")])
    result.append([{"tag": "note", "elements": [{"tag": "plain_text", "content":
        "收入单位 USD；当日退款 %s 单列。点击/访问为落地页数据，激活为安装事件。\n"
        "发布量只计当日确认成功记录；手动短链只计效果。UTC 当天对应北京时间 08:00 至次日 08:00。"
        % money(totals["refund_cents"])}]},
        {"tag": "action", "actions": [{"tag": "button", "type": "primary",
         "text": {"tag": "plain_text", "content": "查看社媒数据明细"}, "url": REPORT_URL}]}])
    return result, True


def make_card(day, content, complete, index=1, count=1):
    suffix = "（%d/%d）" % (index, count) if count > 1 else ""
    return {"config": {"wide_screen_mode": True, "enable_forward": True},
            "header": {"template": "blue" if complete else "orange",
                       "title": {"tag": "plain_text", "content": "社媒发布人日报 · " + day + suffix}},
            "elements": [element for block in content for element in block]}


def payload_size(card, chat_id):
    content = json.dumps(card, ensure_ascii=False, separators=(",", ":"))
    envelope = dict(receive_id=chat_id, msg_type="interactive", content=content, uuid="0" * 36)
    return len(json.dumps(envelope, ensure_ascii=False).encode("utf-8"))


def build_cards(report, chat_id, limit=SAFE_PAYLOAD_BYTES):
    content, complete = blocks(report)
    groups, current = [], []
    for block in content:
        candidate = make_card(report["report_date"], current + [block], complete, 9999, 9999)
        if payload_size(candidate, chat_id) > limit:
            if not current:
                raise ValueError("report_block_exceeds_safe_payload_limit")
            groups.append(current)
            current = [block]
            if payload_size(make_card(report["report_date"], current, complete, 9999, 9999), chat_id) > limit:
                raise ValueError("report_block_exceeds_safe_payload_limit")
        else:
            current.append(block)
    if current:
        groups.append(current)
    if len(groups) > 9999:
        raise ValueError("report_fragment_limit")
    cards = [make_card(report["report_date"], group, complete, index, len(groups))
             for index, group in enumerate(groups, 1)]
    if any(payload_size(card, chat_id) > limit for card in cards):
        raise ValueError("report_card_exceeds_safe_payload_limit")
    return cards


def preview_html(cards):
    def markup(text):
        escaped = html.escape(text)
        return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped).replace("\n", "<br>")
    parts = []
    for card in cards:
        parts.append('<article><header>' + html.escape(card["header"]["title"]["content"]) + '</header><main>')
        for element in card["elements"]:
            tag = element["tag"]
            if tag == "hr":
                parts.append('<hr>')
            elif tag == "div":
                if element.get("text"):
                    parts.append('<section>' + markup(element["text"]["content"]) + '</section>')
                if element.get("fields"):
                    parts.append('<div class="metrics">' + ''.join(
                        '<div>' + markup(field["text"]["content"]) + '</div>' for field in element["fields"]) + '</div>')
            elif tag == "note":
                parts.append('<aside>' + '<br>'.join(markup(x["content"]) for x in element["elements"]) + '</aside>')
            elif tag == "action":
                parts.extend('<a href="' + html.escape(x["url"], quote=True) + '">' +
                             html.escape(x["text"]["content"]) + '</a>' for x in element["actions"])
        parts.append('</main></article>')
    return ('''<!doctype html><html lang="zh"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>社媒发布人日报预览</title><style>*{box-sizing:border-box}body{max-width:760px;margin:24px auto;padding:0 12px;
font:15px/1.65 "Microsoft YaHei",system-ui;background:#f3f5f9;color:#202b3d}article{background:#fff;border:1px solid #e2e7ef;
border-radius:12px;overflow:hidden;margin-bottom:20px}header{padding:18px 24px;background:#e8f0ff;color:#245bba;
font-size:20px;font-weight:700}main{padding:20px 24px}section{margin:10px 0}strong{color:#172d50}
.metrics{display:grid;grid-template-columns:1fr 1fr;gap:16px;padding:16px;background:#f7f9fd;border-radius:10px}
.metrics strong{font-size:25px}hr{border:0;border-top:1px solid #e6ebf2;margin:24px 0}aside{font-size:12px;color:#778294;
margin:22px 0 14px}a{display:inline-block;border:1px solid #3370ff;border-radius:6px;color:#3370ff;padding:6px 16px;
text-decoration:none}@media(max-width:480px){body{font-size:13px;margin:8px auto}main{padding:14px}header{padding:14px;font-size:17px}.metrics strong{font-size:22px}}
</style>''' + "".join(parts) + "</html>")
