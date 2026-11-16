"""Persistent personal Feishu notifications. Delivery is independent of publishing."""

import importlib.util
import json
import os
import time
import uuid

from . import store

RECIPIENT = "ou_62fed22c4918524e48ded4eab8b82b1e"
SERVICE_REASONS = {
    "quota_unavailable",
    "generation_timeout",
    "codex_unavailable",
    "codex_request_failed",
    "codex_generation_failed",
    "caption_store_unavailable",
}


def add(c, event, items):
    if not items:
        return
    reasons = {x["reason"] for x in items}
    incident = (
        next(iter(reasons)) if len(reasons) == 1 and reasons <= SERVICE_REASONS else ""
    )
    c.execute(
        "INSERT OR IGNORE INTO alerts(event,incident,body,created) VALUES(?,?,?,?)",
        (event, incident, store.dump(items), time.time()),
    )


def send(event, body):
    path = os.environ.get(
        "STORY_COPY_FEISHU_HELPER", "/root/codex_test/fb_page_ad_restriction_report.py"
    )
    spec = importlib.util.spec_from_file_location("story_copy_feishu_transport", path)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    token = helper.tenant_access_token(helper.load_config(helper.CONFIG_PATH))
    payload = {
        "receive_id": RECIPIENT,
        "msg_type": "text",
        "uuid": str(uuid.uuid5(uuid.NAMESPACE_URL, event)),
        "content": json.dumps({"text": body}, ensure_ascii=False),
    }
    result = helper.post_json(
        "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id",
        payload,
        token,
    )
    if result.get("code") != 0 or not result.get("data", {}).get("message_id"):
        raise RuntimeError("feishu_delivery_failed")
    return result["data"]["message_id"]


def deliver(c, send_func=send):
    now = time.time()
    rows = c.execute(
        "SELECT * FROM alerts WHERE sent IS NULL AND next_try<=? ORDER BY created,event LIMIT 100",
        (now,),
    ).fetchall()
    for r in rows:
        if r["incident"]:
            last = c.execute(
                "SELECT max(sent) FROM alerts WHERE incident=?", (r["incident"],)
            ).fetchone()[0]
            if last and now - last < 900:
                continue
            siblings = [other for other in rows if other["incident"] == r["incident"]]
        else:
            siblings = [r]
        if c.execute("SELECT sent FROM alerts WHERE event=?", (r["event"],)).fetchone()[
            0
        ]:
            continue
        items = [i for entry in siblings for i in json.loads(entry["body"])]
        groups = {}
        for i in items:
            group = (
                i["endpoint"],
                i["title"],
                i["language"],
                i["reason"],
                i["outcome"],
            )
            groups.setdefault(group, []).append(i["key"])
        lines = ["短剧 AI 文案异常", f"影响 {len(items)} 条。"]
        for (end, title, lang, reason, outcome), ids in groups.items():
            lines.append(
                f"{end} · {title} · {lang} · {reason} · {outcome}\n任务 "
                + ", ".join(ids)
            )
        keys = [(x["event"],) for x in siblings]
        try:
            receipt = send_func(r["event"], "\n".join(lines))
        except Exception:
            c.executemany(
                "UPDATE alerts SET attempts=attempts+1,next_try=strftime('%s','now')+min(900,30*(attempts+1)) WHERE event=?",
                keys,
            )
        else:
            c.executemany(
                "UPDATE alerts SET sent=?,receipt=?,attempts=attempts+1 WHERE event=?",
                [(now, receipt, k[0]) for k in keys],
            )
        c.commit()
