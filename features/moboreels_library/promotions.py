"""Durable per-user creation claims and response receipts."""
import datetime
from contextlib import contextmanager
import hashlib
import json
import logging
import os
from pathlib import Path
import sqlite3
import threading
import uuid
from urllib.parse import urlsplit


def now():
    return datetime.datetime.utcnow().isoformat(timespec="milliseconds") + "Z"


class PromotionError(RuntimeError):
    def __init__(self, message, code="promotion_unavailable", status=503):
        super().__init__(message)
        self.code, self.status = code, status


def identity(institution, actor):
    tenant = str(actor.get("tenant_key") or "").strip()
    user = str(actor.get("user_id") or "").strip()
    name = str(actor.get("name") or "").strip()
    if not tenant or not user or not name or len(tenant) > 128 or len(user) > 128 or len(name) > 255:
        raise PromotionError("登录用户身份不完整，请重新登录", "invalid_actor", 401)
    raw = json.dumps(["moboreels", institution, tenant, user], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:32], tenant, user, name


def interpret(envelope, story_id):
    body = envelope.get("response")
    if envelope.get("http_status") == 200 and isinstance(body, dict):
        if body.get("code") == 200:
            data = body.get("data") or {}
            if not isinstance(data, dict):
                return "unknown", {}
            try:
                good = (str(data.get("story_id")) == str(story_id) and int(data.get("project_type")) == 2
                        and bool(data.get("koc_code")) and len(str(data["koc_code"])) <= 64
                        and all(urlsplit(str(data.get(k) or "")).scheme in ("https", "http")
                                and urlsplit(str(data[k])).netloc
                                and not urlsplit(str(data[k])).username
                                and not urlsplit(str(data[k])).password for k in ("deep_link", "home_link")))
            except (ValueError, TypeError, KeyError):
                good = False
            if good:
                return "succeeded", {k: str(data[k]) for k in ("koc_code", "deep_link", "home_link")}
            return "unknown", {}
        # No documented error code proves that creation did not occur.
        # Only an operator's verified not-created decision permits a retry.
    return "unknown", {}


