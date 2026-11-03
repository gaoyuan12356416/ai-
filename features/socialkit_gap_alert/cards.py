"""Small Feishu cards. Business strings are plain text, never executable markup."""
import json
from decimal import Decimal
from datetime import datetime, timedelta, timezone

from .collector import comparison_period

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
    period = report.get("period") or comparison_period(report["date"])
    text = ["Dramawave · 我方未发布或收入差距较大",
            "收入区间：%s 至 %s UTC（最近 7 个完整日，不含当天）" % (period["start"], period["end"]),
            "播报日期：%s UTC（北京时间 08:00 切日）" % report["date"],
            "条件一：SocialKit 区间收入 > $0，且我方该平台全部历史未查到成功发布。",
            "条件二：SocialKit 区间收入 ≥ $10，且我方区间收入 < SocialKit 的 50%。满足任一条件播报。",
            "SocialKit 区间源最新更新（北京时间）：%s" % beijing_time(report["socialkit"].get("source_updated_at")),
            "比较采集（北京时间）：%s" % beijing_time(report["collected_at"]),
            "收入为 USD 原始收入，不扣退款；历史数据可能回补。"]
    elements = [{"tag": "div", "text": {"tag": "plain_text", "content": "\n".join(text)}}]
    for event in events:
        reasons = event.get("reasons") or [event["state"]]
        label = "；".join({"no_confirmed_publication": "该平台全部历史未查到成功发布",
                           "large_revenue_gap": "近 7 个完整日收入差距较大"}.get(reason, reason)
                          for reason in reasons)
        socialkit, own = event["socialkit_revenue_cents"], event["own_revenue_cents"]
        ratio = format(Decimal(own) * 100 / Decimal(socialkit), ".2f") + "%"
        content = ("%s · %s\n剧目 ID：%s\n近 7 日收入：SocialKit %s ／ 我方 %s\n命中原因：%s"
                   "\n收入差额（SocialKit－我方）：%s；我方占比：%s"
                   "\n历史成功发布：%d；最近发布日期（UTC）：%s") % (
                       LABELS[event["platform"]], event["drama_name"], event["drama"],
                       dollars(event["socialkit_revenue_cents"]), dollars(event["own_revenue_cents"]),
                       label, dollars(socialkit - own), ratio,
                       event["history_success_count"], event["last_published_date"] or "无")
        elements.append({"tag": "hr"})
        elements.append({"tag": "div", "text": {"tag": "plain_text", "content": content}})
    own = report.get("own_source_updated_at", {})
    times = ["%s/%s：%s" % (LABELS[p], site, beijing_time(value)) for p in LABELS
             for site, value in sorted(own.get(p, {}).items())]
    if times:
        elements.append({"tag": "note", "elements": [{"tag": "plain_text",
                         "content": "我方区间源最新更新（北京时间）：" + "；".join(times)}]})
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
