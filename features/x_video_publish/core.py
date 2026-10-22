"""Owner-scoped personal publishing, bridged to immutable legacy X queues.

This module never opens an X token or performs a network request. Source and
account DTOs are resolved by the authenticated Sidecar before entering here.
"""
from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import secrets
import sqlite3

from features.x_accounts.language import canonical_drama_language
from features.x_posts.service import XPostError, XPostStore, _connect, utc_now
from .templates import DEFAULT_COPY, normalize_caption, render_caption

PersonalPublishError = XPostError
BEIJING_TZ = timezone(timedelta(hours=8))
PREVIEW_TTL_SECONDS = 600
MAX_ROWS = 200
_TERMINAL = {"failed_preflight", "completed", "completed_with_errors", "stopped", "needs_review"}


def actor_subject(actor):
    if not isinstance(actor, dict):
        raise XPostError("auth_required", "用户身份无效", 401)
    values = tuple(str(actor.get(k) or "").strip() for k in ("tenant_key", "user_id"))
    if any(not v or len(v) > 255 or any(ord(c) < 32 for c in v) for v in values):
        raise XPostError("auth_required", "用户身份不完整", 401)
    return values


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _now(value=None):
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise XPostError("invalid_request", "当前时间必须包含时区", 400)
    return value.astimezone(timezone.utc)


def _stamp(value):
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _id(value, label="ID"):
    if isinstance(value, bool) or not re.fullmatch(r"[1-9][0-9]{0,29}", str(value or "")):
        raise XPostError("invalid_request", "%s 必须是精确正整数字符串" % label, 400)
    return str(value)


def _account_id(value):
    result = int(_id(value, "账号 ID"))
    if result > 2 ** 63 - 1:
        raise XPostError("invalid_request", "账号配置 ID 超出支持范围", 400)
    return result


def _timing(value, now, check_future=True):
    if value in (None, ""):
        return ""
    value = str(value).strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?", value):
            parsed = datetime.fromisoformat(value).replace(tzinfo=BEIJING_TZ)
        elif re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.0{1,3})?Z", value):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        else:
            raise ValueError()
    except ValueError:
        raise XPostError("x_video_schedule_invalid", "发布时间需为北京时间 YYYY-MM-DD HH:mm[:ss] 或整秒 UTC 时间（可带 .000Z）", 400) from None
    if check_future and parsed <= now:
        raise XPostError("x_video_schedule_past", "发布时间已过，请重新选择未来时间", 400)
    return _stamp(parsed)


def _account(account, account_id, owner, language=None):
    if not isinstance(account, dict) or _account_id(account.get("id")) != int(account_id):
        raise XPostError("x_video_account_not_found", "账号不存在或不属于当前用户", 404)
    if (str(account.get("owner_tenant_key") or ""), str(account.get("owner_user_id") or "")) != owner:
        raise XPostError("x_video_account_not_found", "账号不存在或不属于当前用户", 404)
    if account.get("status") != "active" or account.get("publish_approved") is not True or account.get("publish_eligible") is not True:
        raise XPostError("x_video_account_unavailable", "账号授权或允许发布状态不满足要求", 409)
    try:
        current_language = canonical_drama_language(account.get("drama_language"))
    except ValueError:
        raise XPostError("x_post_account_language_mismatch", "账号剧语言无效", 409) from None
    if language and current_language != language:
        raise XPostError("x_post_account_language_mismatch", "素材与账号剧语言不一致，或账号语言已变更", 409)
    return current_language


