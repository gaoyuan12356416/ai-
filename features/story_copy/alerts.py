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
    groups = {}
    for item in items:
        groups.setdefault(item.get("publish_batch", ""), []).append(item)
    if len(groups) > 1:
        for batch, members in groups.items():
            add(c, event + ":" + uuid.uuid5(uuid.NAMESPACE_URL, batch).hex, members)
        return
    reasons = {x["reason"] for x in items}
    incident = (
        next(iter(reasons)) if len(reasons) == 1 and reasons <= SERVICE_REASONS else ""
    )
    publish_batches = {x.get("publish_batch", "") for x in items}
    if len(publish_batches) == 1 and "" not in publish_batches:
        incident = "publish-batch:" + next(iter(publish_batches))
    previous = c.execute(
        "SELECT body,sent FROM alerts WHERE event=?", (event,)
    ).fetchone()
    if previous:
        packed = c.execute(
            "SELECT 1 FROM packed_alerts WHERE event=?", (event,)
        ).fetchone()
        if previous["sent"] or packed:
            event += ":" + items[0]["key"]
        else:
            combined = {entry["key"]: entry for entry in json.loads(previous["body"])}
            combined.update({entry["key"]: entry for entry in items})
            c.execute(
                "UPDATE alerts SET body=? WHERE event=?",
                (store.dump(list(combined.values())), event),
            )
            return
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
        packet = c.execute(
            "SELECT p.* FROM alert_packets p JOIN packed_alerts a ON p.id=a.packet WHERE a.event=?",
            (r["event"],),
        ).fetchone()
        if packet:
            if packet["delivered"]:
                continue
            if packet["id"] != r["event"]:
                continue
            keys = [(k,) for k in json.loads(packet["events"])]
            message = packet["body"]
        else:
            keys = []
        if not packet and r["incident"]:
            publication = r["incident"].startswith("publish-batch:")
            if publication and c.execute(
                "SELECT 1 FROM jobs WHERE state IN ('pending','generating') "
                "AND json_extract(payload,'$.publish_batch')=? LIMIT 1",
                (r["incident"].removeprefix("publish-batch:"),),
            ).fetchone():
                # A native submission may span several drama/language model calls.
                # Freeze its notification only when all discovered jobs have settled.
                continue
            last = c.execute(
                "SELECT max(sent) FROM alerts WHERE incident=?", (r["incident"],)
            ).fetchone()[0]
            if not publication and last and now - last < 900:
                continue
            candidates = c.execute(
                "SELECT * FROM alerts WHERE incident=? AND sent IS NULL AND next_try<=? ORDER BY created,event",
                (r["incident"], now),
            ).fetchall() if publication else rows
            siblings = [
                other
                for other in candidates
                if other["incident"] == r["incident"]
                and not c.execute(
                    "SELECT 1 FROM packed_alerts WHERE event=?", (other["event"],)
                ).fetchone()
            ]
        else:
            siblings = [r]
        if c.execute("SELECT sent FROM alerts WHERE event=?", (r["event"],)).fetchone()[
            0
        ]:
            continue
        if not packet:
            # Freeze membership and payload before the first send. A timeout reuses the same Feishu UUID.
            if not r["incident"].startswith("publish-batch:"):
                siblings = siblings[:8]
            keys = [(x["event"],) for x in siblings]
            message = message_for(siblings)
            c.execute(
                "INSERT INTO alert_packets(id,incident,events,body) VALUES(?,?,?,?)",
                (r["event"], r["incident"], store.dump([k[0] for k in keys]), message),
            )
            c.executemany(
                "INSERT INTO packed_alerts VALUES(?,?)",
                [(key[0], r["event"]) for key in keys],
            )
            c.commit()
        try:
            receipt = send_func(r["event"], message)
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
            c.execute(
                "UPDATE alert_packets SET delivered=? WHERE id=?", (now, r["event"])
            )
        c.commit()


def message_for(siblings):
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
    shown = 0
    for (end, title, lang, reason, outcome), ids in groups.items():
        detail = f"{end} · {title} · {lang} · {reason} · {outcome}\n任务 " + ", ".join(ids)
        if len(("\n".join(lines) + "\n" + detail).encode("utf-8")) > 16000:
            break
        lines.append(detail)
        shown += len(ids)
    if shown < len(items):
        lines.append(f"另有 {len(items) - shown} 条，完整明细已保存在文案告警记录中。")
    return "\n".join(lines)
