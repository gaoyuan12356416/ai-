from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from urllib import request, error


class DefiniteFailure(Exception):
    pass


class DeliveryUnknown(Exception):
    pass


class DeliveryStore:
    def __init__(self, path):
        self.db = sqlite3.connect(str(path), timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("CREATE TABLE IF NOT EXISTS delivery (report_date TEXT NOT NULL, chat_id TEXT NOT NULL, request_uuid TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, message_id TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL, PRIMARY KEY(report_date,chat_id))")
        self.db.commit()

    def get(self, report_date, chat_id):
        row = self.db.execute("SELECT * FROM delivery WHERE report_date=? AND chat_id=?", (report_date, chat_id)).fetchone()
        return dict(row) if row else None

    def claim(self, report_date, chat_id):
        self.db.execute("BEGIN IMMEDIATE")
        row = self.get(report_date, chat_id)
        if row and row["status"] == "sent":
            self.db.rollback()
            return None
        if row and row["status"] in ("sending", "unknown"):
            self.db.rollback()
            raise DeliveryUnknown("prior_delivery_unconfirmed")
        if row and row["attempts"] >= 3:
            self.db.rollback()
            raise DefiniteFailure("delivery_retry_limit")
        request_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, "post-daily-report/" + report_date + "/" + chat_id))
        self.db.execute("INSERT INTO delivery(report_date,chat_id,request_uuid,status,attempts,updated_at) VALUES(?,?,?,'sending',1,?) ON CONFLICT(report_date,chat_id) DO UPDATE SET status='sending',attempts=delivery.attempts+1,updated_at=excluded.updated_at", (report_date, chat_id, request_uuid, datetime.now(timezone.utc).isoformat()))
        self.db.commit()
        return request_uuid

    def finish(self, report_date, chat_id, status, message_id="", detail=""):
        self.db.execute("UPDATE delivery SET status=?,message_id=?,detail=?,updated_at=? WHERE report_date=? AND chat_id=?", (status, message_id, detail, datetime.now(timezone.utc).isoformat(), report_date, chat_id))
        self.db.commit()

    def close(self):
        self.db.close()


def _post(url, payload, token=None, sending=False):
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token: headers["Authorization"] = "Bearer " + token
    req = request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers=headers)
    try:
        with request.urlopen(req, timeout=20) as response:
            result = json.load(response)
    except error.HTTPError as exc:
        if exc.code >= 500 and sending:
            raise DeliveryUnknown("feishu_http_%s" % exc.code) from None
        raise DefiniteFailure("feishu_http_%s" % exc.code) from None
    except Exception as exc:
        cls = DeliveryUnknown if sending else DefiniteFailure
        raise cls("feishu_transport_%s" % type(exc).__name__) from None
    if result.get("code") != 0:
        raise DefiniteFailure("feishu_code_%s" % result.get("code", "missing"))
    return result


def send_card(config_path, chat_id, card, request_uuid):
    try:
        with open(config_path, encoding="utf-8") as handle:
            config = json.load(handle)
        credentials = {"app_id": config["appId"], "app_secret": config["appSecret"]}
    except Exception:
        raise DefiniteFailure("feishu_config_unavailable") from None
    base = "https://open.feishu.cn/open-apis"
    auth = _post(base + "/auth/v3/tenant_access_token/internal", credentials)
    token = auth.get("tenant_access_token")
    if not token:
        raise DefiniteFailure("feishu_auth_token_missing")
    result = _post(base + "/im/v1/messages?receive_id_type=chat_id",
                   {"receive_id": chat_id, "msg_type": "interactive", "content": json.dumps(card, ensure_ascii=False, separators=(",", ":")), "uuid": request_uuid}, token, sending=True)
    message_id = result.get("data", {}).get("message_id")
    if not message_id:
        raise DeliveryUnknown("feishu_success_without_message_id")
    return {"code": result["code"], "message_id": message_id,
            "create_time": result.get("data", {}).get("create_time")}