def ensure_core_schema(conn):
    """Called inside the canonical X Post schema transaction; no nested commit."""
    statements = [
        """CREATE TABLE IF NOT EXISTS x_video_publish_account_config (
            tenant_key TEXT NOT NULL,user_id TEXT NOT NULL,account_id INTEGER NOT NULL,
            default_copy TEXT NOT NULL,drama_language TEXT NOT NULL,version INTEGER NOT NULL,
            updated_at TEXT NOT NULL,PRIMARY KEY(tenant_key,user_id,account_id))""",
        """CREATE TABLE IF NOT EXISTS x_video_publish_draft (
            tenant_key TEXT NOT NULL,user_id TEXT NOT NULL,payload_json TEXT NOT NULL,
            version INTEGER NOT NULL,updated_at TEXT NOT NULL,PRIMARY KEY(tenant_key,user_id))""",
        """CREATE TABLE IF NOT EXISTS x_video_publish_preview (
            token_hash TEXT PRIMARY KEY,tenant_key TEXT NOT NULL,user_id TEXT NOT NULL,
            source TEXT NOT NULL,payload_hash TEXT NOT NULL,rows_json TEXT NOT NULL,
            created_at TEXT NOT NULL,expires_at TEXT NOT NULL,batch_id INTEGER)""",
        """CREATE TABLE IF NOT EXISTS x_video_publish_batch (
            id INTEGER PRIMARY KEY AUTOINCREMENT,tenant_key TEXT NOT NULL,user_id TEXT NOT NULL,
            actor_name TEXT NOT NULL,source TEXT NOT NULL,idempotency_key TEXT NOT NULL,
            preview_token_hash TEXT NOT NULL UNIQUE,payload_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,UNIQUE(tenant_key,user_id,idempotency_key))""",
        """CREATE TABLE IF NOT EXISTS x_video_publish_task (
            id INTEGER PRIMARY KEY AUTOINCREMENT,batch_id INTEGER NOT NULL,
            manual_run_id INTEGER NOT NULL UNIQUE,account_id INTEGER NOT NULL,
            material_id TEXT NOT NULL,drama_language TEXT NOT NULL,caption TEXT NOT NULL,
            caption_source TEXT NOT NULL,config_version INTEGER NOT NULL,
            scheduled_at TEXT NOT NULL,processing TEXT NOT NULL,row_number INTEGER NOT NULL,
            source_snapshot_json TEXT NOT NULL,account_snapshot_json TEXT NOT NULL,
            created_at TEXT NOT NULL,UNIQUE(batch_id,account_id,material_id),
            FOREIGN KEY(batch_id) REFERENCES x_video_publish_batch(id),
            FOREIGN KEY(manual_run_id) REFERENCES x_post_manual_run(id))""",
        "CREATE INDEX IF NOT EXISTS ix_x_video_publish_task_pair ON x_video_publish_task(account_id,material_id)",
        "CREATE INDEX IF NOT EXISTS ix_x_video_publish_batch_owner ON x_video_publish_batch(tenant_key,user_id,id)",
    ]
    for sql in statements:
        conn.execute(sql)
    for table in ("x_post_manual_run", "x_post_queue"):
        if "copy_contract_version" not in {r[1] for r in conn.execute("PRAGMA table_info(" + table + ")")}:
            conn.execute("ALTER TABLE " + table + " ADD COLUMN copy_contract_version INTEGER NOT NULL DEFAULT 1")
    # Frozen mappings are the only authority for sharing an anchor reservation.
    for table in ("x_video_publish_task", "x_video_publish_batch"):
        for action in ("UPDATE", "DELETE"):
            conn.execute("CREATE TRIGGER IF NOT EXISTS trg_%s_%s BEFORE %s ON %s BEGIN SELECT RAISE(ABORT, 'personal publish identity immutable'); END" % (table, action.lower(), action, table))
    for table in ("x_post_manual_run", "x_post_queue"):
        conn.execute("CREATE TRIGGER IF NOT EXISTS trg_%s_copy_version BEFORE UPDATE OF copy_contract_version ON %s WHEN NEW.copy_contract_version<>OLD.copy_contract_version BEGIN SELECT RAISE(ABORT, 'copy contract immutable'); END" % (table, table))
    conn.execute("""CREATE TRIGGER IF NOT EXISTS trg_x_video_publish_preview_identity
      BEFORE UPDATE OF token_hash,tenant_key,user_id,source,payload_hash,rows_json,created_at,expires_at
      ON x_video_publish_preview BEGIN SELECT RAISE(ABORT, 'personal preview identity immutable'); END""")
    conn.execute("""CREATE TRIGGER IF NOT EXISTS trg_x_video_publish_preview_seal
      BEFORE UPDATE OF batch_id ON x_video_publish_preview WHEN OLD.batch_id IS NOT NULL
        OR NOT EXISTS (SELECT 1 FROM x_video_publish_batch b WHERE b.id=NEW.batch_id
          AND b.preview_token_hash=OLD.token_hash AND b.tenant_key=OLD.tenant_key AND b.user_id=OLD.user_id)
      BEGIN SELECT RAISE(ABORT, 'personal preview seal invalid'); END""")
    conn.execute("""CREATE TRIGGER IF NOT EXISTS trg_x_video_publish_child_owner
      BEFORE UPDATE OF actor_tenant_key ON x_post_manual_run
      WHEN OLD.actor_tenant_key<>NEW.actor_tenant_key AND EXISTS
        (SELECT 1 FROM x_video_publish_task WHERE manual_run_id=OLD.id)
      BEGIN SELECT RAISE(ABORT, 'personal child owner immutable'); END""")
    conn.execute("DROP TRIGGER IF EXISTS trg_x_video_publish_task_identity")
    conn.execute("""CREATE TRIGGER IF NOT EXISTS trg_x_video_publish_task_identity
        BEFORE INSERT ON x_video_publish_task WHEN NOT EXISTS (
          SELECT 1 FROM x_post_manual_run r JOIN x_video_publish_batch b ON b.id=NEW.batch_id
          JOIN x_video_publish_preview p ON p.token_hash=b.preview_token_hash AND p.batch_id IS NULL
          WHERE r.id=NEW.manual_run_id AND r.trigger_source='manual' AND r.copy_contract_version=2
          AND r.actor_tenant_key=b.tenant_key AND r.actor_user_id=b.user_id
          AND r.account_ids_json='[' || NEW.account_id || ']'
          AND r.material_ids_json='["' || NEW.material_id || '"]'
          AND r.body_template=NEW.caption AND r.scheduled_at=NEW.scheduled_at
          AND r.random_template_enabled=(NEW.processing='random'))
        BEGIN SELECT RAISE(ABORT, 'personal child identity mismatch'); END""")
    # Override only the ownership comparison, preserving the global reservation fence.
    for action, columns in (("insert", ""), ("update", " OF material_key,manual_run_id")):
        name = "trg_x_post_queue_manual_reservation_" + action
        conn.execute("DROP TRIGGER IF EXISTS " + name)
        conn.execute("""CREATE TRIGGER %s BEFORE %s%s ON x_post_queue
          WHEN EXISTS (SELECT 1 FROM x_post_manual_material_reservation r
            WHERE r.material_key=NEW.material_key AND r.state='active'
              AND (NEW.manual_run_id IS NULL OR r.manual_run_id<>NEW.manual_run_id)
              AND NOT EXISTS (
                SELECT 1 FROM x_video_publish_task anchor JOIN x_video_publish_task sibling
                  ON sibling.batch_id=anchor.batch_id AND sibling.material_id=anchor.material_id
                WHERE anchor.manual_run_id=r.manual_run_id
                  AND sibling.manual_run_id=NEW.manual_run_id
                  AND sibling.account_id=NEW.account_id AND sibling.material_id=NEW.material_key))
          BEGIN SELECT RAISE(ABORT, 'x_post_queue material reserved by manual run'); END""" % (name, action.upper(), columns))
    conn.execute("""CREATE TRIGGER IF NOT EXISTS trg_x_video_publish_queue_identity
      BEFORE INSERT ON x_post_queue WHEN NEW.copy_contract_version=2 AND NOT EXISTS (
        SELECT 1 FROM x_video_publish_task t WHERE t.manual_run_id=NEW.manual_run_id
          AND t.account_id=NEW.account_id AND t.material_id=NEW.material_key
          AND t.caption=NEW.body_template AND NEW.account_drama_language_frozen=1
          AND t.drama_language=NEW.account_drama_language
          AND (t.processing='random')=NEW.random_template_enabled)
      BEGIN SELECT RAISE(ABORT, 'personal queue identity mismatch'); END""")


