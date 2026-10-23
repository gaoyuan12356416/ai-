"""Owner-scoped personal X material pools; no platform writes or credentials.

All source and identity callbacks run before SQLite write locks. A scheduler
may freeze exactly one head item with the core's commit(conn=...) hook.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from features.x_posts.service import XPostError

UTC = timezone.utc
BJ = timezone(timedelta(hours=8))
ID = re.compile(r"[1-9][0-9]{0,29}\Z")
MODES = {"original": "original", "random": "random", "random_overlay": "random"}


def stamp(value):
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def parse_time(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def subject(actor):
    tenant, user = str(actor.get("tenant_key") or ""), str(actor.get("user_id") or "")
    if not tenant or not user:
        raise XPostError("invalid_actor", "需要有效的当前用户身份", status=403)
    if actor.get("x_platform_access") is True and actor.get("_pool_subject"):
        return tuple(actor["_pool_subject"])
    return tenant, user


def id_text(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not ID.fullmatch(str(value)):
        raise XPostError("invalid_id", "ID必须为完整的正整数字符串")
    return str(value)


def language(value):
    text = str(value or "").strip().replace("_", "-").lower()
    return "ja" if text == "jp" else text


def install_schema(conn):
    conn.executescript("""
      CREATE TABLE IF NOT EXISTS x_video_publish_pool(
        tenant_key TEXT NOT NULL,user_id TEXT NOT NULL,account_id INTEGER NOT NULL,
        real_account_id TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'paused',times_json TEXT NOT NULL DEFAULT '[]',
        processing_mode TEXT NOT NULL DEFAULT 'original',language TEXT NOT NULL,
        pause_reason TEXT NOT NULL DEFAULT '',effective_at_utc TEXT NOT NULL,
        created_at_utc TEXT NOT NULL,updated_at_utc TEXT NOT NULL,
        PRIMARY KEY(tenant_key,user_id,account_id));
      CREATE TABLE IF NOT EXISTS x_video_publish_pool_item(
        id INTEGER PRIMARY KEY AUTOINCREMENT,tenant_key TEXT NOT NULL,user_id TEXT NOT NULL,
        account_id INTEGER NOT NULL,material_id TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',
        position INTEGER NOT NULL,material_name TEXT NOT NULL,language TEXT NOT NULL,
        material_json TEXT NOT NULL,task_id TEXT NOT NULL DEFAULT '',batch_id TEXT NOT NULL DEFAULT '',
        reason TEXT NOT NULL DEFAULT '',created_at_utc TEXT NOT NULL,updated_at_utc TEXT NOT NULL);
      CREATE UNIQUE INDEX IF NOT EXISTS uq_x_video_pool_pending
        ON x_video_publish_pool_item(tenant_key,user_id,account_id,material_id)
        WHERE status IN ('pending','processing','unknown');
      CREATE INDEX IF NOT EXISTS idx_x_video_pool_order
        ON x_video_publish_pool_item(tenant_key,user_id,account_id,status,position,id);
      CREATE TABLE IF NOT EXISTS x_video_publish_pool_slot(
        id INTEGER PRIMARY KEY AUTOINCREMENT,tenant_key TEXT NOT NULL,user_id TEXT NOT NULL,
        account_id INTEGER NOT NULL,real_account_id TEXT NOT NULL,bj_date TEXT NOT NULL,
        slot_time TEXT NOT NULL,at_utc TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL DEFAULT '',
        item_id INTEGER,material_id TEXT NOT NULL DEFAULT '',task_id TEXT NOT NULL DEFAULT '',
        batch_id TEXT NOT NULL DEFAULT '',created_at_utc TEXT NOT NULL,
        UNIQUE(tenant_key,user_id,real_account_id,bj_date,slot_time));
      CREATE TABLE IF NOT EXISTS x_video_publish_pool_preview(
        preview_id TEXT PRIMARY KEY,tenant_key TEXT NOT NULL,user_id TEXT NOT NULL,
        account_id INTEGER NOT NULL,version INTEGER NOT NULL,account_facts TEXT NOT NULL,
        config_facts TEXT NOT NULL,material_facts TEXT NOT NULL,material_ids_json TEXT NOT NULL,
        expires_at_utc TEXT NOT NULL,created_at_utc TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS x_video_publish_pool_operation(
        tenant_key TEXT NOT NULL,user_id TEXT NOT NULL,idempotency_key TEXT NOT NULL,
        account_id INTEGER NOT NULL,request_hash TEXT NOT NULL,response_json TEXT NOT NULL,
        created_at_utc TEXT NOT NULL,PRIMARY KEY(tenant_key,user_id,idempotency_key));
    """)
    columns = {row[1] for row in conn.execute('PRAGMA table_info(x_video_publish_pool)')}
    for name in ('operator_tenant_key', 'operator_user_id', 'operator_name'):
        if name not in columns:
            conn.execute('ALTER TABLE x_video_publish_pool ADD COLUMN ' + name + " TEXT NOT NULL DEFAULT ''")


class PoolService:
    def __init__(self, store, account_resolver, material_resolver, identity_validator, *, now_fn=None, enabled=False):
        self.store = store
        self.account_resolver, self.material_resolver = account_resolver, material_resolver
        self.identity_validator = identity_validator
        self.now_fn = now_fn or (lambda: datetime.now(UTC))
        self.enabled = bool(enabled)
        with self.connection() as conn:
            install_schema(conn)

    @contextmanager
    def connection(self):
        conn = self.store.connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _gate(self):
        if not self.enabled:
            raise XPostError("pool_gate_closed", "账号素材池暂未启用", status=409)

    def _fresh_actor(self, actor):
        owner = (str(actor.get('tenant_key') or ''), str(actor.get('user_id') or ''))
        fresh = self.identity_validator(*owner)
        if not isinstance(fresh, dict) or (str(fresh.get('tenant_key') or ''), str(fresh.get('user_id') or '')) != owner:
            raise XPostError("identity_inactive", "计划发布人当前身份不匹配", status=403)
        if fresh.get('x_platform_access') is True and actor.get('_pool_subject'):
            fresh = {**fresh, '_pool_subject': actor['_pool_subject']}
        return fresh

    def _scope(self, actor, aid):
        """Share an existing account pool without moving or resetting its rows."""
        if actor.get('x_platform_access') is True:
            with self.connection() as conn:
                rows = conn.execute('SELECT tenant_key,user_id FROM x_video_publish_pool WHERE account_id=? LIMIT 2', (aid,)).fetchall()
            if len(rows) > 1:
                raise XPostError('pool_version_conflict', '账号存在多个历史素材池，请核对后合并', 409)
            actor['_pool_subject'] = list(rows[0]) if rows else [actor['tenant_key'], actor['user_id']]

    def _account(self, actor, aid):
        tenant, user = str(actor.get('tenant_key') or ''), str(actor.get('user_id') or '')
        aid = id_text(aid)
        account = self.account_resolver(actor, aid)
        if (not isinstance(account, dict) or str(account.get("id") or account.get("account_id")) != aid
                or (actor.get('x_platform_access') is not True and
                    (account.get("owner_tenant_key") != tenant or str(account.get("owner_user_id")) != user))):
            raise XPostError("account_not_found", "未找到当前用户的X账号", status=404)
        if not ID.fullmatch(str(account.get("x_user_id") or "")):
            raise XPostError("account_identity_invalid", "X账号身份无效", status=409)
        if not language(account.get("drama_language") or account.get("language")):
            raise XPostError("account_language_invalid", "请先配置账号剧语言", status=409)
        self._scope(actor, aid)
        return account

    @staticmethod
    def _facts(account):
        return canonical({key: account.get(key) for key in (
            "id", "x_user_id", "owner_tenant_key", "owner_user_id", "drama_language",
            "status", "publish_approved", "publish_eligible")})

    @staticmethod
    def _usable(account):
        if (account.get("status") != "active" or account.get("publish_approved") is not True
                or account.get("publish_eligible") is not True):
            raise XPostError("account_unavailable", "账号当前不可发布，请检查授权和允许发布状态", status=409)

    @staticmethod
    def _version(payload):
        value = payload.get("expected_version")
        if type(value) is not int or value < 0:
            raise XPostError("invalid_version", "请提交当前素材池版本")
        return value

    def _row(self, conn, actor, aid, account=None):
        owner = subject(actor)
        found = conn.execute("SELECT * FROM x_video_publish_pool WHERE tenant_key=? AND user_id=? AND account_id=?",
                             (*owner, aid)).fetchone()
        if found:
            return dict(found)
        return {"tenant_key": owner[0], "user_id": owner[1], "account_id": int(aid),
                "real_account_id": str((account or {}).get("x_user_id") or ""), "version": 0,
                "status": "paused", "times_json": "[]", "processing_mode": "original",
                "language": language((account or {}).get("drama_language") or (account or {}).get("language")),
                "pause_reason": "", "effective_at_utc": "", "created_at_utc": "", "updated_at_utc": ""}

    def _locked(self, conn, actor, aid, payload, account):
        if actor.get('x_platform_access') is True:
            existing = conn.execute('SELECT tenant_key,user_id FROM x_video_publish_pool WHERE account_id=?', (aid,)).fetchall()
            if len(existing) > 1 or existing and tuple(existing[0]) != subject(actor):
                raise XPostError('pool_version_conflict', '素材池已被其他操作人更新，请刷新后重试', 409)
        pool = self._row(conn, actor, aid, account)
        if pool["version"] != self._version(payload):
            raise XPostError("pool_version_conflict", "素材池已改变，请刷新后重试", status=409)
        if pool["real_account_id"] != str(account["x_user_id"]):
            raise XPostError("account_identity_changed", "账号身份已改变，请刷新", status=409)
        return pool

    def _ensure(self, conn, actor, aid, account):
        now = stamp(self.now_fn())
        conn.execute("""INSERT OR IGNORE INTO x_video_publish_pool(tenant_key,user_id,account_id,
          real_account_id,language,effective_at_utc,created_at_utc,updated_at_utc) VALUES(?,?,?,?,?,?,?,?)""",
          (*subject(actor), aid, str(account["x_user_id"]), language(account.get("drama_language") or account.get("language")), now, now, now))

    def _bump(self, conn, actor, aid, *, record_operator=False):
        conn.execute("UPDATE x_video_publish_pool SET version=version+1,updated_at_utc=? WHERE tenant_key=? AND user_id=? AND account_id=?",
                     (stamp(self.now_fn()), *subject(actor), aid))
        if record_operator and actor.get('x_platform_access') is True:
            conn.execute('UPDATE x_video_publish_pool SET operator_tenant_key=?,operator_user_id=?,operator_name=? WHERE tenant_key=? AND user_id=? AND account_id=?',
                         (actor['tenant_key'], actor['user_id'], str(actor.get('name') or ''), *subject(actor), aid))
        return self._dto(self._row(conn, actor, aid))

    @staticmethod
    def _dto(pool):
        return {"version": pool["version"], "enabled": pool["status"] == "enabled", "status": pool["status"],
                "times": json.loads(pool["times_json"]), "processing_mode": "random_overlay" if pool["processing_mode"] == "random" else "original",
                "language": pool["language"], "pause_reason": pool["pause_reason"]}

    def _sync_language(self, actor, aid, account):
        current_language = language(account.get("drama_language") or account.get("language"))
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            pool = self._row(conn, actor, aid, account)
            if pool["language"] != current_language:
                conn.execute("""UPDATE x_video_publish_pool SET status='paused',language=?,pause_reason='account_language_changed',
                  effective_at_utc=?,version=version+1,updated_at_utc=? WHERE tenant_key=? AND user_id=? AND account_id=?""",
                  (current_language, stamp(self.now_fn()), stamp(self.now_fn()), *subject(actor), aid))
            conn.commit()

    def listing(self, actor, aid, query=None):
        query = query or {}
        aid = id_text(aid)
        account = self._account(actor, aid)
        self._sync_language(actor, aid, account)
        self.sync_outcomes(actor, aid)
        state = query.get("state", "pending")
        if state not in {"pending", "processing", "published", "failed", "slots"}:
            raise XPostError("invalid_state", "素材池筛选状态无效")
        try:
            limit = int(query.get("limit", 50))
            offset = int(query.get("cursor") or 0)
            if not 1 <= limit <= 100 or offset < 0:
                raise ValueError()
        except (TypeError, ValueError):
            raise XPostError("invalid_cursor", "分页参数无效") from None
        with self.connection() as conn:
            pool = self._row(conn, actor, aid, account)
            key = (*subject(actor), aid)
            counts = {name: 0 for name in ("pending", "processing", "published", "failed", "slots")}
            for row in conn.execute("SELECT status,COUNT(*) AS n FROM x_video_publish_pool_item WHERE tenant_key=? AND user_id=? AND account_id=? GROUP BY status", key):
                counts[row["status"] if row["status"] in {"pending", "processing", "published"} else "failed"] += row["n"]
            counts["slots"] = conn.execute("SELECT COUNT(*) FROM x_video_publish_pool_slot WHERE tenant_key=? AND user_id=? AND account_id=?", key).fetchone()[0]
            if state == "slots":
                rows = conn.execute("SELECT * FROM x_video_publish_pool_slot WHERE tenant_key=? AND user_id=? AND account_id=? ORDER BY id DESC LIMIT ? OFFSET ?", (*key, limit + 1, offset)).fetchall()
                items = [{"slot_id": str(r["id"]), "at_utc": r["at_utc"], "status": r["status"], "reason": r["reason"], "batch_id": r["batch_id"], "task_id": r["task_id"], "material_id": r["material_id"]} for r in rows[:limit]]
            else:
                wanted = ("failed", "unknown", "cancelled", "removed") if state == "failed" else (state,)
                rows = conn.execute("SELECT * FROM x_video_publish_pool_item WHERE tenant_key=? AND user_id=? AND account_id=? AND status IN (" + ",".join("?" for _ in wanted) + ") ORDER BY position,id LIMIT ? OFFSET ?", (*key, *wanted, limit + 1, offset)).fetchall()
                items = [{"item_id": str(r["id"]), "material_id": r["material_id"], "material_name": r["material_name"], "language": r["language"],
                          "language_mismatch": r["language"] != pool["language"], "status": r["status"], "position": r["position"],
                          "task_id": r["task_id"], "batch_id": r["batch_id"], "reason": r["reason"]} for r in rows[:limit]]
        return {"pool": self._dto(pool), "items": items if state != "slots" else [], "slots": items if state == "slots" else [],
                "counts": counts, "next_cursor": str(offset + limit) if len(rows) > limit else None}

    def settings(self, actor, aid, payload):
        self._gate()
        aid, account = id_text(aid), self._account(actor, aid)
        self._sync_language(actor, aid, account)
        times = payload.get("times")
        if (not isinstance(times, list) or not 1 <= len(times) <= 1440
                or any(not isinstance(v, str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", v) for v in times)
                or len(set(times)) != len(times)):
            raise XPostError("invalid_times", "每日至少设置一个北京时间，格式HH:mm且不得重复")
        mode = MODES.get(payload.get("processing_mode"))
        if mode is None:
            raise XPostError("invalid_processing", "加工方式无效")
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._locked(conn, actor, aid, payload, account)
            self._ensure(conn, actor, aid, account)
            conn.execute("UPDATE x_video_publish_pool SET times_json=?,processing_mode=?,effective_at_utc=? WHERE tenant_key=? AND user_id=? AND account_id=?",
                         (canonical(sorted(times)), mode, stamp(self.now_fn()), *subject(actor), aid))
            result = self._bump(conn, actor, aid, record_operator=True)
            conn.commit()
        return {"pool": result}

    def toggle(self, actor, aid, payload, enable):
        if enable:
            self._gate()
            actor = self._fresh_actor(actor)
        aid, account = id_text(aid), self._account(actor, aid)
        self._sync_language(actor, aid, account)
        self.sync_outcomes(actor, aid)
        if enable:
            self._usable(account)
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            pool = self._locked(conn, actor, aid, payload, account)
            if enable:
                if not json.loads(pool["times_json"]):
                    raise XPostError("pool_times_required", "请先设置每日时间点", status=409)
                if conn.execute("SELECT 1 FROM x_video_publish_pool_item WHERE tenant_key=? AND user_id=? AND account_id=? AND status='unknown' LIMIT 1", (*subject(actor), aid)).fetchone():
                    raise XPostError("pool_unknown_outcome", "存在结果待核对的任务，暂不能启用", status=409)
                if conn.execute("SELECT 1 FROM x_video_publish_pool_item WHERE tenant_key=? AND user_id=? AND account_id=? AND status='pending' AND language<>? LIMIT 1", (*subject(actor), aid, pool["language"])).fetchone():
                    raise XPostError("pool_language_mismatch", "请先移除语言不匹配的待发布素材", status=409)
            self._ensure(conn, actor, aid, account)
            conn.execute("UPDATE x_video_publish_pool SET status=?,pause_reason=?,effective_at_utc=? WHERE tenant_key=? AND user_id=? AND account_id=?",
                         ("enabled" if enable else "paused", "" if enable else "operator_paused", stamp(self.now_fn()), *subject(actor), aid))
            result = self._bump(conn, actor, aid, record_operator=True)
            conn.commit()
        return {"pool": result}

    def _config_facts(self, actor, aid, conn=None):
        settings = self.store.get_settings(actor, aid, conn=conn)
        return canonical({key: settings.get(key) for key in ("version", "default_copy", "drama_language")})

    def _materials(self, actor, ids):
        result = self.material_resolver(actor, ids)
        if not isinstance(result, dict):
            raise XPostError("material_source_unavailable", "素材查询失败", status=503)
        if "items" in result:
            return result.get("items", {}), result.get("errors", {})
        return result, {}

    def _validate_materials(self, actor, aid, account, mids, mode):
        materials, failures = self._materials(actor, mids)
        rows = [{"material_id": mid, "account_id": aid, "processing": mode} for mid in mids if mid in materials and mid not in failures]
        result = self.store.preview(actor, {"source": "pool", "rows": rows}, {aid: account}, materials, now=self.now_fn()) if rows else {"rows": []}
        valid, errors = {}, dict(failures)
        for row in result["rows"]:
            mid = str(row["material_id"])
            if row.get("valid") is True:
                valid[mid] = materials[mid]
            else:
                errors[mid] = {"code": row.get("code") or row.get("error_code") or "material_invalid", "message": row.get("message") or row.get("error_message") or row.get("error") or "素材未通过发布校验"}
        for mid in mids:
            if mid not in valid and mid not in errors:
                errors[mid] = {"code": "material_not_found", "message": "未找到素材或素材不可发布"}
        return valid, errors

    def preview(self, actor, aid, payload):
        self._gate()
        aid, account = id_text(aid), self._account(actor, aid)
        self._usable(account)
        self._sync_language(actor, aid, account)
        self.sync_outcomes(actor, aid)
        values = payload.get("material_ids")
        if not isinstance(values, list) or not 1 <= len(values) <= 200:
            raise XPostError("invalid_material_ids", "每次请输入1至200行素材ID")
        entries, unique, skipped = [], [], []
        for number, value in enumerate(values, 1):
            text = value.strip() if isinstance(value, str) else str(value)
            entry = {"row_number": number, "material_id": text[:128]}
            if not text:
                continue
            entries.append(entry)
            try:
                mid = id_text(text if isinstance(value, str) else value)
            except XPostError:
                skipped.append({**entry, "code": "invalid_material_id", "message": "素材ID必须为完整的正整数，不接受小数或科学计数法"})
                continue
            if mid not in unique:
                unique.append(mid)
        with self.connection() as conn:
            pool = self._locked(conn, actor, aid, payload, account)
            config_facts = self._config_facts(actor, aid, conn)
            occupied = {r[0] for r in conn.execute("SELECT material_id FROM x_video_publish_pool_item WHERE tenant_key=? AND user_id=? AND account_id=? AND status IN ('pending','processing','unknown')", (*subject(actor), aid))}
        materials, errors = self._validate_materials(actor, aid, account, unique, pool["processing_mode"])
        invalid_rows = {item["row_number"] for item in skipped}
        accepted, items = {}, []
        for entry in entries:
            if entry["row_number"] in invalid_rows:
                continue
            mid = entry["material_id"]
            error = ({"code": "pool_material_exists", "message": "素材已在待发布、处理中或待核对队列"} if mid in occupied
                     else {"code": "duplicate_material_id", "message": "重复素材ID，已保留首条有效输入"} if mid in accepted
                     else errors.get(mid))
            if error:
                skipped.append({**entry, "code": str(error.get("code") or "material_invalid")[:64], "message": str(error.get("message") or "素材未通过校验")[:500]})
                continue
            material = materials[mid]
            accepted[mid] = material
            items.append({**entry, "material_name": str(material.get("material_name") or material.get("name") or mid),
                          "language": language(material.get("language") or material.get("drama_language"))})
        token, expires = "", self.now_fn() + timedelta(minutes=10)
        if items:
            token = secrets.token_urlsafe(32)
            with self.connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                self._locked(conn, actor, aid, payload, account)
                if self._config_facts(actor, aid, conn) != config_facts:
                    raise XPostError("pool_preview_changed", "默认文案已改变，请重新校验", status=409)
                conn.execute("""INSERT INTO x_video_publish_pool_preview(preview_id,tenant_key,user_id,account_id,version,
                  account_facts,config_facts,material_facts,material_ids_json,expires_at_utc,created_at_utc) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                  (token, *subject(actor), aid, pool["version"], self._facts(account), config_facts, canonical(accepted), canonical(list(accepted)), stamp(expires), stamp(self.now_fn())))
                conn.commit()
        return {"preview_id": token, "expires_at_utc": stamp(expires), "items": items,
                "skipped": sorted(skipped, key=lambda item: item["row_number"]),
                "summary": {"total": len(entries), "valid": len(items), "skipped": len(skipped)}}

    @staticmethod
    def _operation_key(key):
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{8,120}", key):
            raise XPostError("invalid_operation_id", "入池操作标识无效")
        return key

    def existing(self, actor, aid, key, request_hash=None):
        aid, key = id_text(aid), self._operation_key(key)
        self._scope(actor, aid)
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM x_video_publish_pool_operation WHERE tenant_key=? AND user_id=? AND idempotency_key=?", (*subject(actor), key)).fetchone()
        if row is None:
            return None
        if str(row["account_id"]) != aid or request_hash is not None and row["request_hash"] != request_hash:
            raise XPostError("operation_conflict", "同一操作标识不可用于不同入池内容", status=409)
        return {**json.loads(row["response_json"]), "idempotent": True}

    def add(self, actor, aid, payload):
        aid, key = id_text(aid), self._operation_key(payload.get("idempotency_key"))
        request_hash = hashlib.sha256(canonical({"account_id": aid, "expected_version": self._version(payload), "preview_id": payload.get("preview_id")}).encode()).hexdigest()
        existing = self.existing(actor, aid, key, request_hash)
        if existing:
            return existing
        self._gate()
        account = self._account(actor, aid)
        self._usable(account)
        self._sync_language(actor, aid, account)
        with self.connection() as conn:
            proof = conn.execute("SELECT * FROM x_video_publish_pool_preview WHERE preview_id=? AND tenant_key=? AND user_id=? AND account_id=?", (str(payload.get("preview_id") or ""), *subject(actor), aid)).fetchone()
            pool = self._locked(conn, actor, aid, payload, account)
        if not proof or parse_time(proof["expires_at_utc"]) <= self.now_fn():
            raise XPostError("pool_preview_expired", "入池校验已失效，请重新校验", status=409)
        mids = json.loads(proof["material_ids_json"])
        materials, errors = self._validate_materials(actor, aid, account, mids, pool["processing_mode"])
        if errors or canonical(materials) != proof["material_facts"] or self._facts(account) != proof["account_facts"]:
            raise XPostError("pool_preview_changed", "账号或素材已变化，请重新校验", status=409)
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            old = conn.execute("SELECT * FROM x_video_publish_pool_operation WHERE tenant_key=? AND user_id=? AND idempotency_key=?", (*subject(actor), key)).fetchone()
            if old:
                if old["request_hash"] != request_hash:
                    raise XPostError("operation_conflict", "同一操作标识不可用于不同入池内容", status=409)
                return {**json.loads(old["response_json"]), "idempotent": True}
            current = self._locked(conn, actor, aid, payload, account)
            if (proof["version"] != current["version"] or parse_time(proof["expires_at_utc"]) <= self.now_fn()
                    or self._config_facts(actor, aid, conn) != proof["config_facts"]):
                raise XPostError("pool_preview_changed", "素材池或默认文案已改变，请重新校验", status=409)
            self._ensure(conn, actor, aid, account)
            occupied = {r[0] for r in conn.execute("SELECT material_id FROM x_video_publish_pool_item WHERE tenant_key=? AND user_id=? AND account_id=? AND status IN ('pending','processing','unknown')", (*subject(actor), aid))}
            if occupied.intersection(mids):
                raise XPostError("pool_material_exists", "部分素材已入池，请重新校验", status=409)
            position = conn.execute("SELECT COALESCE(MAX(position),0) FROM x_video_publish_pool_item WHERE tenant_key=? AND user_id=? AND account_id=?", (*subject(actor), aid)).fetchone()[0]
            now, items = stamp(self.now_fn()), []
            for mid in mids:
                position += 1
                material = materials[mid]
                name = str(material.get("material_name") or material.get("name") or mid)
                lang = language(material.get("language") or material.get("drama_language"))
                item_id = conn.execute("""INSERT INTO x_video_publish_pool_item(tenant_key,user_id,account_id,material_id,
                    position,material_name,language,material_json,created_at_utc,updated_at_utc) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (*subject(actor), aid, mid, position, name, lang, canonical(material), now, now)).lastrowid
                items.append({"item_id": str(item_id), "material_id": mid, "material_name": name, "status": "pending", "position": position})
            result = {"pool": self._bump(conn, actor, aid, record_operator=True), "items": items, "idempotent": False}
            conn.execute("INSERT INTO x_video_publish_pool_operation VALUES(?,?,?,?,?,?,?)", (*subject(actor), key, aid, request_hash, canonical(result), now))
            conn.commit()
        return result

    def reorder_or_remove(self, actor, aid, payload, remove):
        self._gate()
        aid, account = id_text(aid), self._account(actor, aid)
        values = payload.get("item_ids") if remove else [payload.get("item_id")]
        if not isinstance(values, list) or not 1 <= len(values) <= 200:
            raise XPostError("invalid_items", "请选择1至200条素材")
        ids = [id_text(value) for value in values]
        if len(ids) != len(set(ids)):
            raise XPostError("invalid_items", "素材项不得重复")
        direction = payload.get("direction")
        if not remove and direction not in {"top", "up", "down"}:
            raise XPostError("invalid_direction", "移动方向无效")
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._locked(conn, actor, aid, payload, account)
            rows = conn.execute("SELECT * FROM x_video_publish_pool_item WHERE tenant_key=? AND user_id=? AND account_id=? AND id IN (" + ",".join("?" for _ in ids) + ")", (*subject(actor), aid, *ids)).fetchall()
            if len(rows) != len(ids) or any(row["status"] != "pending" or row["task_id"] for row in rows):
                raise XPostError("pool_item_bound", "只能移动或移除尚未生成任务的素材", status=409)
            now = stamp(self.now_fn())
            if remove:
                conn.execute("UPDATE x_video_publish_pool_item SET status='removed',updated_at_utc=? WHERE tenant_key=? AND user_id=? AND account_id=? AND id IN (" + ",".join("?" for _ in ids) + ")", (now, *subject(actor), aid, *ids))
            else:
                row = rows[0]
                if direction == "top":
                    position = conn.execute("SELECT MIN(position) FROM x_video_publish_pool_item WHERE tenant_key=? AND user_id=? AND account_id=? AND status='pending'", (*subject(actor), aid)).fetchone()[0] - 1
                    conn.execute("UPDATE x_video_publish_pool_item SET position=?,updated_at_utc=? WHERE id=?", (position, now, row["id"]))
                else:
                    op, order = ("<", "DESC") if direction == "up" else (">", "ASC")
                    other = conn.execute("SELECT id,position FROM x_video_publish_pool_item WHERE tenant_key=? AND user_id=? AND account_id=? AND status='pending' AND position " + op + "? ORDER BY position " + order + ",id " + order + " LIMIT 1", (*subject(actor), aid, row["position"])).fetchone()
                    if other:
                        conn.execute("UPDATE x_video_publish_pool_item SET position=?,updated_at_utc=? WHERE id=?", (other["position"], now, row["id"]))
                        conn.execute("UPDATE x_video_publish_pool_item SET position=?,updated_at_utc=? WHERE id=?", (row["position"], now, other["id"]))
            result = self._bump(conn, actor, aid, record_operator=True)
            conn.commit()
        return {"pool": result}

    @staticmethod
    def _batch_tasks(batch):
        body = batch.get("batch", batch)
        return body.get("tasks", body.get("items", []))

    def sync_outcomes(self, actor, aid):
        """Read durable task outcomes, then update pool views; never retry tasks."""
        self._scope(actor, aid)
        with self.connection() as conn:
            rows = conn.execute("SELECT id,task_id,batch_id FROM x_video_publish_pool_item WHERE tenant_key=? AND user_id=? AND account_id=? AND status IN ('processing','unknown')", (*subject(actor), aid)).fetchall()
        batches, updates = {}, []
        for row in rows:
            if row["batch_id"] not in batches:
                batches[row["batch_id"]] = self.store.get_batch(actor, row["batch_id"])
            task = next((t for t in self._batch_tasks(batches[row["batch_id"]]) if str(t.get("id") or t.get("task_id")) == row["task_id"]), None)
            if task is None:
                raise XPostError("pool_task_missing", "素材池任务关联缺失，需要核对", status=503)
            status = str(task.get("status") or "")
            mapped = ("unknown" if status in {"unknown", "unknown_outcome", "needs_review", "held"}
                      else "published" if status in {"published", "completed", "success"}
                      else "failed" if status in {"failed", "failed_preflight", "blocked", "completed_with_errors"}
                      else "cancelled" if status == "cancelled" else None)
            if mapped:
                updates.append((mapped, str(task.get("error_message") or task.get("error") or "")[:500], row["id"]))
        if not updates:
            return
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for state, reason, item_id in updates:
                conn.execute("UPDATE x_video_publish_pool_item SET status=?,reason=?,updated_at_utc=? WHERE id=? AND status IN ('processing','unknown')", (state, reason, stamp(self.now_fn()), item_id))
            if any(state == "unknown" for state, _, _ in updates):
                conn.execute("UPDATE x_video_publish_pool SET status='paused',pause_reason='unknown_outcome',version=version+1,updated_at_utc=? WHERE tenant_key=? AND user_id=? AND account_id=? AND (status<>'paused' OR pause_reason<>'unknown_outcome')", (stamp(self.now_fn()), *subject(actor), aid))
            conn.commit()
