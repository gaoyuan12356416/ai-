"""Hong Kong event relay and CPU-owned incident aggregation."""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib import request as http
from urllib.error import HTTPError

from .ledger import encoded, safe_code

ROUTE = "/api/integrations/v2/drama-page-alerts"


class _NoRedirect(http.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Relay:
    def __init__(self, url=None, token=None):
        self.url = url or os.environ.get("FB_PAGE_V2_ALERT_URL", "https://ai.yingliangads.com" + ROUTE)
        self.token = token or os.environ.get("FB_PAGE_ASSIGNMENT_API_TOKEN", "")
        self.http = http.build_opener(_NoRedirect)

    def push(self, events):
        if not self.token:
            raise ValueError("missing_alert_relay_bearer")
        message = http.Request(self.url, data=encoded({"events": events}).encode(), method="POST",
                               headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.token})
        try:
            response = self.http.open(message, timeout=5)
        except HTTPError as exc:
            exc.close()
            raise ValueError("alert_relay_http_%d" % exc.code) from None
        with response:
            result = json.loads(response.read(16385))
            if response.status != 200 or result.get("accepted") != [e["id"] for e in events]:
                raise ValueError("missing_alert_relay_receipt")


def validate(payload, now):
    events = payload.get("events") if isinstance(payload, dict) else None
    if not isinstance(events, list) or not 1 <= len(events) <= 50:
        raise ValueError("invalid_events")
    checked = []
    for event in events:
        if not isinstance(event, dict):
            raise ValueError("invalid_event")
        key, kind, action = event.get("id"), event.get("kind"), event.get("action")
        group, external = event.get("page_group_id"), event.get("external_task_id", "")
        at = event.get("at", now)
        if (not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", key)
                or not isinstance(kind, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", kind)
                or action not in ("fault", "recovered")
                or not isinstance(external, str) or len(external) > 128
                or (group is not None and (type(group) is not int or not 0 < group <= 4294967295))
                or type(at) not in (int, float) or not math.isfinite(at) or at <= 0):
            raise ValueError("invalid_event_fields")
        checked.append(dict(id=key, kind=kind, action=action, at=min(float(at), now + 60),
                            external_task_id=external, page_group_id=group, detail=safe_code(event.get("detail"))))
    return checked


class AlertHub:
    def __init__(self, root, clock=time.time):
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path, self.clock = root / "v2-incidents.sqlite3", clock
        with self.db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS inbox(id TEXT PRIMARY KEY,at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS healthy_through(kind TEXT PRIMARY KEY,at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS episode(
                    id TEXT PRIMARY KEY,kind TEXT NOT NULL,reason TEXT NOT NULL,start REAL NOT NULL,last REAL NOT NULL,
                    closed REAL,affected INTEGER NOT NULL DEFAULT 0,announced INTEGER NOT NULL DEFAULT 0,
                    occurrences INTEGER NOT NULL DEFAULT 0,announced_occurrences INTEGER NOT NULL DEFAULT 0,
                    due REAL NOT NULL,example TEXT NOT NULL DEFAULT '[]');
                CREATE UNIQUE INDEX IF NOT EXISTS open_reason ON episode(kind,reason) WHERE closed IS NULL;
                CREATE TABLE IF NOT EXISTS affected(
                    incident TEXT NOT NULL,subbatch TEXT NOT NULL,PRIMARY KEY(incident,subbatch));
                CREATE TABLE IF NOT EXISTS message(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT NOT NULL UNIQUE,incident TEXT NOT NULL,
                    mode TEXT NOT NULL,payload TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'pending',
                    tries INTEGER NOT NULL DEFAULT 0,next_try REAL NOT NULL,receipt TEXT,last_error TEXT);
                CREATE INDEX IF NOT EXISTS delivery_due ON message(state,next_try,seq);
            """)

    @contextmanager
    def db(self, write=False):
        conn = sqlite3.connect(str(self.path), timeout=5, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA synchronous=FULL")
            if write:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            if write:
                conn.commit()
        finally:
            conn.close()

    def interrupted(self):
        with self.db(True) as db:
            db.execute("UPDATE message SET state='unknown',last_error='interrupted_send' WHERE state='sending'")

    def _message(self, db, eid, mode, now):
        row = db.execute("SELECT * FROM episode WHERE id=?", (eid,)).fetchone()
        note = {"kind": row["kind"], "reason": row["reason"], "mode": mode, "affected": row["affected"],
                "occurrences": row["occurrences"], "new_occurrences": row["occurrences"]-row["announced_occurrences"],
                "since_last": row["affected"] - row["announced"], "started": row["start"],
                "at": now, "examples": json.loads(row["example"])}
        db.execute("INSERT INTO message(id,incident,mode,payload,next_try) VALUES(?,?,?,?,?)",
                   (uuid.uuid4().hex, eid, mode, encoded(note), now))
        db.execute("UPDATE episode SET announced=affected,announced_occurrences=occurrences,due=? WHERE id=?",
                   (now + 300, eid))

    def accept(self, payload):
        now = self.clock()
        events = validate(payload, now)
        with self.db(True) as db:
            for event in events:
                if not db.execute("INSERT OR IGNORE INTO inbox(id,at) VALUES(?,?)", (event["id"], now)).rowcount:
                    continue
                kind, at = event["kind"], event["at"]
                if event["action"] == "recovered":
                    db.execute("INSERT INTO healthy_through VALUES(?,?) ON CONFLICT(kind) "
                               "DO UPDATE SET at=max(at,excluded.at)", (kind, at))
                    for ep in db.execute("SELECT id FROM episode WHERE kind=? AND closed IS NULL AND last<=?",
                                         (kind, at)).fetchall():
                        db.execute("UPDATE episode SET closed=? WHERE id=?", (at, ep["id"]))
                        self._message(db, ep["id"], "recovered", now)
                    continue
                reason = event["detail"]
                healthy = db.execute("SELECT at FROM healthy_through WHERE kind=?", (kind,)).fetchone()
                late = healthy is not None and at <= healthy[0]
                if late:
                    seen = db.execute("SELECT 1 FROM episode WHERE kind=? AND reason=? "
                                      "AND closed>=? LIMIT 1", (kind, reason, at)).fetchone()
                    if seen:
                        continue
                ep = db.execute("SELECT * FROM episode WHERE kind=? AND reason=? AND closed IS NULL",
                                (kind, reason)).fetchone() if not late else None
                if ep is None:
                    last = db.execute("SELECT max(closed) FROM episode WHERE kind=? AND reason=?",
                                      (kind, reason)).fetchone()[0]
                    if last is not None and at <= last:
                        continue
                    eid = uuid.uuid4().hex
                    db.execute("INSERT INTO episode(id,kind,reason,start,last,due,closed) VALUES(?,?,?,?,?,?,?)",
                               (eid, kind, reason, at, at, now + 300, healthy[0] if late else None))
                    examples = []
                else:
                    eid = ep["id"]
                    examples = json.loads(ep["example"])
                subbatch = event["external_task_id"] or ("event-" + event["id"])
                count = db.execute("INSERT OR IGNORE INTO affected VALUES(?,?)", (eid, subbatch)).rowcount
                if count and len(examples) < 4:
                    examples.append({k: event[k] for k in ("external_task_id", "page_group_id", "detail")})
                db.execute("UPDATE episode SET last=max(last,?),affected=affected+?,occurrences=occurrences+1,"
                           "example=? WHERE id=?",
                           (at, count, encoded(examples), eid))
                if ep is None:
                    self._message(db, eid, "late_recovered" if late else "first", now)
        return [event["id"] for event in events]

    def summaries(self):
        now = self.clock()
        with self.db() as db:
            due = db.execute("SELECT 1 FROM episode WHERE closed IS NULL AND due<=? "
                             "AND occurrences>announced_occurrences LIMIT 1",
                             (now,)).fetchone()
        if due:
            with self.db(True) as db:
                for row in db.execute("SELECT id FROM episode WHERE closed IS NULL AND due<=? "
                                      "AND occurrences>announced_occurrences",
                                      (now,)).fetchall():
                    self._message(db, row[0], "summary", now)

    def claim(self):
        now = self.clock()
        sql = """SELECT * FROM message m WHERE m.state='pending' AND m.next_try<=? AND NOT EXISTS
                 (SELECT 1 FROM message p WHERE p.incident=m.incident AND p.seq<m.seq
                  AND p.state IN ('pending','sending')) ORDER BY m.seq LIMIT 1"""
        with self.db() as db:
            if not db.execute(sql, (now,)).fetchone():
                return None
        with self.db(True) as db:
            row = db.execute(sql, (now,)).fetchone()
            if not row:
                return None
            db.execute("UPDATE message SET state='sending',tries=tries+1 WHERE id=?", (row["id"],))
            return dict(id=row["id"], payload=json.loads(row["payload"]), tries=row["tries"] + 1)

    def finish(self, msg, state, receipt="", error=""):
        delay = min(300, 2 ** min(msg["tries"], 8))
        with self.db(True) as db:
            db.execute("""UPDATE message SET state=?,receipt=?,last_error=?,next_try=? WHERE id=?
                          AND state='sending'""", (state, receipt, error, self.clock() + delay, msg["id"]))

    def status(self):
        with self.db() as db:
            return dict(db.execute("SELECT state,count(*) FROM message GROUP BY state"))


def text_for(payload):
    bj = datetime.fromtimestamp(payload["at"], timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    phase = {"first": "异常", "summary": "异常汇总", "recovered": "已恢复",
             "late_recovered": "已恢复（延迟到达的异常）"}[payload["mode"]]
    lines = ["香港 Page 画像 V2 " + phase, "北京时间 " + bj,
             "故障环节 %s，原因 %s" % (payload["kind"], payload["reason"]),
             "累计影响 %s 个子批或未识别调用" % payload["affected"]]
    if payload["mode"] == "summary":
        lines.append("本次新增异常 %s 次，新增受影响子批 %s 个" % (payload["new_occurrences"], payload["since_last"]))
    for example in payload["examples"]:
        external = "".join(ch for ch in example["external_task_id"] if ch.isprintable()) or "未知"
        lines.append("子批 %s，Page 池 %s，原因 %s" % (external, example["page_group_id"] or "未知", example["detail"]))
    return "\n".join(lines)


class FeishuSender:
    def __init__(self):
        from features.fb_page_assignment.notify import Notifier
        self.app = Notifier(os.environ)

    def send(self, msg):
        import requests
        from features.fb_page_assignment.notify import DeliveryError
        kind, target = self.app.recipient()
        token = self.app.token()
        try:
            response = self.app.http.post("https://open.feishu.cn/open-apis/im/v1/messages",
                params={"receive_id_type": kind}, headers={"Authorization": "Bearer " + token},
                json={"receive_id": target, "msg_type": "text", "uuid": "v2-" + msg["id"],
                      "content": encoded({"text": text_for(msg["payload"])})}, timeout=8)
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise DeliveryError("feishu_send_unknown", uncertain=True) from exc
        if response.status_code != 200 or data.get("code") != 0:
            raise DeliveryError("feishu_rejected_" + str(data.get("code", 0)), uncertain=response.status_code >= 500)
        mid = str((data.get("data") or {}).get("message_id") or "")
        if not mid:
            raise DeliveryError("feishu_receipt_missing", uncertain=True)
        return mid