def personal_task(conn, run_id):
    row = conn.execute("SELECT t.*,b.tenant_key,b.user_id,b.actor_name,b.source FROM x_video_publish_task t JOIN x_video_publish_batch b ON b.id=t.batch_id WHERE t.manual_run_id=?", (int(run_id),)).fetchone()
    return dict(row) if row else None


def personal_batch_held(conn, batch_id):
    return conn.execute("""SELECT 1 FROM x_video_publish_task t
      JOIN x_post_manual_run r ON r.id=t.manual_run_id
      LEFT JOIN x_post_queue q ON q.manual_run_id=r.id
      LEFT JOIN x_post_publish_log l ON l.queue_id=q.id
      WHERE t.batch_id=? AND (r.status IN ('needs_review','stopped') OR r.unknown_count>0
        OR COALESCE(l.unknown_outcome,0)=1 OR l.status IN ('post_creating','repost_creating')) LIMIT 1""", (int(batch_id),)).fetchone() is not None


@contextlib.contextmanager
def _readonly_personal_connection(db_path):
    """Runtime guards must never initialize/migrate the shared publishing DB."""
    try:
        uri = Path(db_path).resolve().as_uri() + "?mode=ro"
        with contextlib.closing(sqlite3.connect(uri, uri=True, timeout=5)) as conn:
            conn.row_factory = sqlite3.Row
            yield conn
    except (sqlite3.Error, OSError):
        raise XPostError("x_video_ledger_unavailable", "发布任务账本暂不可读，已阻止继续发布", 503) from None


def _readonly_personal_task(conn, run_id):
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('x_video_publish_task','x_video_publish_batch')")}
    if not tables:
        # An unupgraded legacy database has no personal mappings. Do not create
        # them merely because an old upload requests its next access token.
        return None
    if len(tables) != 2:
        raise XPostError("x_video_ledger_unavailable", "发布任务账本不完整，已阻止继续发布", 503)
    task = personal_task(conn, run_id)
    if task is not None:
        try:
            task["account_snapshot"] = json.loads(task["account_snapshot_json"])
            task["source_snapshot"] = json.loads(task["source_snapshot_json"])
        except (ValueError, TypeError):
            raise XPostError("x_video_ledger_unavailable", "发布任务快照不可读，已阻止继续发布", 503) from None
    return task


def read_personal_task(db_path, run_id):
    """Look up an immutable owner mapping without any DDL or write connection."""
    if not run_id:
        return None
    with _readonly_personal_connection(db_path) as conn:
        return _readonly_personal_task(conn, run_id)


def assert_personal_run_publishable(db_path, run_id, account=None):
    """Read-only execution guard; preserves holds and current owner/language checks."""
    if not run_id:
        return None
    with _readonly_personal_connection(db_path) as conn:
        task = _readonly_personal_task(conn, run_id)
        if task is None:
            return None
        if personal_batch_held(conn, task["batch_id"]):
            raise XPostError("x_post_unknown_outcome", "本批次已暂停，请先核对已有发布结果", 409, True)
        if account is not None:
            _account(account, task["account_id"], (task["tenant_key"], task["user_id"]), task["drama_language"])
        return task


