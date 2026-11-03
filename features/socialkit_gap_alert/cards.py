"""Small Feishu cards. Business strings are plain text, never executable markup."""
import json
from decimal import Decimal
from datetime import datetime, timedelta, timezone

LABELS = {"facebook": "Facebook", "youtube": "YouTube", "x": "X"}
MAX_BYTES = 24000
MAX_ITEMS = 10


def dollars(cents):
    return "$" + format(Decimal(cents) / 100, ".2f")


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def beijing_time(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return "待核实"


def make_card(report, events):
    text = ["Dramawave · SocialKit 有收入，我方尚无收入",
            "统计日：%s UTC（北京时间 08:00 切日）" % report["date"],
            "SocialKit 源更新（北京时间）：%s" % beijing_time(report["socialkit"].get("source_updated_at")),
            "比较采集（北京时间）：%s" % beijing_time(report["collected_at"]),
            "收入为 USD 原始收入；当天数据仍会回传。"]
    elements = [{"tag": "div", "text": {"tag": "plain_text", "content": "\n".join(text)}}]
    for event in events:
        label = ("已发布，今日未出收入" if event["history_success_count"] else "未查到成功发布")
        content = ("%s · %s\n剧目 ID：%s\nSocialKit %s ／ 我方 %s\n%s"
                   "\n历史成功发布：%d；最近发布日期（UTC）：%s") % (
                       LABELS[event["platform"]], event["drama_name"], event["drama"],
                       dollars(event["socialkit_revenue_cents"]), dollars(event["own_revenue_cents"]),
                       label, event["history_success_count"], event["last_published_date"] or "无")
        elements.append({"tag": "hr"})
        elements.append({"tag": "div", "text": {"tag": "plain_text", "content": content}})
    own = report.get("own_source_updated_at", {})
    times = ["%s/%s：%s" % (LABELS[p], site, beijing_time(value)) for p in LABELS
             for site, value in sorted(own.get(p, {}).items())]
    if times:
        elements.append({"tag": "note", "elements": [{"tag": "plain_text",
                         "content": "我方源更新（北京时间）：" + "；".join(times)}]})
    elements.append({"tag": "action", "actions": [
        {"tag": "button", "text": {"tag": "plain_text", "content": "SocialKit 效果报表"},
         "url": "https://ai.yingliangads.com/socialkit-analytics.html", "type": "default"},
        {"tag": "button", "text": {"tag": "plain_text", "content": "我方社媒效果"},
         "url": "https://ai.yingliangads.com/social-analytics.html", "type": "default"}]})
    return {"config": {"wide_screen_mode": True},
            "header": {"template": "orange", "title": {"tag": "plain_text",
                       "content": "SocialKit 剧目收入差异提醒 · %d 项" % len(events)}},
            "elements": elements}


def payload_size(card, chat_id):
    return len(compact({"receive_id": chat_id, "msg_type": "interactive",
                        "content": compact(card), "uuid": "0" * 36}).encode("utf-8"))


def split_cards(report, events, chat_id):
    result, batch = [], []
    for event in events:
        attempt = batch + [event]
        if batch and (len(attempt) > MAX_ITEMS or payload_size(make_card(report, attempt), chat_id) > MAX_BYTES):
            result.append((batch, make_card(report, batch)))
            batch = []
        batch.append(event)
        if payload_size(make_card(report, batch), chat_id) > MAX_BYTES:
            raise ValueError("single_alert_exceeds_card_limit")
    if batch:
        result.append((batch, make_card(report, batch)))
    return result
