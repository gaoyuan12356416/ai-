"""Feishu cards, measured with exactly the JSON envelope sent to Feishu."""
from __future__ import annotations

import html
import json


SAFE_PAYLOAD_BYTES = 29000
REPORT_URL = "https://ai.yingliangads.com/social-analytics.html"


def number(value):
    return "待核实" if value is None else format(value, ",")


def money(cents):
    if cents is None:
        return "待核实"
    sign = "-" if cents < 0 else ""
    return "%s$%s.%02d" % (sign, format(abs(cents) // 100, ","), abs(cents) % 100)


def safe_name(value):
    value = str(value).replace("\r", " ").replace("\n", " ")
    for old, new in (("<", "＜"), (">", "＞"), ("*", "＊"), ("[", "［"), ("]", "］")):
        value = value.replace(old, new)
    return value[:80]


def metrics_line(row):
    return "收入 **%s** · 激活 **%s** · 点击 **%s** · 访问 **%s**" % (
        money(row.get("revenue_cents")), number(row.get("installs")),
        number(row.get("clicks")), number(row.get("views")))


def published_line(row, platform=None):
    text = "成功发布 %s 条" % number(row.get("published_count"))
    if platform == "x":
        text += "（Post %s · Repost %s）" % (
            number(row.get("post_count")), number(row.get("repost_count")))
    return text


def _available(quality):
    # New states must opt into completeness, never silently become a zero.
    return quality.get("state") in ("ok", "complete", "available")


def source_lines(quality):
    """Expose independently missing sites without making a platform look complete."""
    result = []
    for site_id, source in quality.get("sources", {}).items():
        status = "已有源数据" if _available(source) else "待核实／未同步"
        details = []
        missing = source.get("missing_dates") or []
        if missing:
            details.append("%s 个日期未同步" % len(missing))
        collector = source.get("collector") or {}
        if collector.get("state") and not _available(collector):
            details.append("归属采集待核实")
        if collector.get("freshness") in ("stale", "expired"):
            details.append("归属快照已过期")
        result.append("%s（%s）：%s%s" % (
            safe_name(source.get("label") or site_id), safe_name(site_id), status,
            " · " + " · ".join(details) if details else ""))
    return result


def blocks(report):
    complete = all(_available(value["quality"]) for value in report["platforms"].values())
    intro = (
        "**统计日：%s**\n发布量：UTC 自然日｜效果：UTC 自然日\n"
        "效果覆盖每个人全部历史内容在统计日产生的数据。\n"
        "归因：站点隔离后，以 campaign ID 联表发布记录／手动短链账本。"
    ) % report["report_date"]
    if not complete:
        intro += "\n⚠ **部分渠道数据不完整，合计仅表示已读取部分；缺失数据不能判为零。**"
    result = [intro, "**已归属合计｜%s**\n%s" % (
        published_line(report["totals"]), metrics_line(report["totals"]))]
    for platform, value in report["platforms"].items():
        quality = value["quality"]
        status = "" if _available(quality) else "｜待核实／未同步"
        subtotal = "**%s 渠道小计%s｜%s**\n%s" % (
            safe_name(value["name"]), status, published_line(value["totals"], platform),
            metrics_line(value["totals"]))
        sources = source_lines(quality)
        if sources:
            subtotal += "\n" + "\n".join(sources)
        result.append(subtotal)
    for person in report["rows"]:
        status = "" if person.get("data_status") == "complete" else "｜数据不完整，以下为已知部分"
        text = "**%s%s｜%s**\n%s" % (
            safe_name(person["name"]), status, published_line(person), metrics_line(person))
        for platform, row in person["platforms"].items():
            text += "\n%s｜%s\n%s" % (
                safe_name(report["platforms"].get(platform, {}).get("name") or platform),
                published_line(row, platform), metrics_line(row))
        result.append(text)
    for platform, value in report["platforms"].items():
        for key, title in (("unmatched", "待归属"), ("excluded", "明确排除")):
            row = value[key]
            campaigns = value["quality"].get(key + "_campaigns", 0)
            if campaigns or any(row.get(field) not in (None, 0) for field in (
                    "revenue_cents", "refund_cents", "installs", "clicks", "views", "conversions")):
                result.append("**%s %s（%s 个 campaign ID，未计入个人及合计）**\n%s\n退款 %s" % (
                    value["name"], title, number(campaigns), metrics_line(row), money(row.get("refund_cents"))))
        if not _available(value["quality"]):
            result.append("⚠ %s：效果或发布记录未完整读取，本渠道显示待核实；请查看后台完整性状态。" % value["name"])
    for warning in report.get("warnings", []):
        result.append("⚠ " + safe_name(warning))
    result.append(
        "**指标说明**\n收入为归因内购收入（USD）；激活为安装事件数；点击／访问为落地页数据。"
        "\n当日退款：%s（单列，未从收入扣除）。"
        "\nYouTube 只计已确认公开的视频；X 只计已确认完成的目标账号 Post／Repost，中转源 Post 不重复计入。"
        "\nFacebook 合并 AI 后台旧自动发布与新手动发布，按确认完成的主页帖子去重；不属于 AI 后台发布账本的效果单列待归属。"
        "\n手动短链贡献效果，不计成功发布量；Facebook 手动发布属于发布记录。排队、上传中和结果未知均不算成功。"
        "\n未接入有效日数据的曝光指标不显示数值。效果会随回传更新。"
        "\nUTC 统计日对应北京时间当天 08:00 至次日 08:00。"
        "\n[打开社媒数据报表](%s)" % (money(report["totals"].get("refund_cents")), REPORT_URL))
    return result, complete


def make_card(day, content, complete, index=1, count=1):
    suffix = "（%d/%d）" % (index, count) if count > 1 else ""
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "blue" if complete else "orange",
                   "title": {"tag": "plain_text", "content": "社媒发布人日报 · " + day + suffix}},
        "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": text}} for text in content],
    }


def payload_size(card, chat_id):
    content = json.dumps(card, ensure_ascii=False, separators=(",", ":"))
    envelope = dict(receive_id=chat_id, msg_type="interactive", content=content, uuid="0" * 36)
    return len(json.dumps(envelope, ensure_ascii=False).encode("utf-8"))


def build_cards(report, chat_id, limit=SAFE_PAYLOAD_BYTES):
    content, complete = blocks(report)
    groups, current = [], []
    # Reserve the largest supported part suffix, so final numbering never overflows.
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
    parts = []
    for card in cards:
        parts.append("<h1>" + html.escape(card["header"]["title"]["content"]) + "</h1>")
        parts.extend("<pre>" + html.escape(element["text"]["content"]) + "</pre>"
                     for element in card["elements"])
    return ('<!doctype html><html lang="zh"><meta charset="utf-8"><title>社媒发布人日报预览</title>'
            '<style>body{max-width:850px;margin:32px auto;font:16px system-ui;background:#f4f6fa;'
            'color:#172033}pre{white-space:pre-wrap;background:white;padding:18px;border-radius:10px;'
            'line-height:1.7}h1{font-size:24px}</style>' + "".join(parts) + "</html>")