def settle_personal_reservations(conn, run_id, timestamp):
    """True means personal run; keep shared reservations until every sibling settles."""
    task = personal_task(conn, run_id)
    if task is None:
        return False
    rows = conn.execute("""SELECT t.manual_run_id,r.status,
      (SELECT COUNT(*) FROM x_post_queue q WHERE q.manual_run_id=t.manual_run_id) AS queue_count
      FROM x_video_publish_task t JOIN x_post_manual_run r ON r.id=t.manual_run_id
      WHERE t.batch_id=? AND t.material_id=?""", (task["batch_id"], task["material_id"])).fetchall()
    if all(int(r["queue_count"]) > 0 or r["status"] in _TERMINAL for r in rows):
        any_queue = any(int(r["queue_count"]) > 0 for r in rows)
        conn.execute("""UPDATE x_post_manual_material_reservation SET state=?,release_reason=?,updated_at=?
          WHERE state='active' AND material_key=? AND manual_run_id IN
          (SELECT manual_run_id FROM x_video_publish_task WHERE batch_id=? AND material_id=?)""",
          ("consumed" if any_queue else "released", "" if any_queue else "x_video_batch_preflight_failed", timestamp, task["material_id"], task["batch_id"], task["material_id"]))
    return True


class PersonalStore:
    def __init__(self, db_path):
        self.db_path = str(db_path)
        XPostStore(self.db_path)

    def connect(self):
        return _connect(self.db_path)

    @contextlib.contextmanager
    def _connection(self, conn=None, write=False):
        owned = conn is None
        conn = conn or self.connect()
        try:
            if write and owned:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            if owned and write:
                conn.commit()
        except Exception:
            if owned and write:
                conn.rollback()
            raise
        finally:
            if owned:
                conn.close()

    def get_settings(self, actor, account_id, conn=None):
        owner, account_id = actor_subject(actor), _account_id(account_id)
        with self._connection(conn) as db:
            row = db.execute("SELECT * FROM x_video_publish_account_config WHERE tenant_key=? AND user_id=? AND account_id=?", owner + (account_id,)).fetchone()
        return dict(row) if row else {"account_id": account_id, "default_copy": DEFAULT_COPY, "drama_language": "", "version": 0, "updated_at": ""}

    def settings(self, actor):
        owner = actor_subject(actor)
        with self._connection() as conn:
            rows = conn.execute("SELECT account_id,default_copy,drama_language,version,updated_at FROM x_video_publish_account_config WHERE tenant_key=? AND user_id=? ORDER BY account_id", owner).fetchall()
        return {"items": [dict(row) for row in rows]}

    def save_settings(self, actor, account_id, payload, account):
        owner, account_id = actor_subject(actor), _account_id(account_id)
        language = _account(account, account_id, owner)
        caption = normalize_caption(payload.get("default_copy", payload.get("default_caption", DEFAULT_COPY)))
        with self._connection(write=True) as conn:
            current = self.get_settings(actor, account_id, conn)
            if payload.get("version", current["version"] if not current["version"] else None) != current["version"]:
                raise XPostError("x_video_config_conflict", "账号配置已更新，请刷新后重试", 409)
            conn.execute("""INSERT INTO x_video_publish_account_config VALUES(?,?,?,?,?,?,?)
              ON CONFLICT(tenant_key,user_id,account_id) DO UPDATE SET default_copy=excluded.default_copy,
              drama_language=excluded.drama_language,version=excluded.version,updated_at=excluded.updated_at""",
              owner + (account_id, caption, language, current["version"] + 1, utc_now()))
            return self.get_settings(actor, account_id, conn)

    def delete_settings(self, actor, account_id):
        with self._connection(write=True) as conn:
            conn.execute("DELETE FROM x_video_publish_account_config WHERE tenant_key=? AND user_id=? AND account_id=?", actor_subject(actor) + (_account_id(account_id),))
        return {"deleted": True}

    def get_draft(self, actor):
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM x_video_publish_draft WHERE tenant_key=? AND user_id=?", actor_subject(actor)).fetchone()
        return {"data": json.loads(row["payload_json"]), "version": row["version"], "updated_at": row["updated_at"]} if row else {"data": None, "version": 0, "updated_at": ""}

    def save_draft(self, actor, payload):
        owner = actor_subject(actor)
        data = payload.get("data", payload) if isinstance(payload, dict) else payload
        raw = _json(data)
        if len(raw.encode("utf-8")) > 512 * 1024:
            raise XPostError("x_video_draft_too_large", "草稿超过512KiB", 413)
        with self._connection(write=True) as conn:
            row = conn.execute("SELECT version FROM x_video_publish_draft WHERE tenant_key=? AND user_id=?", owner).fetchone()
            version = int(row[0]) if row else 0
            if isinstance(payload, dict) and "version" in payload and payload["version"] != version:
                raise XPostError("x_video_draft_conflict", "草稿已在另一窗口更新", 409)
            conn.execute("INSERT INTO x_video_publish_draft VALUES(?,?,?,?,?) ON CONFLICT(tenant_key,user_id) DO UPDATE SET payload_json=excluded.payload_json,version=excluded.version,updated_at=excluded.updated_at", owner + (raw, version + 1, utc_now()))
        return self.get_draft(actor)

    def delete_draft(self, actor):
        with self._connection(write=True) as conn:
            conn.execute("DELETE FROM x_video_publish_draft WHERE tenant_key=? AND user_id=?", actor_subject(actor))
        return {"deleted": True}

    @staticmethod
    def _occupancy(conn, account_id, material_id, allow_success=False, own_batch=None):
        # Unknown results fence the account, not just a particular material.
        hold = conn.execute("""SELECT 1 FROM x_post_queue q LEFT JOIN x_post_publish_log l ON l.queue_id=q.id
          LEFT JOIN x_post_repost_ledger r ON r.queue_id=q.id
          WHERE (q.account_id=? OR q.relay_account_id=? OR r.relay_account_id=?)
          AND (COALESCE(l.unknown_outcome,0)=1 OR l.status IN ('post_creating','repost_creating')
          OR COALESCE(r.unknown_outcome,0)=1 OR r.status IN ('source_publishing','reposting','needs_review')) LIMIT 1""", (account_id,) * 3).fetchone()
        if hold:
            raise XPostError("x_post_unknown_outcome", "账号存在结果待核对任务，禁止重复提交", 409, True)
        personal_hold = conn.execute("""SELECT 1 FROM x_video_publish_task t
          JOIN x_post_manual_run r ON r.id=t.manual_run_id WHERE t.account_id=?
          AND (r.status='needs_review' OR r.unknown_count>0) LIMIT 1""", (account_id,)).fetchone()
        if personal_hold:
            raise XPostError("x_post_unknown_outcome", "账号存在结果待核对任务，禁止重复提交", 409, True)
        active = conn.execute("""SELECT 1 FROM x_post_queue q LEFT JOIN x_post_publish_log l ON l.queue_id=q.id
          WHERE q.account_id=? AND q.material_key=? AND (q.status IN ('queued','reserved','publishing')
          OR l.status IN ('reserved','media_uploading','post_creating','repost_creating')) LIMIT 1""", (account_id, material_id)).fetchone()
        if active:
            raise XPostError("x_video_pair_active", "此账号与素材已有进行中任务", 409)
        pending = conn.execute("""SELECT 1 FROM x_video_publish_task t JOIN x_post_manual_run r ON r.id=t.manual_run_id
          WHERE t.account_id=? AND t.material_id=? AND r.status IN ('queued','running','needs_review')
          AND (? IS NULL OR t.batch_id<>?) LIMIT 1""", (account_id, material_id, own_batch, own_batch)).fetchone()
        if pending:
            raise XPostError("x_video_pair_active", "此账号与素材已有等待或待核对任务", 409)
        success = conn.execute("SELECT 1 FROM x_post_queue WHERE account_id=? AND material_key=? AND status='published' LIMIT 1", (account_id, material_id)).fetchone()
        if success and not allow_success:
            raise XPostError("x_video_repeat_required", "此账号已成功发布过该素材；默认跳过，可明确选择再次发布", 409)

    def preview(self, actor, payload, accounts, materials, now=None):
        owner, current = actor_subject(actor), _now(now)
        if not isinstance(payload, dict) or payload.get("source", "manual") not in {"manual", "excel", "pool"}:
            raise XPostError("invalid_request", "发布来源无效", 400)
        source, rows = payload.get("source", "manual"), payload.get("rows")
        if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_ROWS:
            raise XPostError("x_video_rows_invalid", "每批需有1至200条明确的发布配对", 400)
        if not isinstance(accounts, dict) or not isinstance(materials, dict):
            raise XPostError("invalid_request", "可信账号与素材数据缺失", 400)
        result, seen, material_times = [], set(), {}
        with self._connection(write=True) as conn:
            for index, raw in enumerate(rows, 1):
                row = {"row_number": index, "valid": False, "repeat_required": False}
                try:
                    if not isinstance(raw, dict):
                        raise XPostError("invalid_request", "发布行必须是对象", 400)
                    row["row_number"] = int(raw.get("row_number") or index)
                    account_id, material_id = _account_id(raw.get("account_id")), _id(raw.get("material_id"), "素材 ID")
                    row.update(account_id=str(account_id), material_id=material_id)
                    pair = (account_id, material_id)
                    if pair in seen:
                        raise XPostError("x_video_duplicate_row", "同账号与素材重复，已保留首条有效行", 409)
                    account = accounts.get(str(account_id), accounts.get(account_id))
                    material = materials.get(material_id, materials.get(int(material_id)))
                    language = _account(account, account_id, owner)
                    if not isinstance(material, dict) or str(material.get("material_id", material.get("id", ""))) != material_id:
                        raise XPostError("x_video_material_not_found", "素材不存在或未通过来源校验", 404)
                    material_language = canonical_drama_language(material.get("language", material.get("drama_language")))
                    if language != material_language:
                        raise XPostError("x_post_account_language_mismatch", "素材语言与账号剧语言不匹配", 409)
                    schedule = _timing(raw.get("scheduled_at", raw.get("publish_time", "")), current)
                    if source == "manual" and material_id in material_times and material_times[material_id] != schedule:
                        raise XPostError("x_video_material_time_mismatch", "同一素材的目标账号必须共用发布时间", 400)
                    processing = raw.get("processing", raw.get("processing_mode", "original")) or "original"
                    if processing not in {"original", "random"}:
                        raise XPostError("x_video_processing_invalid", "加工方式仅支持原片或随机加工", 400)
                    config = self.get_settings(actor, account_id, conn)
                    supplied_copy = raw.get("caption", raw.get("text", ""))
                    use_default = supplied_copy in (None, "")
                    caption = normalize_caption(config["default_copy"] if use_default else supplied_copy)
                    rendered = render_caption(caption, material.get("drama_name", material.get("name")), material.get("drama_description", material.get("description", material.get("desc"))), "https://preview.invalid/0.html")
                    row.update(caption=caption, rendered_caption=rendered, caption_source="default" if use_default else "edited", config_version=int(config["version"]), drama_language=language, scheduled_at=schedule, processing=processing, source_snapshot={k: material[k] for k in ("material_id", "id", "drama_name", "drama_description", "description", "language", "duration_seconds", "material_name", "content_id", "series_code", "url", "material_url") if k in material}, account_snapshot={k: account[k] for k in ("id", "username", "display_name", "x_user_id", "drama_language") if k in account})
                    try:
                        self._occupancy(conn, account_id, material_id, bool(raw.get("allow_repeat_success") is True))
                    except XPostError as exc:
                        if exc.code != "x_video_repeat_required":
                            raise
                        row.update(repeat_required=True, error_code=exc.code, error_message=str(exc))
                    if conn.execute("SELECT 1 FROM x_post_manual_material_reservation WHERE material_key=? AND state='active' LIMIT 1", (material_id,)).fetchone():
                        raise XPostError("x_post_manual_material_unavailable", "素材已被其他待执行批次预约", 409)
                    seen.add(pair)
                    material_times[material_id] = schedule
                    row["valid"] = not row["repeat_required"]
                except (XPostError, ValueError, TypeError) as exc:
                    row.update(error_code=getattr(exc, "code", "invalid_request"), error_message=str(exc)[:300])
                result.append(row)
            token = secrets.token_urlsafe(32)
            expiry = _stamp(current + timedelta(seconds=PREVIEW_TTL_SECONDS))
            frozen = _json(result)
            conn.execute("INSERT INTO x_video_publish_preview VALUES(?,?,?,?,?,?,?,?,NULL)", (hashlib.sha256(token.encode()).hexdigest(),) + owner + (source, hashlib.sha256((source + frozen).encode()).hexdigest(), frozen, _stamp(current), expiry))
        return {"preview_token": token, "expires_at": expiry, "source": source, "rows": result, "valid_count": sum(r["valid"] for r in result), "invalid_count": sum(not r["valid"] for r in result)}

    def get_preview(self, actor, token):
        with self._connection() as conn:
            row = self._preview_row(conn, actor, token)
        return {"preview_token": token, "source": row["source"], "rows": json.loads(row["rows_json"]), "expires_at": row["expires_at"], "batch_id": row["batch_id"]}

    @staticmethod
    def _preview_row(conn, actor, token):
        if not isinstance(token, str) or not 20 <= len(token) <= 200:
            raise XPostError("x_video_preview_not_found", "确认预览不存在，请重新核对", 404)
        row = conn.execute("SELECT * FROM x_video_publish_preview WHERE token_hash=? AND tenant_key=? AND user_id=?", (hashlib.sha256(token.encode()).hexdigest(),) + actor_subject(actor)).fetchone()
        if not row:
            raise XPostError("x_video_preview_not_found", "确认预览不存在，请重新核对", 404)
        return row

    def commit(self, actor, preview_token, idempotency_key, accounts=None, materials=None, now=None, conn=None, allow_repeat_success=False):
        owner, current = actor_subject(actor), _now(now)
        if not isinstance(idempotency_key, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{8,160}", idempotency_key):
            raise XPostError("invalid_request", "发布幂等键无效", 400)
        if type(allow_repeat_success) is not bool:
            raise XPostError("invalid_request", "再次发布确认必须是布尔值", 400)
        with self._connection(conn, write=True) as db:
            frozen = self._preview_row(db, actor, preview_token)
            existing = db.execute("SELECT * FROM x_video_publish_batch WHERE tenant_key=? AND user_id=? AND idempotency_key=?", owner + (idempotency_key,)).fetchone()
            if existing:
                if existing["preview_token_hash"] != frozen["token_hash"]:
                    raise XPostError("x_post_idempotency_conflict", "幂等键已用于其他发布批次", 409)
                return dict(self._batch(db, actor, existing["id"]), created=False)
            if not isinstance(accounts, dict) or not isinstance(materials, dict):
                raise XPostError("x_video_trusted_context_required", "首次提交需重新读取可信账号与素材数据", 400)
            if frozen["batch_id"]:
                raise XPostError("x_video_preview_consumed", "此确认清单已生成批次", 409)
            if frozen["expires_at"] <= _stamp(current):
                raise XPostError("x_video_preview_expired", "确认清单已过期，请重新核对", 409)
            all_rows = json.loads(frozen["rows_json"])
            rows = [r for r in all_rows if r["valid"] or (r.get("repeat_required") and allow_repeat_success)]
            if frozen["source"] != "excel" and len(rows) != len(all_rows):
                raise XPostError("x_video_batch_invalid", "手动发布清单存在无效或待确认行，请逐条修正后提交", 400)
            if not rows:
                raise XPostError("x_video_no_valid_rows", "没有可发布的有效行", 400)
            for row in rows:
                aid, mid = int(row["account_id"]), row["material_id"]
                _timing(row["scheduled_at"], current)
                if accounts is not None:
                    _account(accounts.get(str(aid), accounts.get(aid)), aid, owner, row["drama_language"])
                if materials is not None:
                    material = materials.get(mid, materials.get(int(mid)))
                    if not isinstance(material, dict) or canonical_drama_language(material.get("language", material.get("drama_language"))) != row["drama_language"]:
                        raise XPostError("x_video_material_changed", "素材数据已变更，请重新核对", 409)
                    rendered = render_caption(row["caption"], material.get("drama_name", material.get("name")), material.get("drama_description", material.get("description", material.get("desc"))), "https://preview.invalid/0.html")
                    if rendered != row["rendered_caption"]:
                        raise XPostError("x_video_material_changed", "素材文案数据已变更，请重新核对", 409)
                if row["caption_source"] == "default" and self.get_settings(actor, aid, db)["version"] != row["config_version"]:
                    raise XPostError("x_video_config_conflict", "账号默认文案已变更，请重新核对", 409)
                self._occupancy(db, aid, mid, allow_success=allow_repeat_success)
            mids = sorted({r["material_id"] for r in rows})
            if db.execute("SELECT 1 FROM x_post_manual_material_reservation WHERE state='active' AND material_key IN (%s) LIMIT 1" % ",".join("?" for _ in mids), mids).fetchone():
                raise XPostError("x_post_manual_material_unavailable", "素材已被其他待执行批次预约", 409)
            timestamp = _stamp(current)
            actor_name = str(actor.get("name") or actor.get("email") or actor["user_id"]).strip()[:255]
            batch_id = db.execute("INSERT INTO x_video_publish_batch(tenant_key,user_id,actor_name,source,idempotency_key,preview_token_hash,payload_hash,created_at) VALUES(?,?,?,?,?,?,?,?)", owner + (actor_name, frozen["source"], idempotency_key, frozen["token_hash"], frozen["payload_hash"], timestamp)).lastrowid
            anchors = {}
            for row in rows:
                aid, mid = int(row["account_id"]), row["material_id"]
                effective = _now(row["scheduled_at"]) if row["scheduled_at"] else current
                run_date = effective.astimezone(BEIJING_TZ).date()
                key = "x-video:%s:%s:%s" % (batch_id, aid, mid)
                run_id = db.execute("""INSERT INTO x_post_manual_run(
                  idempotency_key,trigger_source,publish_mode,scheduled_at,scheduled_timezone,
                  run_date,source_date,account_ids_json,material_ids_json,body_template,
                  random_template_enabled,actor_user_id,actor_tenant_key,actor_name,status,
                  expected_count,created_at,updated_at,duration_policy_version,copy_contract_version)
                  VALUES(?,'manual',?,?,'Asia/Shanghai',?,?,?,?,?,?,?,?,?,'queued',1,?,?,2,2)""",
                  (key, "scheduled" if row["scheduled_at"] else "immediate", row["scheduled_at"], str(run_date), str(run_date - timedelta(days=1)), _json([aid]), _json([mid]), row["caption"], int(row["processing"] == "random"), owner[1], owner[0], actor_name, timestamp, timestamp)).lastrowid
                db.execute("""INSERT INTO x_video_publish_task(batch_id,manual_run_id,account_id,material_id,drama_language,caption,caption_source,config_version,scheduled_at,processing,row_number,source_snapshot_json,account_snapshot_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (batch_id, run_id, aid, mid, row["drama_language"], row["caption"], row["caption_source"], row["config_version"], row["scheduled_at"], row["processing"], row["row_number"], _json(row["source_snapshot"]), _json(row["account_snapshot"]), timestamp))
                anchors.setdefault(mid, run_id)
            for mid, run_id in anchors.items():
                db.execute("INSERT INTO x_post_manual_material_reservation(manual_run_id,material_key,state,release_reason,created_at,updated_at) VALUES(?,?,'active','',?,?)", (run_id, mid, timestamp, timestamp))
            db.execute("UPDATE x_video_publish_preview SET batch_id=? WHERE token_hash=?", (batch_id, frozen["token_hash"]))
            return dict(self._batch(db, actor, batch_id), created=True)

    def _batch(self, conn, actor, batch_id):
        row = conn.execute("SELECT * FROM x_video_publish_batch WHERE id=? AND tenant_key=? AND user_id=?", (int(batch_id),) + actor_subject(actor)).fetchone()
        if not row:
            raise XPostError("x_video_batch_not_found", "发布批次不存在", 404)
        result = {k: row[k] for k in ("id", "source", "created_at", "idempotency_key")}
        tasks = conn.execute("""SELECT t.*,r.status AS run_status,r.error_code,r.error_message,
          q.id AS queue_id,q.status AS queue_status,l.id AS log_id,l.status AS log_status,
          l.unknown_outcome,l.x_post_id,l.x_post_url,l.post_text
          FROM x_video_publish_task t JOIN x_post_manual_run r ON r.id=t.manual_run_id
          LEFT JOIN x_post_queue q ON q.manual_run_id=r.id
          LEFT JOIN x_post_publish_log l ON l.queue_id=q.id WHERE t.batch_id=? ORDER BY t.id""", (int(batch_id),)).fetchall()
        result["tasks"] = []
        for task in tasks:
            item = dict(task)
            item["source_snapshot"] = json.loads(item.pop("source_snapshot_json"))
            item["account_snapshot"] = json.loads(item.pop("account_snapshot_json"))
            if item["unknown_outcome"] or item["run_status"] == "needs_review" or item["log_status"] in {"post_creating", "repost_creating"}:
                status = "needs_review"
            elif item["queue_status"] == "published":
                status = "success"
            elif item["queue_status"] == "failed" or item["run_status"] in {"failed_preflight", "completed_with_errors", "stopped"}:
                status = "failed"
            elif item["run_status"] == "running" or item["queue_status"] in {"reserved", "publishing"}:
                status = "processing"
            else:
                status = "waiting"
            item["status"] = status
            item["account_id"] = str(item["account_id"])
            item["unknown_outcome"] = bool(item["unknown_outcome"])
            result["tasks"].append(item)
        counts = {s: sum(t["status"] == s for t in result["tasks"]) for s in ("waiting", "processing", "success", "failed", "needs_review")}
        stopped = any(t["run_status"] == "stopped" for t in result["tasks"])
        result.update(counts=counts, expected_count=len(result["tasks"]), status="needs_review" if counts["needs_review"] else "stopped" if stopped else "running" if counts["processing"] else "queued" if counts["waiting"] else "completed_with_errors" if counts["failed"] else "completed")
        result["blocked_reason"] = "结果待核对，已停止后续任务" if counts["needs_review"] else "批次已停止，剩余任务不会自动重试" if stopped else ""
        return result

    def get_batch(self, actor, batch_id):
        with self._connection() as conn:
            return self._batch(conn, actor, batch_id)

    def list_batches(self, actor, limit=50, offset=0):
        owner = actor_subject(actor)
        limit, offset = max(1, min(100, int(limit))), max(0, int(offset))
        with self._connection() as conn:
            ids = conn.execute("SELECT id FROM x_video_publish_batch WHERE tenant_key=? AND user_id=? ORDER BY id DESC LIMIT ? OFFSET ?", owner + (limit, offset)).fetchall()
            total = conn.execute("SELECT COUNT(*) FROM x_video_publish_batch WHERE tenant_key=? AND user_id=?", owner).fetchone()[0]
            return {"items": [self._batch(conn, actor, r[0]) for r in ids], "total": total, "limit": limit, "offset": offset}

    def operations(self, actor, batch_id):
        return {"items": self.get_batch(actor, batch_id)["tasks"]}

    def operation(self, actor, idempotency_key):
        with self._connection() as conn:
            row = conn.execute("SELECT id FROM x_video_publish_batch WHERE tenant_key=? AND user_id=? AND idempotency_key=?", actor_subject(actor) + (str(idempotency_key),)).fetchone()
            return self._batch(conn, actor, row[0]) if row else None

    def get_task_for_run(self, run_id):
        with self._connection() as conn:
            task = personal_task(conn, run_id)
        if task is not None:
            task["account_snapshot"] = json.loads(task["account_snapshot_json"])
            task["source_snapshot"] = json.loads(task["source_snapshot_json"])
        return task

    def assert_run_publishable(self, run_id, account=None):
        with self._connection() as conn:
            task = personal_task(conn, run_id)
            if task is None:
                return None
            if personal_batch_held(conn, task["batch_id"]):
                raise XPostError("x_post_unknown_outcome", "本批次已暂停，请先核对已有发布结果", 409, True)
            if account is not None:
                _account(account, task["account_id"], (task["tenant_key"], task["user_id"]), task["drama_language"])
            return task
