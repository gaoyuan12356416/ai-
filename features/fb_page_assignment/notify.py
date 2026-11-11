"""Private Feishu alerts using the existing app and current administrator identity."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import time

import requests


class DeliveryError(Exception):
    def __init__(self, code, uncertain=False):
        super().__init__(code)
        self.code = code
        self.uncertain = uncertain


def alert_text(task):
    lines = ["【剧目 Page 分配异常】", "调用方关联 ID：" + str(task.get("external_task_id") or "未提供"),
             "本次 task_id：" + task["task_id"],
             "北京时间：" + datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S"),
             "原因：" + "; ".join(str(r) for r in task.get("alert_reasons", [])),
             "查询表：ads_ai.fb_drama_page_assignment_log"]
    rows = task.get("rows", [])
    for row in rows[:20]:
        lines.append("剧 " + str(row.get("drama_id") or "无有效剧ID") + " → Page " +
                     str(row.get("page_id") or "未分配") + "；" +
                     str(row.get("error_message") or row.get("error_code") or row.get("selection_stage") or ""))
    if len(rows) > 20:
        lines.append("另有 %s 条，请按两个任务 ID 查询数据库完整记录。" % (len(rows) - 20))
    return "\n".join(lines).encode("utf-8")[:18000].decode("utf-8", errors="ignore")


class Notifier:
    def __init__(self, env, session=None):
        self.env = env
        self.http = session or requests.Session()
        self._token = ""
        self._expires = 0

    def recipient(self):
        path = Path(self.env.get("DRAMA_JOB_DB_PATH", "/root/drama_material_service/data/drama_material_jobs.sqlite3"))
        try:
            with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)) as db:
                rows = db.execute("SELECT user_id,open_id FROM drama_admin_user WHERE name=? AND status='active'",
                                  ("郜远",)).fetchall()
        except (OSError, sqlite3.Error):
            raise DeliveryError("feishu_recipient_source_unavailable") from None
        recipients = {(('user_id', str(uid)) if uid else ('open_id', str(oid)))
                      for uid, oid in rows if uid or oid}
        if len(recipients) != 1:
            raise DeliveryError("feishu_recipient_not_unique")
        return next(iter(recipients))

    def token(self):
        if self._token and time.monotonic() < self._expires:
            return self._token
        app_id, secret = self.env.get("FEISHU_APP_ID"), self.env.get("FEISHU_APP_SECRET")
        if not app_id or not secret:
            raise DeliveryError("feishu_not_configured")
        try:
            response = self.http.post("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                                      json={"app_id": app_id, "app_secret": secret}, timeout=15)
            data = response.json()
            if response.status_code != 200 or data.get("code") != 0 or not data.get("tenant_access_token"):
                raise DeliveryError("feishu_token_rejected")
            self._token = data["tenant_access_token"]
            self._expires = time.monotonic() + max(0, int(data.get("expire", 3600)) - 120)
        except (requests.RequestException, ValueError, TypeError):
            raise DeliveryError("feishu_token_unavailable") from None
        return self._token

    def send(self, task):
        kind, recipient = self.recipient()
        token = self.token()
        try:
            response = self.http.post("https://open.feishu.cn/open-apis/im/v1/messages",
                                      params={"receive_id_type": kind},
                                      headers={"Authorization": "Bearer " + token},
                                      json={"receive_id": recipient, "msg_type": "text",
                                            "content": json.dumps({"text": alert_text(task)}, ensure_ascii=False),
                                            "uuid": "fbpa-" + task["task_id"]}, timeout=15)
            data = response.json()
        except (requests.RequestException, ValueError):
            raise DeliveryError("feishu_send_unknown", uncertain=True) from None
        if data.get("code") != 0:
            # A parsed API rejection is known; never log credential-bearing HTTP objects.
            raise DeliveryError("feishu_rejected_" + str(data.get("code", "unknown")),
                                uncertain=response.status_code >= 500)
        message_id = str((data.get("data") or {}).get("message_id") or "")
        if not message_id:
            raise DeliveryError("feishu_receipt_missing", uncertain=True)
        return message_id