class PromotionManager:
    def __init__(self, settings, db, provider):
        self.settings, self.db, self.provider = settings, db, provider
        self.root = Path(settings.data_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self.receipts = self.root / "promotion-receipts"
        self.receipts.mkdir(exist_ok=True)
        self.path = self.root / "promotions.sqlite3"
        self.mirror_lock = threading.Lock()
        with self.connect() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS promotions (
                    identity_key TEXT PRIMARY KEY, record_json TEXT NOT NULL,
                    mirror_pending INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, identity_key TEXT NOT NULL,
                    event TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL
                );
            """)

    @contextmanager
    def connect(self):
        c = sqlite3.connect(str(self.path), timeout=15, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=FULL")
        try:
            yield c
        finally:
            c.close()

    def key(self, star, story):
        return self.settings.institution_user_id + ":" + star + ":2:" + str(story)

    def _load(self, c, key):
        row = c.execute("SELECT record_json FROM promotions WHERE identity_key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def _save(self, c, key, record, pending=1):
        c.execute("INSERT INTO promotions(identity_key,record_json,mirror_pending) VALUES(?,?,?) "
                  "ON CONFLICT(identity_key) DO UPDATE SET record_json=excluded.record_json,mirror_pending=excluded.mirror_pending",
                  (key, json.dumps(record, ensure_ascii=False), pending))

    def _audit(self, c, key, event, payload):
        c.execute("INSERT INTO audit(identity_key,event,payload_json,created_at) VALUES(?,?,?,?)",
                  (key, event, json.dumps(payload, ensure_ascii=False), now()))

    def _receipt(self, request_id, envelope):
        path = self.receipts / (request_id + ".json")
        temporary = path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as f:
            json.dump(envelope, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(temporary), str(path))
        if hasattr(os, "O_DIRECTORY"):
            fd = os.open(str(self.receipts), os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    def recover(self):
        """Called only after the service-wide process lock is held."""
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                for row in c.execute("SELECT identity_key,record_json FROM promotions").fetchall():
                    record = json.loads(row["record_json"])
                    if record["status"] != "creating":
                        continue
                    path = self.receipts / (record["request_id"] + ".json")
                    envelope = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"transport_error": "interrupted"}
                    status, result = interpret(envelope, record["story_id"])
                    record.update(status=status, response_json=envelope, updated_at=now(), **result)
                    self._save(c, row["identity_key"], record)
                    self._audit(c, row["identity_key"], "recovered", {"status": status, "request_id": record["request_id"]})
                c.commit()
            except Exception:
                c.rollback()
                raise

    def _read_record(self, actor, story):
        star, _, _, _ = identity(self.settings.institution_user_id, actor)
        key = self.key(star, story)
        with self.connect() as c:
            record = self._load(c, key)
        if record is not None:
            return key, record
        # Do not create when an older durable DB result cannot be checked.
        try:
            record = self.db.get_promotion(star, str(story))
        except Exception:
            raise PromotionError("推广记录暂时无法读取，请稍后重试") from None
        if record:
            for field in ("created_at", "updated_at"):
                value = record.get(field)
                if isinstance(value, (datetime.datetime, str)) and value:
                    try:
                        parsed = value if isinstance(value, datetime.datetime) else datetime.datetime.fromisoformat(
                            value[:-1] + "+00:00" if value.endswith("Z") else value)
                        if parsed.tzinfo is None:
                            parsed = parsed.replace(tzinfo=datetime.timezone.utc)
                        record[field] = parsed.astimezone(datetime.timezone.utc).isoformat(
                            timespec="milliseconds").replace("+00:00", "Z")
                    except ValueError:
                        pass
            if record.get("status") == "creating":
                record["status"] = "unknown"
            with self.connect() as c:
                c.execute("BEGIN IMMEDIATE")
                current = self._load(c, key)
                if current is None:
                    self._save(c, key, record, pending=record.get("status") == "unknown")
                else:
                    record = current
                c.commit()
        return key, record

    @staticmethod
    def public(record):
        if record is None:
            return None
        fields = ("status", "story_id", "star_name", "koc_code", "deep_link", "home_link", "created_at", "updated_at")
        result = {k: record.get(k) for k in fields}
        result["message"] = {
            "creating": "正在生成推广链接，请稍候", "unknown": "请求结果待核对，请联系管理员，暂不重复创建",
            "rejected": "供应方未接受本次请求，可稍后重试", "succeeded": "推广链接已生成",
        }.get(record.get("status"), "")
        return result

    def get(self, actor, story):
        _, record = self._read_record(actor, story)
        return self.public(record)

    def statuses(self, actor, story_ids):
        star, _, _, _ = identity(self.settings.institution_user_id, actor)
        keys = [self.key(star, s) for s in story_ids]
        if not keys:
            return {}
        with self.connect() as c:
            rows = c.execute("SELECT record_json FROM promotions WHERE identity_key IN (" + ",".join("?" for _ in keys) + ")", keys).fetchall()
        return {r["story_id"]: r["status"] for r in (json.loads(row[0]) for row in rows)}

    def create(self, actor, story):
        key, record = self._read_record(actor, story)
        if record and record["status"] in ("succeeded", "creating", "unknown"):
            return {"promotion": self.public(record), "reused": True}
        drama = self.db.get_drama(str(story))
        if not drama:
            raise PromotionError("未找到该短剧", "drama_not_found", 404)
        if int(drama.get("is_show") or 0) != 1:
            raise PromotionError("该短剧已下架，不能新建推广链接；已有链接仍可查看", "drama_unavailable", 409)
        star, tenant, user, name = identity(self.settings.institution_user_id, actor)
        request_id = uuid.uuid4().hex
        body = {"story_id": str(story), "star_user_id": star, "star_name": name, "project_type": 2}
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            current = self._load(c, key)
            if current and current["status"] in ("succeeded", "creating", "unknown"):
                c.commit()
                return {"promotion": self.public(current), "reused": True}
            record = {
                "institution_user_id": self.settings.institution_user_id, "star_user_id": star,
                "project_type": 2, "story_id": str(story), "tenant_key": tenant, "user_id": user,
                "star_name": name, "status": "creating", "request_id": request_id,
                "koc_code": None, "deep_link": None, "home_link": None,
                "request_json": body, "response_json": None,
                "created_at": current["created_at"] if current else now(), "updated_at": now(),
            }
            self._save(c, key, record)
            self._audit(c, key, "creation_intent", {"request_id": request_id, "request": body})
            c.commit()
        try:
            envelope = self.provider.create_promotion(body, request_id)
        except Exception as exc:
            envelope = {"transport_error": type(exc).__name__}
        # Persist response before any DB mirroring or frontend acknowledgement.
        self._receipt(request_id, envelope)
        status, result = interpret(envelope, story)
        record.update(status=status, response_json=envelope, updated_at=now(), **result)
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            self._save(c, key, record)
            self._audit(c, key, "creation_result", {"request_id": request_id, "status": status})
            c.commit()
        # Mirroring is done by the background outbox worker: upstream success is final here.
        return {"promotion": self.public(record), "reused": False}

    def flush_outbox(self):
        if not self.mirror_lock.acquire(blocking=False):
            return
        try:
            with self.connect() as c:
                rows = c.execute("SELECT identity_key,record_json FROM promotions WHERE mirror_pending=1 LIMIT 20").fetchall()
            for row in rows:
                record = json.loads(row["record_json"])
                if record["status"] == "creating":
                    continue
                try:
                    self.db.mirror_promotion(record)
                except Exception as exc:
                    logging.warning("promotion mirror deferred: %s", type(exc).__name__)
                    return
                with self.connect() as c:
                    c.execute("UPDATE promotions SET mirror_pending=0 WHERE identity_key=? AND record_json=?",
                              (row["identity_key"], row["record_json"]))
        finally:
            self.mirror_lock.release()

    def resolve(self, star, story, decision, evidence, result=None, operator=""):
        """Local operator CLI only, after checking the upstream KOC platform."""
        if not evidence.strip() or not operator.strip():
            raise PromotionError("必须记录核对依据和处理人", "resolution_evidence_required", 400)
        key = self.key(star, story)
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            record = self._load(c, key)
            if not record or record["status"] != "unknown":
                c.rollback()
                raise PromotionError("仅可核对未知结果记录", "resolution_conflict", 409)
            if decision == "created":
                envelope = {"http_status": 200, "response": {"code": 200, "data": dict(result or {}, story_id=str(story), project_type=2)}}
                status, values = interpret(envelope, story)
                if status != "succeeded":
                    c.rollback()
                    raise PromotionError("必须补录有效的原口令和两种链接", "invalid_resolution", 400)
                record.update(status=status, response_json=envelope, **values)
            elif decision == "not-created":
                record.update(status="rejected", response_json={"confirmed_not_created": True})
            else:
                c.rollback()
                raise PromotionError("无效的核对结论", "invalid_resolution", 400)
            record["updated_at"] = now()
            self._audit(c, key, "operator_resolution", {"decision": decision, "evidence": evidence, "operator": operator, "previous_request_id": record["request_id"]})
            self._save(c, key, record)
            c.commit()
        return self.public(record)
