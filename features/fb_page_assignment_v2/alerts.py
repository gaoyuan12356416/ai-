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

KIND_ZH = {
    "request_conflict": "请求内容一致性校验",
    "pool_authorization": "Page 池校验",
    "auth": "接口身份认证",
    "contract": "请求参数校验",
    "timeout": "请求处理超时",
    "persistence": "本地回执读写",
    "saturation": "请求队列容量",
    "page_source": "选页基础数据读取",
    "worker_pool": "选页工作进程",
    "request_transport": "请求传输",
    "request_internal": "接口内部处理",
    "audit_sync": "审计记录同步",
    "audit_store": "本地审计账本",
    "connection": "接口连接",
}
REASON_ZH = {
    "idempotency_conflict": "同一子批标识已用于不同的 Page 池或剧目列表（包括顺序、数量）",
    "unauthorized": "接口身份认证未通过",
    "body_too_large": "请求正文超过大小限制",
    "invalid_json": "请求正文不是有效的 JSON",
    "body_must_be_object": "请求正文必须是对象",
    "external_task_id_required_1_to_128_characters": "缺少有效子批标识，长度需为 1 至 128 个字符",
    "drama_ids_required_1_to_100_items": "剧目列表需包含 1 至 100 个条目",
    "drama_id_must_be_nonblank_string_at_most_128_characters": "剧目 ID 需为非空字符串，且不超过 128 个字符",
    "request_identifier_encoding_invalid": "请求标识包含无效字符编码",
    "page_group_id_required": "缺少有效的 Page 池 ID",
    "publication_pool_unavailable": "Page 池不存在、已停用或不属于支持的产品",
    "publication_pool_not_authorized": "Page 池与发布任务允许的范围不一致",
    "publication_task_unverifiable": "无法核实发布任务",
    "receipt_lookup_failed": "读取已保存的选页回执失败",
    "receipt_save_failed": "保存选页回执失败",
    "queue_full": "选页请求队列已满",
    "process_pool_broken": "选页工作进程异常退出",
    "request_deadline": "选页请求处理超时",
    "response_deadline": "接口响应等待超时",
    "selection_deadline": "选页计算或数据读取超时",
    "selection_unavailable": "选页计算暂时不可用",
    "selection_dependency_unavailable": "选页依赖的数据或服务暂时不可用",
    "drama_theme_lookup_failed": "读取剧目画像标签失败",
    "resource_row_limit": "基础数据数量超过单次读取上限",
    "handler_failure": "接口处理发生内部异常",
    "connect_timeout": "连接选页接口超时",
    "connection_refused": "选页接口拒绝连接",
    "peer_disconnected": "对端提前断开连接",
    "TimeoutError": "等待操作完成超时",
    "ConnectionError": "连接异常中断",
    "ConnectionResetError": "连接被对端重置",
    "BrokenPipeError": "连接已断开，无法继续传输",
    "RemoteProtocolError": "收到不符合协议的请求或响应",
    "LocalProtocolError": "本地请求或响应协议处理异常",
    "OperationalError": "数据库操作失败",
    "InterfaceError": "数据库连接异常",
    "IntegrityError": "数据库记录完整性校验失败",
    "ReadbackMismatch": "审计回读结果与原始回执不一致",
    "OSError": "系统读写或网络操作失败",
    "unspecified": "未提供具体原因，需核查服务日志",
}


def chinese_code(value, labels, fallback):
    code = safe_code(value)
    if code in labels:
        return labels[code]
    # Database errors carry a numeric suffix; preserve it for diagnosis.
    prefix, _, number = code.rpartition(".")
    if prefix in labels and number.isdecimal():
        return "%s（错误码 %s）" % (labels[prefix], number)
    return "%s（代码：%s）" % (fallback, code)


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
             "故障环节：%s；原因：%s" % (
                 chinese_code(payload["kind"], KIND_ZH, "未识别的故障环节"),
                 chinese_code(payload["reason"], REASON_ZH, "未识别的异常，请核查服务日志")),
             "累计影响 %s 个子批或未识别调用" % payload["affected"]]
    if payload["mode"] == "summary":
        lines.append("本次新增异常 %s 次，新增受影响子批 %s 个" % (payload["new_occurrences"], payload["since_last"]))
    for example in payload["examples"]:
        external = "".join(ch for ch in example["external_task_id"] if ch.isprintable()) or "未知"
        lines.append("子批 %s，Page 池 %s，原因：%s" % (
            external, example["page_group_id"] or "未知",
            chinese_code(example["detail"], REASON_ZH, "未识别的异常，请核查服务日志")))
    if payload["reason"] == "idempotency_conflict" and payload["mode"] in ("first", "summary"):
        lines.append("处理建议：同一子批重试时保持 Page 池和剧目列表完全一致；拆分后的不同子批使用各自固定且唯一的请求标识。")
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
