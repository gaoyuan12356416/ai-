from __future__ import annotations

import html
import json
from datetime import datetime

from .collector import BEIJING


def number(value):
    return "待核实" if value is None else format(value, ",")


def money(value):
    return "待核实" if value is None else "$%.2f" % (value / 100)


def safe_name(value):
    return str(value).replace("<", "＜").replace(">", "＞").replace("\n", " ").replace("*", "＊")[:80]


def metrics_line(row):
    return "收入 **%s** · 激活 **%s** · 点击 **%s** · 访问 **%s**" % (
        money(row["revenue_cents"]), number(row["installs"]), number(row["clicks"]), number(row["views"]))


def build_card(report):
    day = report["report_date"]
    pub_zone = "北京时间" if report["publication_timezone"] == "Asia/Shanghai" else "UTC"
    blocks = ["**统计日：%s**\n发布量：%s自然日｜效果：UTC 自然日\n效果覆盖每个人全部历史内容在统计日产生的数据。" % (day, pub_zone),
              "**合计｜成功发布 %s 条**\n%s" % (number(report["totals"]["published"]), metrics_line(report["totals"]))]
    for row in report["rows"]:
        blocks.append("**%s｜成功发布 %s 条**\n%s" % (safe_name(row["name"]), number(row["published"]), metrics_line(row)))
    if report["unmatched"]["campaigns"]:
        blocks.append("**待归属（未计入个人及合计）**\n" + metrics_line(report["unmatched"]))
    for warning in report["warnings"]:
        blocks.append("⚠ " + warning)
    blocks.append("**指标说明**\n收入为归因内购收入（USD）；激活为安装事件数；点击/访问为落地页数据。"
        "\n**YouTube 曝光：未接入有效日数据，暂不展示数值。**"
        "\n发布量仅计已确认公开的视频，按视频去重；不含排队、上传中或结果未知的任务。"
        "\n效果数据会随回传更新；UTC 统计日对应北京时间当天 08:00 至次日 08:00。")
    if report["totals"]["refund_cents"]:
        blocks.append("当日退款：%s（单列，未从收入扣除）" % money(report["totals"]["refund_cents"]))
    generated = datetime.fromisoformat(report["generated_at"]).astimezone(BEIJING).strftime("%m-%d %H:%M:%S")
    updated = report.get("source_updated_at_utc")
    suffix = "生成：%s 北京时间" % generated
    if updated:
        suffix += "｜源数据更新：%s UTC" % updated
    blocks.append(suffix)
    return {"config": {"wide_screen_mode": True},
            "header": {"template": "blue" if report["metrics_available"] else "orange",
                       "title": {"tag": "plain_text", "content": "YouTube 发布人日报 · " + day}},
            "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": text}} for text in blocks]}


def preview_html(card):
    title = html.escape(card["header"]["title"]["content"])
    blocks = "".join("<pre>" + html.escape(e["text"]["content"]) + "</pre>" for e in card["elements"])
    return '<!doctype html><html lang="zh"><meta charset="utf-8"><title>'+title+'</title><style>body{max-width:850px;margin:32px auto;font:16px system-ui;background:#f4f6fa;color:#172033}pre{white-space:pre-wrap;background:white;padding:18px;border-radius:10px;line-height:1.7}h1{font-size:24px}</style><h1>'+title+'</h1>'+blocks+'</html>'
