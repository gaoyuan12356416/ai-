"""Durable, per-account KOC registration; ambiguous writes are never replayed."""
from contextlib import contextmanager
import hashlib
import json
import sqlite3
import threading
import uuid

from .content_tools import atomic_json
from .media_sources import MediaSource, PLATFORMS, account_id, platform, profile_link
from .promotions import identity, now, PromotionError

PUBLIC = ("id", "platform_type", "platform_name", "account_id", "nickname", "platform_link",
          "status", "message", "provider_id", "provider_create_time", "created_at", "updated_at", "star_name")


class MediaAccounts:
    def __init__(self, settings, db, provider, source=None, autostart=True):
        self.settings, self.db, self.provider = settings, db, provider
        self.source = source or MediaSource(db)
        self.root = settings.data_dir / "media-accounts"
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "registrations.sqlite3"
        self.worker_lock, self.mirror_lock = threading.Lock(), threading.Lock()
        self.autostart = autostart
        with self.connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS registrations (
                    id TEXT PRIMARY KEY, tenant_key TEXT NOT NULL, user_id TEXT NOT NULL,
                    payload TEXT NOT NULL, mirror_pending INTEGER NOT NULL DEFAULT 1
                );
                CREATE INDEX IF NOT EXISTS registrations_owner ON registrations(tenant_key,user_id);
                CREATE TABLE IF NOT EXISTS audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, registration_id TEXT NOT NULL,
                    event TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL
                );
            """)
            # Started requests without a persisted acknowledgement have an unknown outcome.
            for stored in conn.execute("SELECT payload FROM registrations").fetchall():
                row = json.loads(stored[0])
                if row["status"] != "submitting":
                    continue
                receipt = self.root / "receipts" / (row["request_id"] + ".json")
                try:
                    envelope = json.loads(receipt.read_text(encoding="utf-8"))
                    self._apply_receipt(row, envelope)
                except (OSError, ValueError):
                    row.update(status="unknown", message="提交结果待核实，不会自动重复报备", updated_at=now())
                self._save(conn, row)
        self.kick()

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(str(self.path), timeout=15, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        try:
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _save(conn, row):
        conn.execute("""INSERT INTO registrations(id,tenant_key,user_id,payload,mirror_pending) VALUES (?,?,?,?,1)
            ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,mirror_pending=1""",
            (row["id"], row["tenant_key"], row["user_id"], json.dumps(row, ensure_ascii=False)))

    def preview(self, actor, body):
        identity(self.settings.institution_user_id, actor)
        if set(body) != {"platform_type", "account_ids"}:
            raise ValueError("仅支持媒体类型和账号 ID")
        kind = platform(body["platform_type"])
        rows = self.source.lookup(kind, body["account_ids"])
        return {"list": rows, "platform_type": kind, "platform_name": PLATFORMS[kind],
                "duplicates_removed": len(body["account_ids"]) - len(rows)}

    def submit(self, actor, body):
        star, tenant, user, name = identity(self.settings.institution_user_id, actor)
        if set(body) != {"platform_type", "accounts"}:
            raise ValueError("请求字段无效；达人身份由登录用户确定")
        kind, accounts = platform(body["platform_type"]), body["accounts"]
        if not isinstance(accounts, list) or not 1 <= len(accounts) <= 100:
            raise ValueError("每批提交 1 至 100 个账号")
        prepared, seen = [], set()
        for item in accounts:
            if not isinstance(item, dict) or set(item) != {"account_id", "nickname", "platform_link"}:
                raise ValueError("账号字段无效")
            key = account_id(item["account_id"])
            if key in seen:
                continue
            seen.add(key)
            nickname = str(item["nickname"] or "").strip()
            if not nickname or len(nickname) > 255 or any(ord(c) < 32 for c in nickname):
                raise ValueError("请补全账号名称，长度不超过 255 个字符")
            prepared.append({"account_id": key, "nickname": nickname, "platform_link": profile_link(kind, item["platform_link"])})
        # Resolve again server-side. The browser cannot substitute another profile for a known ID.
        known = {r["account_id"]: r for r in self.source.lookup(kind, [r["account_id"] for r in prepared])}
        for item in prepared:
            matched = known[item["account_id"]]
            if matched["match_status"] == "conflict":
                raise ValueError("账号 %s 匹配到冲突资料，请先核对账号库" % item["account_id"])
            if matched["match_status"] == "matched" and any(item[k] != matched[k] for k in ("nickname", "platform_link")):
                raise ValueError("账号资料已变化，请重新匹配后提交")
            item["metadata_source"] = "catalog" if matched["match_status"] == "matched" else "operator"
        results = []
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for item in prepared:
                    key = hashlib.sha256(json.dumps([self.settings.institution_user_id, kind, item["account_id"]]).encode()).hexdigest()
                    old = conn.execute("SELECT payload FROM registrations WHERE id=?", (key,)).fetchone()
                    if old:
                        row = json.loads(old[0])
                        if row["tenant_key"] != tenant or row["user_id"] != user:
                            results.append({"account_id": item["account_id"], "status": "held", "message": "该账号已有其他达人报备记录，请联系管理员核实"})
                            continue
                        if not row.get("retry_verified"):
                            results.append({**self.public(row), "reused": True})
                            continue
                    timestamp = now()
                    row = dict(item, id=key, institution_user_id=self.settings.institution_user_id, project_type=2,
                               platform_type=kind, platform_name=PLATFORMS[kind], tenant_key=tenant, user_id=user,
                               star_user_id=star, star_name=name, status="queued", message="等待提交",
                               request_id=uuid.uuid4().hex, created_at=timestamp, updated_at=timestamp,
                               provider_id=None, provider_create_time=None)
                    row["request"] = {"project_type": 2, "nickname": item["nickname"], "platform_type": kind,
                                      "platform_link": item["platform_link"], "star_user_id": star}
                    self._save(conn, row)
                    conn.execute("INSERT INTO audit(registration_id,event,payload,created_at) VALUES (?,?,?,?)",
                                 (key, "queued", json.dumps(row, ensure_ascii=False), timestamp))
                    results.append({**self.public(row), "reused": False})
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        self.kick()
        return {"list": results}

    @staticmethod
    def public(row):
        return {key: row.get(key) for key in PUBLIC}

    def list(self, actor, query):
        _, tenant, user, _ = identity(self.settings.institution_user_id, actor)
        page, size = int(query.get("page_index", 1)), 50
        if not 1 <= page <= 100000:
            raise ValueError("页码无效")
        with self.connect() as conn:
            if actor.get("role") == "admin":
                values = conn.execute("SELECT payload FROM registrations WHERE tenant_key=?", (tenant,)).fetchall()
            else:
                values = conn.execute("SELECT payload FROM registrations WHERE tenant_key=? AND user_id=?", (tenant, user)).fetchall()
        rows = [json.loads(r[0]) for r in values]
        search = str(query.get("q", "")).casefold().strip()[:200]
        rows = [r for r in rows if (not query.get("platform_type") or str(r["platform_type"]) == str(query["platform_type"]))
                and (not query.get("status") or r["status"] == query["status"])
                and (not search or search in (r["account_id"] + " " + r["nickname"]).casefold())]
        rows.sort(key=lambda r: (r["created_at"], r["id"]), reverse=True)
        return {"list": [self.public(r) for r in rows[(page-1)*size:page*size]], "total": len(rows),
                "page_index": page, "page_size": size}

    def kick(self):
        if self.autostart:
            threading.Thread(target=self.work, name="moboreels-media-registration", daemon=True).start()

    def work(self):
        if not self.worker_lock.acquire(blocking=False):
            return
        try:
            while True:
                with self.connect() as conn:
                    values = conn.execute("SELECT payload FROM registrations").fetchall()
                    row = next((r for r in (json.loads(v[0]) for v in values) if r["status"] == "queued"), None)
                    if row is None:
                        break
                    row.update(status="submitting", message="正在提交报备", updated_at=now())
                    self._save(conn, row)
                try:
                    response = self.provider._post("/api/story/addplatformaccount", row["request"], row["request_id"])
                    try:
                        payload = response.json()
                    except ValueError:
                        payload = None
                    envelope = {"http_status": response.status_code, "response": payload, "received_at": now()}
                    atomic_json(self.root / "receipts" / (row["request_id"] + ".json"), envelope)
                    self._apply_receipt(row, envelope)
                except Exception:
                    row.update(status="unknown", message="提交结果待核实，不会自动重复报备", updated_at=now())
                with self.connect() as conn:
                    self._save(conn, row)
                self.flush_outbox()
        finally:
            self.worker_lock.release()
            # A submission may have arrived just after the final queue read.
            if self.autostart:
                with self.connect() as conn:
                    pending = any(json.loads(r[0])["status"] == "queued" for r in conn.execute("SELECT payload FROM registrations"))
                if pending:
                    self.kick()

    @staticmethod
    def _apply_receipt(row, envelope):
        row.update(response=envelope, updated_at=now(), status="unknown", message="供应方响应需核实，不会重复报备")
        body = envelope.get("response")
        if envelope.get("http_status") != 200 or not isinstance(body, dict):
            return
        if body.get("code") != 200:
            row.update(status="failed", message="供应方拒绝本次提交（业务码 %s），请核实后重试" % str(body.get("code"))[:32])
            return
        data = body.get("data")
        try:
            valid = (isinstance(data, dict) and str(data.get("id", "")).isdigit() and int(data["id"]) > 0
                     and int(data.get("project_type", 0)) == 2 and int(data.get("platform_type", 0)) == row["platform_type"]
                     and data.get("nickname") == row["nickname"]
                     and profile_link(row["platform_type"], data.get("platform_link")) == row["platform_link"])
        except (ValueError, TypeError):
            valid = False
        if valid:
            row.update(status="succeeded", message="报备成功", provider_id=str(data["id"]), provider_create_time=data.get("create_time"))

    def resolve(self, actor, record_id, body):
        _, tenant, _, _ = identity(self.settings.institution_user_id, actor)
        if actor.get("role") != "admin":
            raise PromotionError("仅管理员可以核实结果", "permission_denied", 403)
        if set(body) - {"outcome", "provider_id", "evidence"} or body.get("outcome") not in {"succeeded", "not_created"}:
            raise ValueError("核实结果无效")
        evidence = str(body.get("evidence") or "").strip()
        if not 5 <= len(evidence) <= 1000:
            raise ValueError("请填写 5 至 1000 字的核实依据")
        if body["outcome"] == "succeeded" and (not str(body.get("provider_id", "")).isdigit() or int(body["provider_id"]) <= 0):
            raise ValueError("请填写供应方报备记录 ID")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            raw = conn.execute("SELECT payload FROM registrations WHERE id=? AND tenant_key=?", (record_id, tenant)).fetchone()
            if not raw:
                raise PromotionError("未找到报备记录", "not_found", 404)
            row = json.loads(raw[0])
            if row["status"] not in {"unknown", "failed"}:
                raise PromotionError("当前记录不需要核实", "invalid_state", 409)
            if body["outcome"] == "succeeded":
                row.update(status="succeeded", message="管理员核实报备成功", provider_id=str(body["provider_id"]))
            else:
                row.update(status="failed", message="已核实未创建，可重新提交", retry_verified=True)
            row["updated_at"] = now()
            self._save(conn, row)
            conn.execute("INSERT INTO audit(registration_id,event,payload,created_at) VALUES (?,?,?,?)",
                         (record_id, "resolved", json.dumps({"actor": actor, **body}, ensure_ascii=False), now()))
            conn.commit()
        return {"registration": self.public(row)}

    def flush_outbox(self):
        if not self.mirror_lock.acquire(blocking=False):
            return
        try:
            with self.connect() as conn:
                values = conn.execute("SELECT id,payload FROM registrations WHERE mirror_pending=1 LIMIT 20").fetchall()
            for key, payload in values:
                try:
                    self.db.save_media_registration(json.loads(payload))
                except Exception:
                    return  # Durable SQLite and response receipts remain authoritative until MySQL recovers.
                with self.connect() as conn:
                    conn.execute("UPDATE registrations SET mirror_pending=0 WHERE id=? AND payload=?", (key, payload))
        finally:
            self.mirror_lock.release()
