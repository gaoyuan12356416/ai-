"""Additive SQLite ledger, immutable template versions, schedules and leases."""

from __future__ import annotations

import json
import random
import re
import sqlite3
import threading
from bisect import bisect_left
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from .links import FBPostLinkError, build_short_url, build_w2a_url
from .repositories import MaterialRepository, PagePoolRepository
from .validation import config_hash, expected_version, normalize_template_payload


UTC = timezone.utc
BEIJING = timezone(timedelta(hours=8))


class StoreError(RuntimeError):
    def __init__(self, code: str, message: str, status: int = 400):
        self.code, self.status = code, status
        super().__init__(message)


@dataclass(frozen=True)
class ActorScope:
    user_id: str
    name: str
    is_admin: bool
    owner_user_id: str

    @classmethod
    def from_payload(cls, value: Any) -> "ActorScope":
        if not isinstance(value, Mapping) or set(value) != {"user_id", "name", "is_admin", "owner_user_id"}:
            raise StoreError("invalid_request", "操作人范围无效", 400)
        owner = str(value.get("owner_user_id") or "").strip()
        admin = value.get("is_admin") is True
        if not admin and (not owner.isdigit() or owner == "0"):
            raise StoreError("fb_auto_owner_mapping_missing", "当前账号未唯一映射到Page池负责人", 403)
        return cls(str(value.get("user_id") or "")[:128], str(value.get("name") or "")[:200], admin, owner)


def utc_now() -> datetime:
    return datetime.now(UTC)


def utc_iso(value: Optional[datetime] = None) -> str:
    current = value or utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.astimezone(UTC).isoformat(timespec="seconds")


def _loads(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return fallback


class _ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


class FBAutoPostStore:
    def __init__(self, path: str | Path, *, now_fn=utc_now, rng: random.Random | None = None, max_late_seconds: int = 600):
        self.path = str(path)
        self.now_fn, self.rng = now_fn, rng or random.SystemRandom()
        self.max_late_seconds = int(max_late_seconds)
        self._lock = threading.RLock()
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.ensure_storage()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None, factory=_ClosingConnection)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def ensure_storage(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS fb_auto_template(
          id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'disabled',
          current_version INTEGER NOT NULL DEFAULT 1,owner_user_id TEXT NOT NULL DEFAULT '',scope_is_admin INTEGER NOT NULL DEFAULT 0,
          created_by TEXT NOT NULL DEFAULT '',created_name TEXT NOT NULL DEFAULT '',created_at_utc TEXT NOT NULL,updated_at_utc TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS fb_auto_template_version(
          template_id INTEGER NOT NULL,version INTEGER NOT NULL,config_json TEXT NOT NULL,config_sha256 TEXT NOT NULL,
          created_by TEXT NOT NULL DEFAULT '',created_at_utc TEXT NOT NULL,PRIMARY KEY(template_id,version),
          FOREIGN KEY(template_id) REFERENCES fb_auto_template(id)
        );
        CREATE TABLE IF NOT EXISTS fb_auto_schedule_plan(
          template_id INTEGER NOT NULL,template_version INTEGER NOT NULL,local_date TEXT NOT NULL,times_json TEXT NOT NULL,
          created_at_utc TEXT NOT NULL,PRIMARY KEY(template_id,template_version,local_date)
        );
        CREATE TABLE IF NOT EXISTS fb_auto_run(
          id INTEGER PRIMARY KEY AUTOINCREMENT,template_id INTEGER NOT NULL,template_version INTEGER NOT NULL,
          slot_key TEXT NOT NULL,trigger_type TEXT NOT NULL,status TEXT NOT NULL,config_json TEXT NOT NULL,
          total_pages INTEGER NOT NULL DEFAULT 0,publishable_pages INTEGER NOT NULL DEFAULT 0,missing_token_pages INTEGER NOT NULL DEFAULT 0,
          queued_tasks INTEGER NOT NULL DEFAULT 0,skipped_tasks INTEGER NOT NULL DEFAULT 0,created_at_utc TEXT NOT NULL,completed_at_utc TEXT NOT NULL DEFAULT '',
          UNIQUE(template_id,slot_key)
        );
        CREATE TABLE IF NOT EXISTS fb_auto_due_slot(
          id INTEGER PRIMARY KEY AUTOINCREMENT,template_id INTEGER NOT NULL,template_version INTEGER NOT NULL,
          slot_key TEXT NOT NULL,planned_publish_at_utc TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',
          lease_owner TEXT NOT NULL DEFAULT '',lease_expires_at_utc TEXT NOT NULL DEFAULT '',run_id INTEGER,
          error_code TEXT NOT NULL DEFAULT '',created_at_utc TEXT NOT NULL,updated_at_utc TEXT NOT NULL,
          UNIQUE(template_id,slot_key)
        );
        CREATE TABLE IF NOT EXISTS fb_auto_scheduler_state(
          state_key TEXT PRIMARY KEY,watermark_minute_utc TEXT NOT NULL,updated_at_utc TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS fb_auto_metric_generation(
          id INTEGER PRIMARY KEY AUTOINCREMENT,generation_key TEXT NOT NULL UNIQUE,platform INTEGER NOT NULL,
          metric_date TEXT NOT NULL,product TEXT NOT NULL,status TEXT NOT NULL,row_count INTEGER NOT NULL DEFAULT 0,
          checksum TEXT NOT NULL DEFAULT '',refreshed_at_utc TEXT NOT NULL,created_at_utc TEXT NOT NULL,ready_at_utc TEXT NOT NULL DEFAULT '',
          error_code TEXT NOT NULL DEFAULT '',error_message TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS fb_auto_metric_daily(
          generation_id INTEGER NOT NULL,content_id TEXT NOT NULL,material_id TEXT NOT NULL,
          spend TEXT NOT NULL,af_revenue0 TEXT NOT NULL,PRIMARY KEY(generation_id,content_id,material_id),
          FOREIGN KEY(generation_id) REFERENCES fb_auto_metric_generation(id)
        );
        CREATE TABLE IF NOT EXISTS fb_auto_metric_active_pointer(
          platform INTEGER NOT NULL,metric_date TEXT NOT NULL,product TEXT NOT NULL,generation_id INTEGER NOT NULL,
          activated_at_utc TEXT NOT NULL,PRIMARY KEY(platform,metric_date,product),
          FOREIGN KEY(generation_id) REFERENCES fb_auto_metric_generation(id)
        );
        CREATE TABLE IF NOT EXISTS fb_auto_run_page(
          run_id INTEGER NOT NULL,page_id TEXT NOT NULL,group_id TEXT NOT NULL,group_ids_json TEXT NOT NULL DEFAULT '[]',owner_user_id TEXT NOT NULL,
          timezone TEXT NOT NULL DEFAULT '',language TEXT NOT NULL DEFAULT '',eligible_token_count INTEGER NOT NULL DEFAULT 0,
          snapshot_status TEXT NOT NULL,skip_reason TEXT NOT NULL DEFAULT '',PRIMARY KEY(run_id,page_id)
        );
        CREATE TABLE IF NOT EXISTS fb_auto_task(
          id INTEGER PRIMARY KEY AUTOINCREMENT,run_id INTEGER NOT NULL,template_id INTEGER NOT NULL,template_version INTEGER NOT NULL,
          page_id TEXT NOT NULL,group_id TEXT NOT NULL,status TEXT NOT NULL,skip_reason TEXT NOT NULL DEFAULT '',
          material_id TEXT NOT NULL DEFAULT '',content_id TEXT NOT NULL DEFAULT '',media_url TEXT NOT NULL DEFAULT '',message_text TEXT NOT NULL DEFAULT '',
          short_url TEXT NOT NULL DEFAULT '',long_url TEXT NOT NULL DEFAULT '',
          lease_owner TEXT NOT NULL DEFAULT '',lease_expires_at_utc TEXT NOT NULL DEFAULT '',attempt_count INTEGER NOT NULL DEFAULT 0,
          graph_post_id TEXT NOT NULL DEFAULT '',error_code TEXT NOT NULL DEFAULT '',error_message TEXT NOT NULL DEFAULT '',unknown_outcome INTEGER NOT NULL DEFAULT 0,
          created_at_utc TEXT NOT NULL,started_at_utc TEXT NOT NULL DEFAULT '',completed_at_utc TEXT NOT NULL DEFAULT '',UNIQUE(run_id,page_id)
        );
        CREATE TABLE IF NOT EXISTS fb_auto_publish_ledger(
          task_id INTEGER PRIMARY KEY,page_id TEXT NOT NULL,material_id TEXT NOT NULL,status TEXT NOT NULL,
          graph_post_id TEXT NOT NULL DEFAULT '',definite_attempts INTEGER NOT NULL DEFAULT 0,unknown_outcome INTEGER NOT NULL DEFAULT 0,
          error_code TEXT NOT NULL DEFAULT '',created_at_utc TEXT NOT NULL,updated_at_utc TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS fb_auto_publish_attempt(
          id INTEGER PRIMARY KEY AUTOINCREMENT,task_id INTEGER NOT NULL,sequence INTEGER NOT NULL,
          credential_id TEXT NOT NULL DEFAULT '',fb_user_id TEXT NOT NULL DEFAULT '',result_kind TEXT NOT NULL,
          error_code TEXT NOT NULL DEFAULT '',trace_id TEXT NOT NULL DEFAULT '',created_at_utc TEXT NOT NULL,
          UNIQUE(task_id,sequence)
        );
        CREATE INDEX IF NOT EXISTS idx_fb_auto_task_claim ON fb_auto_task(status,id);
        CREATE INDEX IF NOT EXISTS idx_fb_auto_page_material ON fb_auto_task(page_id,material_id,created_at_utc);
        CREATE INDEX IF NOT EXISTS idx_fb_auto_run_created ON fb_auto_run(created_at_utc,id);
        CREATE INDEX IF NOT EXISTS idx_fb_auto_due_claim ON fb_auto_due_slot(status,planned_publish_at_utc,id);
        CREATE UNIQUE INDEX IF NOT EXISTS uq_fb_auto_active_page ON fb_auto_task(page_id)
          WHERE status IN ('queued','running','submitted');
        CREATE UNIQUE INDEX IF NOT EXISTS uq_fb_auto_unresolved_page ON fb_auto_task(page_id)
          WHERE status IN ('queued','running','submitted','unknown');
        """
        with self.connect() as conn:
            conn.executescript(schema)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(fb_auto_run_page)")}
            if "group_ids_json" not in columns:
                conn.execute("ALTER TABLE fb_auto_run_page ADD COLUMN group_ids_json TEXT NOT NULL DEFAULT '[]'")
            self._ensure_columns(conn, "fb_auto_run", {
                "planned_publish_at_utc": "TEXT NOT NULL DEFAULT ''",
                "metric_generation_ids_json": "TEXT NOT NULL DEFAULT '[]'",
                "video_template": "TEXT NOT NULL DEFAULT ''",
            })
            self._ensure_columns(conn, "fb_auto_task", {
                "planned_publish_at_utc": "TEXT NOT NULL DEFAULT ''",
                "video_template": "TEXT NOT NULL DEFAULT ''",
                "gpu_job_id": "TEXT NOT NULL DEFAULT ''",
                "source_media_url": "TEXT NOT NULL DEFAULT ''",
                "prepared_media_url": "TEXT NOT NULL DEFAULT ''",
                "prepared_sha256": "TEXT NOT NULL DEFAULT ''",
                "prepared_size_bytes": "INTEGER NOT NULL DEFAULT 0",
                "prepared_duration_seconds": "TEXT NOT NULL DEFAULT ''",
                "prepared_profile": "TEXT NOT NULL DEFAULT ''",
                "prepared_at_utc": "TEXT NOT NULL DEFAULT ''",
                "next_prepare_at_utc": "TEXT NOT NULL DEFAULT ''",
                "next_reconcile_at_utc": "TEXT NOT NULL DEFAULT ''",
                "short_url": "TEXT NOT NULL DEFAULT ''",
                "long_url": "TEXT NOT NULL DEFAULT ''",
            })
            self._ensure_columns(conn, "fb_auto_due_slot", {"available_at_utc": "TEXT NOT NULL DEFAULT ''", "trigger_type": "TEXT NOT NULL DEFAULT 'auto'"})
            conn.execute("DROP INDEX IF EXISTS uq_fb_auto_active_page")
            conn.execute("DROP INDEX IF EXISTS uq_fb_auto_unresolved_page")
            conn.execute("DROP INDEX IF EXISTS uq_fb_auto_active_page_slot")
            conn.execute("DROP INDEX IF EXISTS uq_fb_auto_execution_page")
            conn.execute("CREATE UNIQUE INDEX uq_fb_auto_active_page_slot ON fb_auto_task(page_id,planned_publish_at_utc) WHERE status IN ('planned','preparing','ready','running','submitted')")
            conn.execute("CREATE UNIQUE INDEX uq_fb_auto_execution_page ON fb_auto_task(page_id) WHERE status IN ('running','submitted','unknown')")
            conn.execute("BEGIN IMMEDIATE")
            migration_now = utc_iso(self.now_fn())
            legacy_runs = [int(row[0]) for row in conn.execute("SELECT DISTINCT run_id FROM fb_auto_task WHERE status='ready' AND TRIM(prepared_at_utc)=''")]
            conn.execute(
                "UPDATE fb_auto_task SET status='skipped',skip_reason='fb_auto_legacy_ready_unverified',error_code='fb_auto_legacy_ready_unverified',error_message='历史成片缺少可验证的制作完成时间，已安全终止且不会发布',lease_owner='',lease_expires_at_utc='',completed_at_utc=? WHERE status='ready' AND TRIM(prepared_at_utc)=''",
                (migration_now,),
            )
            for run_id in legacy_runs:
                self._refresh_run(conn, run_id, migration_now)
            conn.commit()

    @staticmethod
    def _ensure_columns(conn: sqlite3.Connection, table: str, columns: Mapping[str, str]) -> None:
        existing = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, definition in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    @staticmethod
    def _scope_clause(actor: ActorScope, alias: str = "t") -> tuple[str, tuple[Any, ...]]:
        return ("", ()) if actor.is_admin else (f" AND {alias}.owner_user_id=?", (actor.owner_user_id,))

    def _template_row(self, conn: sqlite3.Connection, template_id: int, actor: ActorScope) -> sqlite3.Row:
        clause, params = self._scope_clause(actor)
        row = conn.execute(f"SELECT t.*,v.config_json,v.config_sha256 FROM fb_auto_template t JOIN fb_auto_template_version v ON v.template_id=t.id AND v.version=t.current_version WHERE t.id=?{clause}", (template_id,) + params).fetchone()
        if row is None:
            raise StoreError("fb_auto_template_not_found", "自动发布模板不存在", 404)
        return row

    @staticmethod
    def _template_dto(row: Mapping[str, Any]) -> Dict[str, Any]:
        return {"id": int(row["id"]), "name": row["name"], "status": row["status"], "version": int(row["current_version"]), "owner_user_id": row["owner_user_id"], "scope_is_admin": bool(row["scope_is_admin"]), "config": _loads(row["config_json"], {}), "config_sha256": row["config_sha256"], "created_at_utc": row["created_at_utc"], "updated_at_utc": row["updated_at_utc"]}

    def create_template(self, raw: Any, actor: ActorScope, resolved_source: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
        config, now = normalize_template_payload(raw), utc_iso(self.now_fn())
        if resolved_source:
            for key in ("app_id", "product", "metric_product", "metric_platform", "material_data_source"):
                if key in resolved_source: config[key] = resolved_source[key]
        with self._lock, self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute("INSERT INTO fb_auto_template(name,status,current_version,owner_user_id,scope_is_admin,created_by,created_name,created_at_utc,updated_at_utc) VALUES(?,'disabled',1,?,?,?,?,?,?)", (config["name"], actor.owner_user_id, int(actor.is_admin), actor.user_id, actor.name, now, now))
            template_id = int(cur.lastrowid)
            encoded = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            conn.execute("INSERT INTO fb_auto_template_version VALUES(?,?,?,?,?,?)", (template_id, 1, encoded, config_hash(config), actor.user_id, now))
            conn.commit()
            return self._template_dto(self._template_row(conn, template_id, actor))

    def update_template(self, template_id: int, raw: Any, actor: ActorScope, version: Any, resolved_source: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
        config, expected, now = normalize_template_payload(raw), expected_version(version), utc_iso(self.now_fn())
        if resolved_source:
            for key in ("app_id", "product", "metric_product", "metric_platform", "material_data_source"):
                if key in resolved_source: config[key] = resolved_source[key]
        with self._lock, self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._template_row(conn, template_id, actor)
            if int(row["current_version"]) != expected:
                raise StoreError("fb_auto_template_version_conflict", "模板已被其他人更新，请刷新后重试", 409)
            if conn.execute("SELECT 1 FROM fb_auto_task WHERE template_id=? AND status='running' LIMIT 1", (template_id,)).fetchone():
                raise StoreError("fb_auto_template_running_change_denied", "模板存在正在向Meta提交的任务，请等待发布结果落账后再停用或修改", 409)
            if row["status"] == "enabled":
                raise StoreError(
                    "fb_auto_enabled_template_edit_denied",
                    "已启用模板不能直接编辑，请先停用后再修改",
                    409,
                )
            new_version = expected + 1
            encoded = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            conn.execute("INSERT INTO fb_auto_template_version VALUES(?,?,?,?,?,?)", (template_id, new_version, encoded, config_hash(config), actor.user_id, now))
            conn.execute("UPDATE fb_auto_template SET name=?,current_version=?,updated_at_utc=? WHERE id=?", (config["name"], new_version, now, template_id))
            stale_runs = [
                int(item[0])
                for item in conn.execute(
                    "SELECT DISTINCT run_id FROM fb_auto_task WHERE template_id=? AND template_version<>? AND status IN ('planned','ready')",
                    (template_id, new_version),
                )
            ]
            conn.execute(
                "UPDATE fb_auto_task SET status='skipped',skip_reason='fb_auto_template_version_changed',error_code='fb_auto_template_version_changed',error_message='模板已更新，旧版本任务不再制作或发布',lease_owner='',lease_expires_at_utc='',next_prepare_at_utc='',completed_at_utc=? WHERE template_id=? AND template_version<>? AND status IN ('planned','ready')",
                (now, template_id, new_version),
            )
            conn.execute(
                "UPDATE fb_auto_due_slot SET status='missed',error_code='fb_auto_due_slot_template_changed',lease_owner='',lease_expires_at_utc='',updated_at_utc=? WHERE template_id=? AND template_version<>? AND status='pending'",
                (now, template_id, new_version),
            )
            for run_id in stale_runs:
                self._refresh_run(conn, run_id, now)
            conn.commit()
            return self._template_dto(self._template_row(conn, template_id, actor))

    def set_template_status(self, template_id: int, enabled: bool, actor: ActorScope, version: Any, *, expected_enabled_fingerprint: Sequence[tuple[int,int]] | None = None) -> Dict[str, Any]:
        expected, now = expected_version(version), utc_iso(self.now_fn())
        with self._lock, self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._template_row(conn, template_id, actor)
            if int(row["current_version"]) != expected:
                raise StoreError("fb_auto_template_version_conflict", "模板版本冲突", 409)
            if not enabled and conn.execute("SELECT 1 FROM fb_auto_task WHERE template_id=? AND status='running' LIMIT 1", (template_id,)).fetchone():
                raise StoreError("fb_auto_template_running_change_denied", "模板存在正在向Meta提交的任务，请等待发布结果落账后再停用", 409)
            if enabled and expected_enabled_fingerprint is not None:
                current = {(int(item[0]),int(item[1])) for item in conn.execute("SELECT id,current_version FROM fb_auto_template WHERE status='enabled' AND id<>?", (template_id,))}
                if current != set(expected_enabled_fingerprint):
                    raise StoreError("fb_auto_capacity_snapshot_changed", "启用模板集合在校验期间变化，请重新启用以复核容量", 409)
            if not enabled:
                conn.execute(
                    "UPDATE fb_auto_due_slot SET status='pending',error_code='fb_auto_due_slot_template_disabled',lease_owner='',lease_expires_at_utc='',available_at_utc=?,updated_at_utc=? WHERE template_id=? AND trigger_type='auto' AND status='preparing'",
                    (now, now, template_id),
                )
                manual_runs = [
                    int(item[0])
                    for item in conn.execute(
                        "SELECT DISTINCT x.run_id FROM fb_auto_task x JOIN fb_auto_run r ON r.id=x.run_id WHERE x.template_id=? AND r.trigger_type='manual' AND x.status IN ('planned','preparing','ready')",
                        (template_id,),
                    )
                ]
                conn.execute(
                    "UPDATE fb_auto_due_slot SET status='failed',error_code='fb_auto_manual_template_disabled',lease_owner='',lease_expires_at_utc='',available_at_utc='',updated_at_utc=? WHERE template_id=? AND trigger_type='manual' AND status IN ('pending','preparing')",
                    (now, template_id),
                )
                conn.execute(
                    "UPDATE fb_auto_task SET status='skipped',skip_reason='fb_auto_manual_template_disabled',error_code='fb_auto_manual_template_disabled',error_message='模板已停用，手动执行任务已取消',lease_owner='',lease_expires_at_utc='',next_prepare_at_utc='',completed_at_utc=? WHERE id IN (SELECT x.id FROM fb_auto_task x JOIN fb_auto_run r ON r.id=x.run_id WHERE x.template_id=? AND r.trigger_type='manual' AND x.status IN ('planned','preparing','ready'))",
                    (now, template_id),
                )
                for run_id in manual_runs:
                    self._refresh_run(conn, run_id, now)
            target_status = "enabled" if enabled else "disabled"
            if row["status"] != target_status:
                conn.execute("UPDATE fb_auto_template SET status=?,updated_at_utc=? WHERE id=?", (target_status, now, template_id))
            conn.commit()
            return self._template_dto(self._template_row(conn, template_id, actor))

    def enabled_group_conflicts(self, template_id: int, group_ids: Sequence[str]) -> List[Dict[str, Any]]:
        wanted = set(str(item) for item in group_ids)
        conflicts: List[Dict[str, Any]] = []
        with self.connect() as conn:
            rows = conn.execute("SELECT t.id,t.name,v.config_json FROM fb_auto_template t JOIN fb_auto_template_version v ON v.template_id=t.id AND v.version=t.current_version WHERE t.status='enabled' AND t.id<>?", (template_id,)).fetchall()
        for row in rows:
            overlap = sorted(wanted.intersection(_loads(row["config_json"], {}).get("group_ids", [])))
            if overlap:
                conflicts.append({"template_id": int(row["id"]), "template_name": row["name"], "group_ids": overlap})
        return conflicts

    def enabled_template_sources(self, exclude_template_id: int) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT t.id,t.name,t.current_version,t.owner_user_id,t.scope_is_admin,v.config_json FROM fb_auto_template t JOIN fb_auto_template_version v ON v.template_id=t.id AND v.version=t.current_version WHERE t.status='enabled' AND t.id<>? ORDER BY t.id", (exclude_template_id,)).fetchall()
        return [{"template_id": int(row["id"]), "template_version": int(row["current_version"]), "template_name": row["name"], "owner_user_id": row["owner_user_id"], "scope_is_admin": bool(row["scope_is_admin"]), "config": _loads(row["config_json"], {})} for row in rows]

    def list_templates(self, actor: ActorScope, *, status: str = "", query: str = "", limit: int = 50, offset: int = 0) -> Dict[str, Any]:
        clause, params = self._scope_clause(actor)
        filters, values = ["1=1" + clause], list(params)
        if status:
            if status not in {"enabled", "disabled"}:
                raise StoreError("invalid_request", "模板状态筛选无效", 400)
            filters.append("t.status=?"); values.append(status)
        if query:
            filters.append("t.name LIKE ? ESCAPE '\\'"); values.append("%" + query.replace("%", "\\%").replace("_", "\\_") + "%")
        where = " AND ".join(filters)
        with self.connect() as conn:
            total = int(conn.execute(f"SELECT COUNT(*) FROM fb_auto_template t WHERE {where}", values).fetchone()[0])
            rows = conn.execute(f"SELECT t.*,v.config_json,v.config_sha256 FROM fb_auto_template t JOIN fb_auto_template_version v ON v.template_id=t.id AND v.version=t.current_version WHERE {where} ORDER BY t.id DESC LIMIT ? OFFSET ?", values + [limit, offset]).fetchall()
        return {"ok": True, "items": [self._template_dto(row) for row in rows], "total": total, "limit": limit, "offset": offset}

    def get_template(self, template_id: int, actor: ActorScope) -> Dict[str, Any]:
        with self.connect() as conn:
            return self._template_dto(self._template_row(conn, template_id, actor))

    def _cooldown_material_ids(self, conn: sqlite3.Connection, page_id: str, days: int) -> set[str]:
        active = ("planned", "preparing", "ready", "running", "submitted", "unknown")
        historical = ("published", "failed_without_retry")
        placeholders = ",".join("?" for _ in active)
        sql = f"SELECT DISTINCT material_id FROM fb_auto_task WHERE page_id=? AND material_id<>'' AND (status IN ({placeholders})"
        params: List[Any] = [page_id, *active]
        if days > 0:
            history_placeholders = ",".join("?" for _ in historical)
            sql += f" OR (status IN ({history_placeholders}) AND created_at_utc>=?)"
            params.extend((*historical, utc_iso(self.now_fn() - timedelta(days=days))))
        sql += ")"
        return {str(row[0]) for row in conn.execute(sql, params)}

    @staticmethod
    def _message(config: Mapping[str, Any], material: Any, short_url: str = "") -> str:
        replacements = {
            "drama_name": material.drama_name,
            "material_name": material.material_name,
            "content_id": material.content_id,
            "desc": getattr(material, "drama_description", ""),
            "url": short_url,
        }
        text = re.sub(
            r"\{\{(drama_name|material_name|content_id|desc|url)\}\}",
            lambda match: str(replacements[match.group(1)] or ""),
            str(config["message_template"]),
        )
        if short_url and len(text) > 5000:
            raise StoreError("fb_auto_message_length_invalid", "宏展开后的发布文案超过5000字符，未创建任务", 409)
        return text[:5000]

    @staticmethod
    def _link_values(page: Any, material: Any, task_id: int, timestamp: int) -> tuple[str, str]:
        try:
            short_url = build_short_url(task_id)
            long_url = build_w2a_url({
                "username": page.page_id,
                "timestamp": timestamp,
                "material_language": material.language,
                "drama_name": material.drama_name or material.content_id,
                "tag": getattr(material, "material_tag", "") or "FBauto",
                "task_id": task_id,
                "page_name": getattr(page, "page_name", "") or page.page_id,
                "page_id": page.page_id,
                "material_name": material.material_name or material.material_id,
                "material_id": material.material_id,
                "content_id": material.content_id,
            })
            return short_url, long_url
        except FBPostLinkError as exc:
            raise StoreError(exc.code, str(exc), exc.status) from None

    def _validate_due_claim(
        self,
        conn: sqlite3.Connection,
        *,
        template_id: int,
        slot_key: str,
        trigger_type: str,
        expected_template_version: int,
        expected_due_id: int | None,
        expected_due_lease_owner: str | None,
        expected_due_lease_expires_at_utc: str | None,
    ) -> sqlite3.Row:
        due = conn.execute(
            "SELECT id,status,template_version,trigger_type,error_code,lease_owner,lease_expires_at_utc FROM fb_auto_due_slot WHERE template_id=? AND slot_key=?",
            (template_id, slot_key),
        ).fetchone()
        paused = (
            trigger_type == "auto"
            and due is not None
            and due["status"] == "pending"
            and due["error_code"] == "fb_auto_due_slot_template_disabled"
        )
        if paused:
            raise StoreError("fb_auto_due_slot_template_disabled", "模板停用已撤销本次自动计划；重新启用后可重新领取", 409)
        if (due is None or due["status"] != "preparing"
                or int(due["template_version"]) != int(expected_template_version)
                or str(due["trigger_type"] or "auto") != trigger_type
                or (expected_due_id is not None and int(due["id"]) != int(expected_due_id))):
            raise StoreError("fb_auto_due_slot_template_changed", "执行计划已取消或版本已变化，不再创建发布任务", 409)
        if ((expected_due_lease_owner is not None and str(due["lease_owner"] or "") != str(expected_due_lease_owner))
                or (expected_due_lease_expires_at_utc is not None and str(due["lease_expires_at_utc"] or "") != str(expected_due_lease_expires_at_utc))
                or not str(due["lease_expires_at_utc"] or "")
                or str(due["lease_expires_at_utc"]) <= utc_iso(self.now_fn())):
            raise StoreError("fb_auto_due_slot_lease_superseded", "计划租约已过期或被其他worker接管，本次结果不再落账", 409)
        return due

    @staticmethod
    def _target_page_ids(value: Sequence[str] | None) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or not value:
            raise StoreError("invalid_request", "定向回补Page范围无效", 400)
        result = tuple(sorted(str(item or "").strip() for item in value))
        if (
            len(result) != len(set(result))
            or not all(re.fullmatch(r"[1-9][0-9]{3,40}", item) for item in result)
        ):
            raise StoreError("invalid_request", "定向回补Page范围无效", 400)
        return result

    @staticmethod
    def _only_target_pages(page_rows: Sequence[Any], target_page_ids: Sequence[str]) -> List[Any]:
        if not target_page_ids:
            return list(page_rows)
        wanted = set(target_page_ids)
        selected = [page for page in page_rows if str(page.page_id) in wanted]
        by_id = {str(page.page_id): page for page in selected}
        missing = sorted(wanted.difference(by_id))
        if missing or len(selected) != len(by_id):
            error = StoreError("fb_auto_target_page_scope_changed", "定向回补Page范围已变化或存在重复，未创建运行", 409)
            error.conflicts = (
                [{"page_id": page_id, "status": "missing"} for page_id in missing[:20]]
                or [{"status": "duplicate"}]
            )
            raise error
        return [by_id[page_id] for page_id in target_page_ids]

    def create_run(self, template_id: int, slot_key: str, trigger_type: str, actor: ActorScope, pages: PagePoolRepository, materials: MaterialRepository, *, planned_publish_at_utc: str = "", expected_template_version: int | None = None, required_template_version: int | None = None, target_page_ids: Sequence[str] | None = None, expected_due_id: int | None = None, expected_due_lease_owner: str | None = None, expected_due_lease_expires_at_utc: str | None = None, max_publishable_pages: int | None = None, max_jobs_per_slot: int | None = None, max_daily_jobs: int | None = None) -> Dict[str, Any]:
        if trigger_type not in {"auto", "manual"} or not slot_key or len(slot_key) > 120:
            raise StoreError("invalid_request", "运行触发参数无效", 400)
        target_ids = self._target_page_ids(target_page_ids)
        try:
            required_version = None if required_template_version is None else int(required_template_version)
        except (TypeError, ValueError, OverflowError):
            raise StoreError("invalid_request", "定向回补模板版本无效", 400) from None
        if required_version is not None and required_version <= 0:
            raise StoreError("invalid_request", "定向回补模板版本无效", 400)
        if target_ids and (
            trigger_type != "manual"
            or required_version is None
            or expected_template_version is not None
        ):
            raise StoreError("invalid_request", "定向回补必须使用手动触发和独立模板版本锁", 400)
        version_guard = expected_template_version if expected_template_version is not None else required_version
        with self.connect() as conn:
            template = self._template_row(conn, template_id, actor)
            if version_guard is not None and int(template["current_version"]) != int(version_guard):
                raise StoreError("fb_auto_due_slot_template_changed", "模板版本已变化，原计划时隙不再执行", 409)
            if version_guard is not None and template["status"] != "enabled":
                code = "fb_auto_due_slot_template_disabled" if trigger_type == "auto" else "fb_auto_due_slot_template_changed"
                raise StoreError(code, "模板已停用，自动计划已暂停；重新启用后可在迟到宽限内继续" if trigger_type == "auto" else "模板已停用，手动执行计划已取消", 409)
            if expected_template_version is not None:
                self._validate_due_claim(
                    conn, template_id=template_id, slot_key=slot_key, trigger_type=trigger_type,
                    expected_template_version=int(expected_template_version), expected_due_id=expected_due_id,
                    expected_due_lease_owner=expected_due_lease_owner,
                    expected_due_lease_expires_at_utc=expected_due_lease_expires_at_utc,
                )
            if trigger_type == "auto" and (
                template["status"] != "enabled"
                or expected_template_version is None
            ):
                raise StoreError("fb_auto_due_slot_template_changed", "模板已停用或版本已变化，原计划时隙不再执行", 409)
            config = _loads(template["config_json"], {})
            scope_admin, scope_owner = bool(template["scope_is_admin"]), str(template["owner_user_id"])
            existing = conn.execute("SELECT id FROM fb_auto_run WHERE template_id=? AND slot_key=?", (template_id, slot_key)).fetchone()
            if existing and expected_template_version is None:
                return {"ok": True, "run_id": int(existing[0]), "idempotent": True}
            now_for_backlog = utc_iso(self.now_fn())
            active = conn.execute("SELECT DISTINCT r.id FROM fb_auto_run r JOIN fb_auto_task x ON x.run_id=r.id WHERE r.template_id=? AND x.status IN ('planned','preparing','ready','running') AND x.planned_publish_at_utc<=? LIMIT 1", (template_id, now_for_backlog)).fetchone()
            if active:
                error = StoreError("fb_auto_previous_run_backlog", "上一个时隙仍有Page任务未完成，本时隙不叠加", 409)
                error.conflicts = [{"run_id": int(active[0]), "status": "backlog"}]
                raise error
        legacy = pages.legacy_conflicts(config["group_ids"])
        if legacy:
            error = StoreError("fb_auto_legacy_queue_conflict", "所选Page池仍被旧版自动发布队列占用", 409)
            error.conflicts = legacy
            raise error
        exclusive = self.enabled_group_conflicts(template_id, config["group_ids"])
        if exclusive:
            error = StoreError("fb_auto_group_template_conflict", "所选Page池已被其他新版启用模板独占", 409)
            error.conflicts = exclusive
            raise error
        page_rows = self._only_target_pages(
            pages.list_pages(config["group_ids"], is_admin=scope_admin, owner_user_id=scope_owner),
            target_ids,
        )
        page_ids = {page.page_id for page in page_rows}
        drift_conflicts: List[Dict[str, Any]] = []
        enabled_others = self.enabled_template_sources(template_id)
        global_slot_jobs = sum(page.eligible_token_count > 0 for page in page_rows)
        enabled_fingerprint = {(template_id, int(template["current_version"]))}
        for other in enabled_others:
            enabled_fingerprint.add((int(other["template_id"]), int(other["template_version"])))
            other_pages = pages.list_pages(
                other["config"]["group_ids"],
                is_admin=other["scope_is_admin"],
                owner_user_id=other["owner_user_id"],
            )
            global_slot_jobs += sum(page.eligible_token_count > 0 for page in other_pages)
            overlap = sorted(
                page_ids.intersection(page.page_id for page in other_pages)
            )
            if overlap:
                drift_conflicts.append(
                    {
                        "template_id": other["template_id"],
                        "template_name": other["template_name"],
                        "overlap_count": len(overlap),
                        "page_ids": overlap[:20],
                    }
                )
        if drift_conflicts:
            error = StoreError(
                "fb_auto_page_template_conflict",
                "当前Page组成员与其他启用模板发生重叠，本时隙未创建",
                409,
            )
            error.conflicts = drift_conflicts[:20]
            raise error
        total, publishable = len(page_rows), sum(1 for page in page_rows if page.eligible_token_count > 0)
        schedule = config["schedule"]
        daily_count = len(schedule["times"]) if schedule["mode"] == "fixed" else int(schedule["daily_count"])
        if trigger_type == "auto" or any(value is not None for value in (max_publishable_pages,max_jobs_per_slot,max_daily_jobs)):
            if total == 0:
                raise StoreError("fb_auto_page_pool_empty", "所选Page池当前没有有效Page，未创建空运行", 409)
            if publishable == 0:
                raise StoreError("fb_auto_page_pool_unpublishable", "所选Page池当前没有可发布Page，未创建运行", 409)
            if ((max_publishable_pages is not None and publishable > int(max_publishable_pages))
                    or (max_jobs_per_slot is not None and global_slot_jobs > int(max_jobs_per_slot))
                    or (max_daily_jobs is not None and publishable * daily_count > int(max_daily_jobs))):
                raise StoreError("fb_auto_capacity_exceeded", f"运行前容量复核失败：可发布Page {publishable}/{max_publishable_pages or '-'}，全局最坏同槽GPU任务 {global_slot_jobs}/{max_jobs_per_slot or '-'}，本模板每日任务 {publishable * daily_count}/{max_daily_jobs or '-'}", 409)
        candidate_snapshot = materials.candidate_snapshot(config)
        # Catalog/metric freezing can be the longest read in planning.  Re-read
        # every mutable Page-side input afterwards so a pool disable, membership
        # growth, token loss, legacy enable, or sibling-template enable cannot
        # create an empty/over-capacity/conflicting run from a stale snapshot.
        legacy = pages.legacy_conflicts(config["group_ids"])
        if legacy:
            error = StoreError("fb_auto_legacy_queue_conflict", "所选Page池仍被旧版自动发布队列占用", 409)
            error.conflicts = legacy
            raise error
        exclusive = self.enabled_group_conflicts(template_id, config["group_ids"])
        if exclusive:
            error = StoreError("fb_auto_group_template_conflict", "所选Page池已被其他新版启用模板独占", 409)
            error.conflicts = exclusive
            raise error
        page_rows = self._only_target_pages(
            pages.list_pages(config["group_ids"], is_admin=scope_admin, owner_user_id=scope_owner),
            target_ids,
        )
        page_ids, global_slot_jobs = {page.page_id for page in page_rows}, sum(page.eligible_token_count > 0 for page in page_rows)
        enabled_fingerprint = {(template_id, int(template["current_version"]))}
        drift_conflicts = []
        for other in self.enabled_template_sources(template_id):
            enabled_fingerprint.add((int(other["template_id"]), int(other["template_version"])))
            other_pages = pages.list_pages(other["config"]["group_ids"], is_admin=other["scope_is_admin"], owner_user_id=other["owner_user_id"])
            global_slot_jobs += sum(page.eligible_token_count > 0 for page in other_pages)
            overlap = sorted(page_ids.intersection(page.page_id for page in other_pages))
            if overlap:
                drift_conflicts.append({"template_id": other["template_id"], "template_name": other["template_name"], "overlap_count": len(overlap), "page_ids": overlap[:20]})
        if drift_conflicts:
            error = StoreError("fb_auto_page_template_conflict", "当前Page组成员与其他启用模板发生重叠，本时隙未创建", 409)
            error.conflicts = drift_conflicts[:20]
            raise error
        total, publishable = len(page_rows), sum(page.eligible_token_count > 0 for page in page_rows)
        if trigger_type == "auto" or any(value is not None for value in (max_publishable_pages,max_jobs_per_slot,max_daily_jobs)):
            if total == 0:
                raise StoreError("fb_auto_page_pool_empty", "所选Page池当前没有有效Page，未创建空运行", 409)
            if publishable == 0:
                raise StoreError("fb_auto_page_pool_unpublishable", "所选Page池当前没有可发布Page，未创建运行", 409)
            if ((max_publishable_pages is not None and publishable > int(max_publishable_pages))
                    or (max_jobs_per_slot is not None and global_slot_jobs > int(max_jobs_per_slot))
                    or (max_daily_jobs is not None and publishable * daily_count > int(max_daily_jobs))):
                raise StoreError("fb_auto_capacity_exceeded", f"运行前容量复核失败：可发布Page {publishable}/{max_publishable_pages or '-'}，全局最坏同槽GPU任务 {global_slot_jobs}/{max_jobs_per_slot or '-'}，本模板每日任务 {publishable * daily_count}/{max_daily_jobs or '-'}", 409)
        now_dt = self.now_fn()
        now = utc_iso(now_dt)
        link_timestamp = int(now_dt.timestamp())
        missing = total - publishable
        with self._lock, self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT id FROM fb_auto_run WHERE template_id=? AND slot_key=?", (template_id, slot_key)).fetchone()
            current = conn.execute("SELECT status,current_version FROM fb_auto_template WHERE id=?", (template_id,)).fetchone()
            if current is None or (version_guard is not None and int(current["current_version"]) != int(version_guard)):
                raise StoreError("fb_auto_due_slot_template_changed", "模板已停用或版本已变化，原计划时隙不再执行", 409)
            if current["status"] != "enabled" and (version_guard is not None or trigger_type == "auto"):
                code = "fb_auto_due_slot_template_disabled" if trigger_type == "auto" else "fb_auto_due_slot_template_changed"
                raise StoreError(code, "模板已停用，自动计划已暂停；重新启用后可在迟到宽限内继续" if trigger_type == "auto" else "模板已停用，手动执行计划已取消", 409)
            if expected_template_version is not None:
                self._validate_due_claim(
                    conn, template_id=template_id, slot_key=slot_key, trigger_type=trigger_type,
                    expected_template_version=int(expected_template_version), expected_due_id=expected_due_id,
                    expected_due_lease_owner=expected_due_lease_owner,
                    expected_due_lease_expires_at_utc=expected_due_lease_expires_at_utc,
                )
            if trigger_type == "auto" or version_guard is not None:
                current_enabled = {(int(row[0]), int(row[1])) for row in conn.execute("SELECT id,current_version FROM fb_auto_template WHERE status='enabled'")}
                if current_enabled != enabled_fingerprint:
                    raise StoreError("fb_auto_capacity_snapshot_changed", "启用模板集合在计划期间变化，请重试容量复核", 409)
            if existing:
                conn.commit()
                return {"ok": True, "run_id": int(existing[0]), "idempotent": True}
            active = conn.execute("SELECT DISTINCT r.id FROM fb_auto_run r JOIN fb_auto_task x ON x.run_id=r.id WHERE r.template_id=? AND x.status IN ('planned','preparing','ready','running') AND x.planned_publish_at_utc<=? LIMIT 1", (template_id, utc_iso(self.now_fn()))).fetchone()
            if active:
                error = StoreError("fb_auto_previous_run_backlog", "上一个到期时隙仍有Page任务未完成，本时隙不叠加", 409)
                error.conflicts = [{"run_id": int(active[0]), "status": "backlog"}]
                raise error
            publish_at = planned_publish_at_utc or now
            cur = conn.execute("INSERT INTO fb_auto_run(template_id,template_version,slot_key,trigger_type,status,config_json,total_pages,publishable_pages,missing_token_pages,created_at_utc,planned_publish_at_utc,metric_generation_ids_json,video_template) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (template_id, int(template["current_version"]), slot_key, trigger_type, "queued", template["config_json"], total, publishable, missing, now, publish_at, json.dumps(list(candidate_snapshot.metric_generation_ids)), str(config["video_template"])))
            run_id, queued, skipped = int(cur.lastrowid), 0, 0
            for page in page_rows:
                # The final cooldown read and material choice share this
                # BEGIN IMMEDIATE transaction with task insertion. Concurrent
                # planners therefore serialize, and a later slot sees the
                # earlier planned/preparing/ready reservation before choosing.
                unknown = conn.execute("SELECT 1 FROM fb_auto_task WHERE page_id=? AND status='unknown' LIMIT 1", (page.page_id,)).fetchone() is not None
                pre_reason = "fb_auto_page_unknown_block" if unknown else ""
                material = None
                if page.eligible_token_count > 0 and not unknown:
                    material = materials.choose_from(candidate_snapshot.candidates, self._cooldown_material_ids(conn, page.page_id, int(config["cooldown_days"])))
                reason = pre_reason or ("" if page.eligible_token_count > 0 else "fb_page_missing_eligible_token")
                snapshot_status = "eligible" if not reason else "skipped"
                conn.execute("INSERT INTO fb_auto_run_page(run_id,page_id,group_id,group_ids_json,owner_user_id,timezone,language,eligible_token_count,snapshot_status,skip_reason) VALUES(?,?,?,?,?,?,?,?,?,?)", (run_id, page.page_id, page.group_id, json.dumps(list(page.group_ids)), page.owner_user_id, page.timezone, page.language, page.eligible_token_count, snapshot_status, reason))
                status = "planned"
                if reason:
                    status = "skipped"
                elif material is None:
                    status, reason = "skipped", "fb_auto_no_eligible_video"
                gpu_job_id = "fb-page-" + __import__("hashlib").sha256(f"{template_id}:{template['current_version']}:{slot_key}:{page.page_id}".encode()).hexdigest()[:48]
                values = (run_id, template_id, int(template["current_version"]), page.page_id, page.group_id, status, reason, material.material_id if material else "", material.content_id if material else "", "", self._message(config, material) if material else "", now, now if status == "skipped" else "", publish_at, str(config["video_template"]), gpu_job_id, material.media_url if material else "")
                try:
                    task_cursor = conn.execute("INSERT INTO fb_auto_task(run_id,template_id,template_version,page_id,group_id,status,skip_reason,material_id,content_id,media_url,message_text,created_at_utc,completed_at_utc,planned_publish_at_utc,video_template,gpu_job_id,source_media_url) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values)
                except sqlite3.IntegrityError:
                    status, reason = "skipped", "fb_auto_page_task_conflict"
                    values = (run_id, template_id, int(template["current_version"]), page.page_id, page.group_id, status, reason, material.material_id if material else "", material.content_id if material else "", "", self._message(config, material) if material else "", now, now, publish_at, str(config["video_template"]), gpu_job_id, material.media_url if material else "")
                    task_cursor = conn.execute("INSERT INTO fb_auto_task(run_id,template_id,template_version,page_id,group_id,status,skip_reason,material_id,content_id,media_url,message_text,created_at_utc,completed_at_utc,planned_publish_at_utc,video_template,gpu_job_id,source_media_url) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values)
                if status == "planned" and material is not None and "{{url}}" in str(config["message_template"]):
                    task_id = int(task_cursor.lastrowid)
                    short_url, long_url = self._link_values(page, material, task_id, link_timestamp)
                    message_text = self._message(config, material, short_url)
                    conn.execute("UPDATE fb_auto_task SET message_text=?,short_url=?,long_url=? WHERE id=?", (message_text, short_url, long_url, task_id))
                if status == "planned": queued += 1
                else: skipped += 1
            run_status = "completed" if queued == 0 else "queued"
            conn.execute("UPDATE fb_auto_run SET status=?,queued_tasks=?,skipped_tasks=?,completed_at_utc=? WHERE id=?", (run_status, queued, skipped, now if queued == 0 else "", run_id))
            conn.commit()
        return {"ok": True, "run_id": run_id, "idempotent": False, "summary": {"total_pages": total, "publishable_pages": publishable, "missing_token_pages": missing, "overlap_pages": sum(len(page.group_ids) > 1 for page in page_rows), "queued_tasks": queued, "skipped_tasks": skipped}}

    def _late_cutoff(self, max_late_seconds: int | None = None) -> str:
        seconds = self.max_late_seconds if max_late_seconds is None else int(max_late_seconds)
        if not 0 <= seconds <= 86400:
            raise StoreError("fb_auto_max_late_invalid", "自动发布最大迟到宽限配置无效", 500)
        return utc_iso(self.now_fn() - timedelta(seconds=seconds))

    def _expire_late_auto_work(self, conn: sqlite3.Connection, now: str, cutoff: str) -> None:
        conn.execute(
            "UPDATE fb_auto_due_slot SET status='missed',error_code='fb_auto_due_slot_template_changed',lease_owner='',lease_expires_at_utc='',updated_at_utc=? WHERE status IN ('pending','preparing') AND (status<>'preparing' OR (lease_expires_at_utc<>'' AND lease_expires_at_utc<?)) AND NOT EXISTS (SELECT 1 FROM fb_auto_template t WHERE t.id=fb_auto_due_slot.template_id AND t.current_version=fb_auto_due_slot.template_version)",
            (now, now),
        )
        conn.execute(
            "UPDATE fb_auto_due_slot SET status='missed',error_code='fb_auto_due_slot_too_late',lease_owner='',lease_expires_at_utc='',updated_at_utc=? WHERE trigger_type='auto' AND status IN ('pending','preparing') AND (status<>'preparing' OR (lease_expires_at_utc<>'' AND lease_expires_at_utc<?)) AND planned_publish_at_utc<?",
            (now, now, cutoff),
        )
        stale_runs = [
            int(item[0])
            for item in conn.execute(
                "SELECT DISTINCT x.run_id FROM fb_auto_task x JOIN fb_auto_run r ON r.id=x.run_id JOIN fb_auto_template t ON t.id=x.template_id WHERE (x.status IN ('planned','ready') OR (x.status='preparing' AND x.lease_expires_at_utc<>'' AND x.lease_expires_at_utc<?)) AND (x.template_version<>t.current_version OR (r.trigger_type='auto' AND x.planned_publish_at_utc<?))",
                (now, cutoff),
            )
        ]
        conn.execute(
            "UPDATE fb_auto_task SET status='skipped',skip_reason='fb_auto_template_version_changed',error_code='fb_auto_template_version_changed',error_message='模板已更新，旧版本任务不再制作或发布',lease_owner='',lease_expires_at_utc='',next_prepare_at_utc='',completed_at_utc=? WHERE id IN (SELECT x.id FROM fb_auto_task x JOIN fb_auto_template t ON t.id=x.template_id WHERE (x.status IN ('planned','ready') OR (x.status='preparing' AND x.lease_expires_at_utc<>'' AND x.lease_expires_at_utc<?)) AND x.template_version<>t.current_version)",
            (now, now),
        )
        conn.execute(
            "UPDATE fb_auto_task SET status='skipped',skip_reason='fb_auto_task_too_late',error_code='fb_auto_task_too_late',error_message='自动发布已超过最大迟到宽限，不再制作或发布',lease_owner='',lease_expires_at_utc='',next_prepare_at_utc='',completed_at_utc=? WHERE id IN (SELECT x.id FROM fb_auto_task x JOIN fb_auto_run r ON r.id=x.run_id WHERE r.trigger_type='auto' AND (x.status IN ('planned','ready') OR (x.status='preparing' AND x.lease_expires_at_utc<>'' AND x.lease_expires_at_utc<?)) AND x.planned_publish_at_utc<?)",
            (now, now, cutoff),
        )
        for run_id in stale_runs:
            self._refresh_run(conn, run_id, now)

    def claim_prepare_next(self, worker_id: str, lease_seconds: int = 3600, *, max_late_seconds: int | None = None) -> Optional[Dict[str, Any]]:
        now_dt, now = self.now_fn(), utc_iso(self.now_fn())
        lease = utc_iso(now_dt + timedelta(seconds=lease_seconds))
        cutoff = self._late_cutoff(max_late_seconds)
        with self._lock, self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._expire_late_auto_work(conn, now, cutoff)
            row = conn.execute(
                "SELECT x.* FROM fb_auto_task x JOIN fb_auto_template t ON t.id=x.template_id AND t.status='enabled' AND t.current_version=x.template_version JOIN fb_auto_run r ON r.id=x.run_id WHERE (((x.status='planned' AND (x.next_prepare_at_utc='' OR x.next_prepare_at_utc<=?)) OR (x.status='preparing' AND x.lease_expires_at_utc<?))) AND (r.trigger_type='manual' OR x.planned_publish_at_utc>=?) ORDER BY x.planned_publish_at_utc,x.id LIMIT 1",
                (now, now, cutoff),
            ).fetchone()
            if row is None:
                conn.commit(); return None
            conn.execute("UPDATE fb_auto_task SET status='preparing',lease_owner=?,lease_expires_at_utc=?,attempt_count=attempt_count+1,started_at_utc=CASE WHEN started_at_utc='' THEN ? ELSE started_at_utc END WHERE id=?", (worker_id, lease, now, int(row["id"])))
            conn.commit(); return dict(conn.execute("SELECT * FROM fb_auto_task WHERE id=?", (int(row["id"]),)).fetchone())

    def complete_prepare(self, task_id: int, prepared: Mapping[str, Any], *, max_late_seconds: int | None = None) -> Dict[str, Any]:
        now = utc_iso(self.now_fn())
        cutoff = self._late_cutoff(max_late_seconds)
        with self._lock, self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT x.*,r.trigger_type FROM fb_auto_task x JOIN fb_auto_run r ON r.id=x.run_id WHERE x.id=?", (task_id,)).fetchone()
            if row is None:
                raise StoreError("fb_auto_prepare_not_claimed", "视频制作任务未被领取", 409)
            if row["status"] == "skipped" and row["trigger_type"] == "manual" and row["skip_reason"] == "fb_auto_manual_template_disabled":
                conn.execute("UPDATE fb_auto_task SET prepared_at_utc=CASE WHEN prepared_at_utc='' THEN ? ELSE prepared_at_utc END WHERE id=?", (now, task_id))
                conn.commit()
                return {"ok": True, "task_id": task_id, "status": "skipped", "skip_reason": "fb_auto_manual_template_disabled"}
            if row["status"] != "preparing":
                raise StoreError("fb_auto_prepare_not_claimed", "视频制作任务未被领取", 409)
            template = conn.execute("SELECT status,current_version FROM fb_auto_template WHERE id=?", (int(row["template_id"]),)).fetchone()
            version_changed = template is None or int(template["current_version"]) != int(row["template_version"])
            manual_disabled = row["trigger_type"] == "manual" and (template is None or template["status"] != "enabled")
            too_late = row["trigger_type"] == "auto" and str(row["planned_publish_at_utc"]) < cutoff
            if manual_disabled or version_changed or too_late:
                reason = "fb_auto_manual_template_disabled" if manual_disabled else ("fb_auto_template_version_changed" if version_changed else "fb_auto_task_too_late")
                message = "视频制作期间模板已停用，手动任务不再发布" if manual_disabled else ("视频制作期间模板已更新，旧版本成片不再发布" if version_changed else "视频制作完成时已超过最大迟到宽限，不再发布")
                conn.execute(
                    "UPDATE fb_auto_task SET status='skipped',skip_reason=?,error_code=?,error_message=?,prepared_at_utc=?,lease_owner='',lease_expires_at_utc='',next_prepare_at_utc='',completed_at_utc=? WHERE id=?",
                    (reason, reason, message, now, now, task_id),
                )
                self._refresh_run(conn, int(row["run_id"]), now)
                conn.commit()
                return {"ok": True, "task_id": task_id, "status": "skipped", "skip_reason": reason}
            url = str(prepared.get("media_url") or "")
            if not url or url == row["source_media_url"]:
                raise StoreError("fb_auto_prepared_response_invalid", "视频制作结果无效", 502)
            conn.execute("UPDATE fb_auto_task SET status='ready',media_url=?,prepared_media_url=?,prepared_sha256=?,prepared_size_bytes=?,prepared_duration_seconds=?,prepared_profile=?,prepared_at_utc=?,lease_owner='',lease_expires_at_utc='',next_prepare_at_utc='',error_code='',error_message='' WHERE id=?", (url, url, str(prepared.get("sha256") or ""), int(prepared.get("size_bytes") or 0), str(prepared.get("duration_seconds") or ""), str(prepared.get("profile") or ""), now, task_id))
            self._refresh_run(conn, int(row["run_id"]), now)
            conn.commit()
        return {"ok": True, "task_id": task_id, "status": "ready"}

    def fail_prepare(self, task_id: int, code: str, message: str) -> Dict[str, Any]:
        now = utc_iso(self.now_fn())
        with self._lock, self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT x.*,r.trigger_type FROM fb_auto_task x JOIN fb_auto_run r ON r.id=x.run_id WHERE x.id=?", (task_id,)).fetchone()
            if row is not None and row["status"] == "skipped" and row["trigger_type"] == "manual" and row["skip_reason"] == "fb_auto_manual_template_disabled":
                conn.commit()
                return {"ok": True, "task_id": task_id, "status": "skipped", "skip_reason": "fb_auto_manual_template_disabled"}
            if row is None or row["status"] != "preparing":
                raise StoreError("fb_auto_prepare_not_claimed", "视频制作任务未被领取", 409)
            conn.execute("UPDATE fb_auto_task SET status='failed',error_code=?,error_message=?,lease_owner='',lease_expires_at_utc='',next_prepare_at_utc='',completed_at_utc=? WHERE id=?", (str(code)[:96], str(message)[:500], now, task_id))
            self._refresh_run(conn, int(row["run_id"]), now); conn.commit()
        return {"ok": True, "task_id": task_id, "status": "failed"}

    def defer_prepare(self, task_id: int, code: str, message: str, *, delay_seconds: int = 300) -> Dict[str, Any]:
        next_at = utc_iso(self.now_fn() + timedelta(seconds=max(300, min(int(delay_seconds), 3600))))
        with self._lock, self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT x.*,r.trigger_type FROM fb_auto_task x JOIN fb_auto_run r ON r.id=x.run_id WHERE x.id=?", (task_id,)).fetchone()
            if row is not None and row["status"] == "skipped" and row["trigger_type"] == "manual" and row["skip_reason"] == "fb_auto_manual_template_disabled":
                conn.commit()
                return {"ok": True, "task_id": task_id, "status": "skipped", "skip_reason": "fb_auto_manual_template_disabled"}
            if row is None or row["status"] != "preparing": raise StoreError("fb_auto_prepare_not_claimed", "视频制作任务未被领取", 409)
            template = conn.execute("SELECT status,current_version FROM fb_auto_template WHERE id=?", (int(row["template_id"]),)).fetchone()
            manual_disabled = row["trigger_type"] == "manual" and (template is None or template["status"] != "enabled")
            if manual_disabled or template is None or int(template["current_version"]) != int(row["template_version"]):
                now = utc_iso(self.now_fn())
                reason = "fb_auto_manual_template_disabled" if manual_disabled else "fb_auto_template_version_changed"
                message = "视频制作重试前模板已停用，手动任务不再制作或发布" if manual_disabled else "视频制作重试前模板已更新，旧版本任务不再制作或发布"
                conn.execute(
                    "UPDATE fb_auto_task SET status='skipped',skip_reason=?,error_code=?,error_message=?,lease_owner='',lease_expires_at_utc='',next_prepare_at_utc='',completed_at_utc=? WHERE id=?",
                    (reason, reason, message, now, task_id),
                )
                self._refresh_run(conn, int(row["run_id"]), now)
                conn.commit()
                return {"ok": True, "task_id": task_id, "status": "skipped", "skip_reason": reason}
            conn.execute("UPDATE fb_auto_task SET status='planned',error_code=?,error_message=?,lease_owner='',lease_expires_at_utc='',next_prepare_at_utc=? WHERE id=?", (str(code)[:96], str(message)[:500], next_at, task_id))
            self._refresh_run(conn, int(row["run_id"]), utc_iso(self.now_fn())); conn.commit()
        return {"ok": True, "task_id": task_id, "status": "planned", "deferred": True, "next_prepare_at_utc": next_at}

    def claim_next(self, worker_id: str, lease_seconds: int = 1200, *, max_late_seconds: int | None = None) -> Optional[Dict[str, Any]]:
        now_dt, now = self.now_fn(), utc_iso(self.now_fn())
        lease = utc_iso(now_dt + timedelta(seconds=lease_seconds))
        cutoff = self._late_cutoff(max_late_seconds)
        with self._lock, self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._expire_late_auto_work(conn, now, cutoff)
            invalid_runs = [
                int(item[0])
                for item in conn.execute(
                    "SELECT DISTINCT run_id FROM fb_auto_task WHERE status='ready' AND (TRIM(prepared_at_utc)='' OR TRIM(prepared_media_url)='' OR media_url<>prepared_media_url OR prepared_media_url=source_media_url)"
                )
            ]
            conn.execute(
                "UPDATE fb_auto_task SET status='skipped',skip_reason='fb_auto_prepared_contract_invalid',error_code='fb_auto_prepared_contract_invalid',error_message='成片审计契约不完整，已安全终止且不会发布',lease_owner='',lease_expires_at_utc='',completed_at_utc=? WHERE status='ready' AND (TRIM(prepared_at_utc)='' OR TRIM(prepared_media_url)='' OR media_url<>prepared_media_url OR prepared_media_url=source_media_url)",
                (now,),
            )
            for run_id in invalid_runs:
                self._refresh_run(conn, run_id, now)
            row = conn.execute("SELECT x.* FROM fb_auto_task x JOIN fb_auto_template t ON t.id=x.template_id AND t.status='enabled' AND t.current_version=x.template_version WHERE x.status='ready' AND TRIM(x.prepared_at_utc)<>'' AND TRIM(x.prepared_media_url)<>'' AND x.media_url=x.prepared_media_url AND x.prepared_media_url<>x.source_media_url AND x.planned_publish_at_utc<=? AND NOT EXISTS (SELECT 1 FROM fb_auto_task other WHERE other.page_id=x.page_id AND other.id<>x.id AND other.status IN ('running','submitted','unknown')) ORDER BY x.planned_publish_at_utc,x.id LIMIT 1", (now,)).fetchone()
            if row is None:
                conn.commit(); return None
            updated = conn.execute("UPDATE fb_auto_task SET status='running',lease_owner=?,lease_expires_at_utc=?,attempt_count=attempt_count+1,started_at_utc=CASE WHEN started_at_utc='' THEN ? ELSE started_at_utc END WHERE id=? AND status='ready' AND TRIM(prepared_at_utc)<>'' AND TRIM(prepared_media_url)<>'' AND media_url=prepared_media_url AND prepared_media_url<>source_media_url AND planned_publish_at_utc<=? AND EXISTS (SELECT 1 FROM fb_auto_template t WHERE t.id=fb_auto_task.template_id AND t.status='enabled' AND t.current_version=fb_auto_task.template_version) AND NOT EXISTS (SELECT 1 FROM fb_auto_task x WHERE x.page_id=fb_auto_task.page_id AND x.id<>fb_auto_task.id AND x.status IN ('running','submitted','unknown'))", (worker_id, lease, now, int(row["id"]), now)).rowcount
            if updated != 1:
                conn.rollback(); return None
            conn.execute("UPDATE fb_auto_run SET status='running' WHERE id=?", (int(row["run_id"]),))
            conn.commit()
            claimed = conn.execute("SELECT * FROM fb_auto_task WHERE id=?", (int(row["id"]),)).fetchone()
            return dict(claimed)

    def mark_stale_running_unknown(self) -> int:
        now = utc_iso(self.now_fn())
        with self._lock, self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute("SELECT id,run_id,page_id,material_id FROM fb_auto_task WHERE status='running' AND lease_expires_at_utc<>'' AND lease_expires_at_utc<?", (now,)).fetchall()
            for row in rows:
                definite_attempts = int(conn.execute(
                    "SELECT COUNT(*) FROM fb_auto_publish_attempt WHERE task_id=? AND result_kind='definite_failure'",
                    (int(row["id"]),),
                ).fetchone()[0])
                conn.execute("UPDATE fb_auto_task SET status='unknown',unknown_outcome=1,error_code='fb_auto_worker_interrupted',error_message='发布执行中断，结果待人工确认',completed_at_utc=? WHERE id=?", (now, int(row["id"])))
                self._upsert_ledger(conn, int(row["id"]), row["page_id"], row["material_id"], "unknown", "", definite_attempts, True, "fb_auto_worker_interrupted", now)
                self._refresh_run(conn, int(row["run_id"]), now)
            conn.commit(); return len(rows)

    def complete_task(self, task_id: int, outcome: Mapping[str, Any]) -> Dict[str, Any]:
        status = str(outcome.get("status") or "")
        if status not in {"submitted", "failed", "unknown"}:
            raise StoreError("invalid_request", "任务结果无效", 400)
        post_id = str(outcome.get("graph_post_id") or "")
        if status == "submitted" and not post_id:
            raise StoreError("fb_auto_graph_id_missing", "Graph成功响应缺少发布ID", 502)
        now = utc_iso(self.now_fn())
        with self._lock, self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM fb_auto_task WHERE id=?", (task_id,)).fetchone()
            if row is None or row["status"] != "running":
                raise StoreError("fb_auto_task_not_claimed", "任务未处于可完成状态", 409)
            code = str(outcome.get("error_code") or "")[:96]
            message = str(outcome.get("error_message") or "")[:500]
            unknown = status == "unknown"
            next_reconcile = utc_iso(self.now_fn() + timedelta(minutes=5)) if status == "submitted" else ""
            conn.execute("UPDATE fb_auto_task SET status=?,graph_post_id=?,error_code=?,error_message=?,unknown_outcome=?,lease_owner='',lease_expires_at_utc='',next_reconcile_at_utc=?,completed_at_utc=? WHERE id=?", (status, post_id, code, message, int(unknown), next_reconcile, now, task_id))
            self._upsert_ledger(conn, task_id, row["page_id"], row["material_id"], status, post_id, int(outcome.get("definite_attempts") or 0), unknown, code, now)
            self._refresh_run(conn, int(row["run_id"]), now)
            conn.commit()
        return {"ok": True, "task_id": task_id, "status": status, "graph_object_id": post_id, "unknown_outcome": unknown}

    def complete_submitted_with_attempt(self, task_id: int, sequence: int, *, credential_id: str, fb_user_id: str, graph_post_id: str, trace_id: str = "", definite_attempts: int = 0) -> Dict[str, Any]:
        """Atomically persist Meta acceptance, Graph ID, task and ledger."""
        post_id = str(graph_post_id or "")
        if not post_id:
            raise StoreError("fb_auto_graph_id_missing", "Graph成功响应缺少发布ID", 502)
        now, next_reconcile = utc_iso(self.now_fn()), utc_iso(self.now_fn() + timedelta(minutes=5))
        with self._lock, self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM fb_auto_task WHERE id=? AND status='running'", (task_id,)).fetchone()
            if row is None:
                raise StoreError("fb_auto_task_not_claimed", "任务未处于可完成状态", 409)
            conn.execute("INSERT INTO fb_auto_publish_attempt(task_id,sequence,credential_id,fb_user_id,result_kind,error_code,trace_id,created_at_utc) VALUES(?,?,?,?,?,'',?,?)", (task_id, int(sequence), str(credential_id)[:64], str(fb_user_id)[:64], "accepted", str(trace_id)[:128], now))
            conn.execute("UPDATE fb_auto_task SET status='submitted',graph_post_id=?,error_code='',error_message='',unknown_outcome=0,lease_owner='',lease_expires_at_utc='',next_reconcile_at_utc=?,completed_at_utc=? WHERE id=?", (post_id, next_reconcile, now, task_id))
            self._upsert_ledger(conn, task_id, row["page_id"], row["material_id"], "submitted", post_id, int(definite_attempts), False, "", now)
            self._refresh_run(conn, int(row["run_id"]), now)
            conn.commit()
        return {"ok": True, "task_id": task_id, "status": "submitted", "graph_object_id": post_id, "unknown_outcome": False, "next_reconcile_at_utc": next_reconcile}

    def record_attempt(self, task_id: int, sequence: int, *, credential_id: str, fb_user_id: str, result_kind: str, error_code: str = "", trace_id: str = "") -> None:
        if result_kind not in {"accepted", "definite_failure", "unknown"}:
            raise StoreError("invalid_request", "尝试结果无效", 400)
        with self.connect() as conn:
            conn.execute("INSERT INTO fb_auto_publish_attempt(task_id,sequence,credential_id,fb_user_id,result_kind,error_code,trace_id,created_at_utc) VALUES(?,?,?,?,?,?,?,?)", (task_id, sequence, str(credential_id)[:64], str(fb_user_id)[:64], result_kind, str(error_code)[:96], str(trace_id)[:128], utc_iso(self.now_fn())))

    def claim_submitted(self, worker_id: str, lease_seconds: int = 1200) -> Optional[Dict[str, Any]]:
        now_dt, now = self.now_fn(), utc_iso(self.now_fn())
        lease = utc_iso(now_dt + timedelta(seconds=lease_seconds))
        with self._lock, self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM fb_auto_task WHERE status='submitted' AND (next_reconcile_at_utc='' OR next_reconcile_at_utc<=?) AND (lease_expires_at_utc='' OR lease_expires_at_utc<?) ORDER BY id LIMIT 1", (now, now)).fetchone()
            if row is None:
                conn.commit(); return None
            if conn.execute("UPDATE fb_auto_task SET lease_owner=?,lease_expires_at_utc=? WHERE id=? AND status='submitted' AND (next_reconcile_at_utc='' OR next_reconcile_at_utc<=?) AND (lease_expires_at_utc='' OR lease_expires_at_utc<?)", (worker_id, lease, int(row["id"]), now, now)).rowcount != 1:
                conn.rollback(); return None
            conn.commit(); return dict(conn.execute("SELECT * FROM fb_auto_task WHERE id=?", (int(row["id"]),)).fetchone())

    def reconcile_task(self, task_id: int, status: str, *, error_code: str = "", error_message: str = "") -> Dict[str, Any]:
        if status not in {"published", "failed_without_retry", "submitted", "unknown"}:
            raise StoreError("invalid_request", "对账状态无效", 400)
        now = utc_iso(self.now_fn())
        with self._lock, self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM fb_auto_task WHERE id=? AND status='submitted'", (task_id,)).fetchone()
            if row is None: raise StoreError("fb_auto_task_not_submitted", "任务不在待对账状态", 409)
            completed = "" if status == "submitted" else now
            unknown = status == "unknown"
            next_reconcile = utc_iso(self.now_fn() + timedelta(minutes=5)) if status == "submitted" else ""
            conn.execute("UPDATE fb_auto_task SET status=?,error_code=?,error_message=?,unknown_outcome=?,lease_owner='',lease_expires_at_utc='',next_reconcile_at_utc=?,completed_at_utc=? WHERE id=?", (status, str(error_code)[:96], str(error_message)[:500], int(unknown), next_reconcile, completed, task_id))
            self._upsert_ledger(conn, task_id, row["page_id"], row["material_id"], status, row["graph_post_id"], 0, unknown, str(error_code)[:96], now)
            self._refresh_run(conn, int(row["run_id"]), now); conn.commit()
        return {"ok": True, "task_id": task_id, "status": status, "graph_object_id": row["graph_post_id"], "unknown_outcome": unknown, "next_reconcile_at_utc": next_reconcile}

    @staticmethod
    def _upsert_ledger(conn: sqlite3.Connection, task_id: int, page_id: str, material_id: str, status: str, post_id: str, attempts: int, unknown: bool, code: str, now: str) -> None:
        conn.execute("INSERT INTO fb_auto_publish_ledger(task_id,page_id,material_id,status,graph_post_id,definite_attempts,unknown_outcome,error_code,created_at_utc,updated_at_utc) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET status=excluded.status,graph_post_id=excluded.graph_post_id,definite_attempts=MAX(fb_auto_publish_ledger.definite_attempts,excluded.definite_attempts),unknown_outcome=excluded.unknown_outcome,error_code=excluded.error_code,updated_at_utc=excluded.updated_at_utc", (task_id, page_id, material_id, status, post_id, attempts, int(unknown), code, now, now))

    @staticmethod
    def _refresh_run(conn: sqlite3.Connection, run_id: int, now: str) -> None:
        counts = {row[0]: int(row[1]) for row in conn.execute("SELECT status,COUNT(*) FROM fb_auto_task WHERE run_id=? GROUP BY status", (run_id,))}
        if any(counts.get(name) for name in ("planned", "preparing", "ready", "queued", "running")):
            status, completed = "running", ""
        elif counts.get("unknown") or counts.get("submitted"):
            status, completed = "attention_required", now
        elif (counts.get("failed") or counts.get("failed_without_retry")) and (counts.get("published") or counts.get("skipped")):
            status, completed = "partial_failed", now
        elif counts.get("failed") or counts.get("failed_without_retry"):
            status, completed = "failed", now
        else:
            status, completed = "completed", now
        conn.execute("UPDATE fb_auto_run SET status=?,completed_at_utc=? WHERE id=?", (status, completed, run_id))

    def list_runs(self, actor: ActorScope, *, limit: int = 50, offset: int = 0) -> Dict[str, Any]:
        clause, params = self._scope_clause(actor, "t")
        with self.connect() as conn:
            total = int(conn.execute(f"SELECT COUNT(*) FROM fb_auto_run r JOIN fb_auto_template t ON t.id=r.template_id WHERE 1=1{clause}", params).fetchone()[0])
            rows = conn.execute(f"SELECT r.*,t.name FROM fb_auto_run r JOIN fb_auto_template t ON t.id=r.template_id WHERE 1=1{clause} ORDER BY r.id DESC LIMIT ? OFFSET ?", params + (limit, offset)).fetchall()
        items = [{key: row[key] for key in row.keys() if key != "config_json"} for row in rows]
        return {"ok": True, "items": items, "total": total, "limit": limit, "offset": offset}

    def get_run(self, run_id: int, actor: ActorScope) -> Dict[str, Any]:
        clause, params = self._scope_clause(actor, "t")
        with self.connect() as conn:
            run = conn.execute(f"SELECT r.*,t.name FROM fb_auto_run r JOIN fb_auto_template t ON t.id=r.template_id WHERE r.id=?{clause}", (run_id,) + params).fetchone()
            if run is None:
                raise StoreError("fb_auto_run_not_found", "运行记录不存在", 404)
            tasks = conn.execute("SELECT id,page_id,group_id,status,skip_reason,material_id,content_id,short_url,long_url,video_template,gpu_job_id,planned_publish_at_utc,prepared_profile,prepared_sha256,prepared_size_bytes,prepared_duration_seconds,prepared_at_utc,graph_post_id,error_code,error_message,unknown_outcome,created_at_utc,completed_at_utc FROM fb_auto_task WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
            pages = conn.execute("SELECT page_id,group_id,group_ids_json,timezone,language,eligible_token_count,snapshot_status,skip_reason FROM fb_auto_run_page WHERE run_id=? ORDER BY page_id", (run_id,)).fetchall()
            attempts = conn.execute("SELECT a.task_id,a.sequence,a.credential_id,a.fb_user_id,a.result_kind,a.error_code,a.trace_id,a.created_at_utc FROM fb_auto_publish_attempt a JOIN fb_auto_task t ON t.id=a.task_id WHERE t.run_id=? ORDER BY a.task_id,a.sequence", (run_id,)).fetchall()
        result = {key: run[key] for key in run.keys() if key != "config_json"}
        result["config"] = _loads(run["config_json"], {})
        result["metric_generation_ids"] = _loads(result.pop("metric_generation_ids_json", "[]"), [])
        result["tasks"] = []
        for row in tasks:
            item = dict(row)
            item["graph_object_id"] = item.pop("graph_post_id")
            result["tasks"].append(item)
        result["page_snapshots"] = []
        for row in pages:
            item = dict(row)
            item["group_ids"] = _loads(item.pop("group_ids_json"), [])
            result["page_snapshots"].append(item)
        result["attempts"] = [dict(row) for row in attempts]
        return {"ok": True, "run": result}

    @staticmethod
    def _activate_metric_generation(conn: sqlite3.Connection, *, platform: int, day: str, product: str, generation_id: int, refreshed_at_utc: str, activated_at_utc: str) -> bool:
        current = conn.execute("SELECT g.refreshed_at_utc FROM fb_auto_metric_active_pointer p JOIN fb_auto_metric_generation g ON g.id=p.generation_id WHERE p.platform=? AND p.metric_date=? AND p.product=?", (platform, day, product)).fetchone()
        try:
            candidate_time = datetime.fromisoformat(refreshed_at_utc).astimezone(UTC)
            current_time = datetime.fromisoformat(str(current[0])).astimezone(UTC) if current else None
        except (TypeError, ValueError):
            raise StoreError("fb_auto_metric_refresh_time_invalid", "指标刷新时间无效", 400) from None
        if current_time is not None and candidate_time < current_time:
            return False
        conn.execute("INSERT INTO fb_auto_metric_active_pointer(platform,metric_date,product,generation_id,activated_at_utc) VALUES(?,?,?,?,?) ON CONFLICT(platform,metric_date,product) DO UPDATE SET generation_id=excluded.generation_id,activated_at_utc=excluded.activated_at_utc", (platform, day, product, generation_id, activated_at_utc))
        return True

    def record_metric_generation(self, *, platform: int, metric_date: str, product: str, rows: Iterable[Mapping[str, Any]], refreshed_at_utc: str) -> Dict[str, Any]:
        """Publish an immutable complete day and atomically swing its active pointer."""
        from .metrics import checksum_rows, metric_date as clean_date, nonnegative_decimal
        day = clean_date(metric_date)
        if product != "Dramawave" or int(platform) != 0:
            raise StoreError("fb_auto_metric_product_unsupported", "指标产品映射未开放", 409)
        canonical = []
        seen = set()
        for raw in rows:
            content_id, material_id = str(raw.get("content_id") or "").strip(), str(raw.get("material_id") or "").strip()
            key = (content_id, material_id)
            if not content_id or not re.fullmatch(r"[1-9][0-9]*", material_id) or key in seen:
                raise StoreError("fb_auto_metric_row_invalid", "指标缓存行身份无效", 400)
            seen.add(key)
            canonical.append({"content_id": content_id, "material_id": material_id, "spend": format(nonnegative_decimal(raw.get("spend")), "f"), "af_revenue0": format(nonnegative_decimal(raw.get("af_revenue0")), "f")})
        canonical.sort(key=lambda item: (item["content_id"], int(item["material_id"])))
        checksum = checksum_rows(canonical)
        key_raw = json.dumps([int(platform), day, product, refreshed_at_utc], separators=(",", ":"))
        generation_key = "fb-auto-metric-v1-" + __import__("hashlib").sha256(key_raw.encode()).hexdigest()
        now = utc_iso(self.now_fn())
        with self._lock, self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT * FROM fb_auto_metric_generation WHERE generation_key=?", (generation_key,)).fetchone()
            if existing:
                if existing["status"] != "ready" or existing["checksum"] != checksum:
                    raise StoreError("fb_auto_metric_generation_conflict", "指标代次冲突", 409)
                self._activate_metric_generation(conn, platform=platform, day=day, product=product, generation_id=int(existing["id"]), refreshed_at_utc=str(existing["refreshed_at_utc"]), activated_at_utc=now)
                conn.commit()
                return {"id": int(existing["id"]), "status": "ready", "row_count": int(existing["row_count"]), "checksum": checksum, "idempotent": True}
            cursor = conn.execute("INSERT INTO fb_auto_metric_generation(generation_key,platform,metric_date,product,status,row_count,checksum,refreshed_at_utc,created_at_utc) VALUES(?,?,?,?,'building',0,'',?,?)", (generation_key, platform, day, product, refreshed_at_utc, now))
            generation_id = int(cursor.lastrowid)
            conn.executemany("INSERT INTO fb_auto_metric_daily(generation_id,content_id,material_id,spend,af_revenue0) VALUES(?,?,?,?,?)", [(generation_id, item["content_id"], item["material_id"], item["spend"], item["af_revenue0"]) for item in canonical])
            stored = conn.execute("SELECT content_id,material_id,spend,af_revenue0 FROM fb_auto_metric_daily WHERE generation_id=? ORDER BY content_id,LENGTH(material_id),material_id", (generation_id,)).fetchall()
            stored_checksum = checksum_rows(stored)
            if stored_checksum != checksum or len(stored) != len(canonical):
                raise StoreError("fb_auto_metric_checksum_mismatch", "指标缓存完整性校验失败", 500)
            conn.execute("UPDATE fb_auto_metric_generation SET status='ready',row_count=?,checksum=?,ready_at_utc=? WHERE id=?", (len(canonical), checksum, now, generation_id))
            self._activate_metric_generation(conn, platform=platform, day=day, product=product, generation_id=generation_id, refreshed_at_utc=refreshed_at_utc, activated_at_utc=now)
            conn.commit()
        return {"id": generation_id, "status": "ready", "row_count": len(canonical), "checksum": checksum, "idempotent": False}

    def record_metric_generation_streaming(self, *, platform: int, metric_date: str, product: str, rows: Iterable[Mapping[str, Any]], refreshed_at_utc: str) -> Dict[str, Any]:
        """Bounded-memory variant for the ordered server-side daily cursor."""
        from .metrics import metric_date as clean_date, nonnegative_decimal
        import hashlib
        day = clean_date(metric_date)
        if product != "Dramawave" or int(platform) != 0:
            raise StoreError("fb_auto_metric_product_unsupported", "指标产品映射未开放", 409)
        identity = json.dumps([platform, day, product, refreshed_at_utc], separators=(",", ":"))
        generation_key = "fb-auto-metric-v1-" + hashlib.sha256(identity.encode()).hexdigest()
        now, digest, count, previous = utc_iso(self.now_fn()), hashlib.sha256(), 0, None
        with self._lock, self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT * FROM fb_auto_metric_generation WHERE generation_key=?", (generation_key,)).fetchone()
            if existing and existing["status"] == "ready":
                self._activate_metric_generation(conn, platform=platform, day=day, product=product, generation_id=int(existing["id"]), refreshed_at_utc=str(existing["refreshed_at_utc"]), activated_at_utc=now); conn.commit()
                return {"id": int(existing["id"]), "status": "ready", "row_count": int(existing["row_count"]), "checksum": existing["checksum"], "idempotent": True}
            if existing:
                raise StoreError("fb_auto_metric_generation_incomplete", "同一指标代次尚未READY，拒绝覆盖并需repair使用新刷新时间", 409)
            cursor = conn.execute("INSERT INTO fb_auto_metric_generation(generation_key,platform,metric_date,product,status,row_count,checksum,refreshed_at_utc,created_at_utc) VALUES(?,?,?,?,'building',0,'',?,?)", (generation_key, platform, day, product, refreshed_at_utc, now))
            generation_id = int(cursor.lastrowid)
            for raw in rows:
                content_id, material_id = str(raw.get("content_id") or "").strip(), str(raw.get("material_id") or "").strip()
                valid_material_id = bool(re.fullmatch(r"[1-9][0-9]*", material_id))
                key = (content_id.encode("utf-8"), len(material_id), material_id)
                if not content_id or not valid_material_id or (previous is not None and key <= previous):
                    raise StoreError("fb_auto_metric_row_invalid", "单日指标行无序、重复或身份无效", 400)
                previous = key
                spend, revenue = format(nonnegative_decimal(raw.get("spend")), "f"), format(nonnegative_decimal(raw.get("af_revenue0")), "f")
                conn.execute("INSERT INTO fb_auto_metric_daily(generation_id,content_id,material_id,spend,af_revenue0) VALUES(?,?,?,?,?)", (generation_id, content_id, material_id, spend, revenue))
                digest.update(json.dumps([content_id, material_id, spend, revenue], ensure_ascii=False, separators=(",", ":")).encode()); digest.update(b"\n"); count += 1
            checksum = digest.hexdigest()
            stored_count = int(conn.execute("SELECT COUNT(*) FROM fb_auto_metric_daily WHERE generation_id=?", (generation_id,)).fetchone()[0])
            if stored_count != count: raise StoreError("fb_auto_metric_checksum_mismatch", "指标缓存完整性校验失败", 500)
            conn.execute("UPDATE fb_auto_metric_generation SET status='ready',row_count=?,checksum=?,ready_at_utc=? WHERE id=?", (count, checksum, now, generation_id))
            self._activate_metric_generation(conn, platform=platform, day=day, product=product, generation_id=generation_id, refreshed_at_utc=refreshed_at_utc, activated_at_utc=now); conn.commit()
        return {"id": generation_id, "status": "ready", "row_count": count, "checksum": checksum, "idempotent": False}

    def load_metric_window(self, *, product: str, platform: int, dates: Sequence[str]):
        from .metrics import MetricTotals, MetricWindow, metric_date as clean_date
        normalized = tuple(dict.fromkeys(clean_date(value) for value in dates))
        if not normalized or product != "Dramawave" or int(platform) != 0:
            raise StoreError("fb_auto_metric_product_unsupported", "指标窗口产品映射未开放", 409)
        placeholders = ",".join("?" for _ in normalized)
        with self.connect() as conn:
            conn.execute("BEGIN")
            pointers = conn.execute(f"SELECT p.metric_date,p.generation_id,g.status FROM fb_auto_metric_active_pointer p JOIN fb_auto_metric_generation g ON g.id=p.generation_id WHERE p.platform=? AND p.product=? AND p.metric_date IN ({placeholders})", (platform, product, *normalized)).fetchall()
            ready = {str(row["metric_date"]): int(row["generation_id"]) for row in pointers if row["status"] == "ready"}
            missing = [day for day in normalized if day not in ready]
            if missing:
                conn.rollback()
                error = StoreError("fb_auto_metric_window_not_ready", "指标窗口缺少完整READY自然日", 409)
                error.conflicts = [{"missing_dates": missing}]
                raise error
            generation_ids = tuple(ready[day] for day in normalized)
            id_placeholders = ",".join("?" for _ in generation_ids)
            rows = conn.execute(f"SELECT content_id,material_id,spend,af_revenue0 FROM fb_auto_metric_daily WHERE generation_id IN ({id_placeholders})", generation_ids).fetchall()
            by_material: Dict[tuple[str, str], Any] = {}
            by_drama: Dict[str, Any] = {}
            from decimal import Decimal
            for row in rows:
                content_id, material_id = str(row["content_id"]), str(row["material_id"])
                spend, revenue = Decimal(str(row["spend"])), Decimal(str(row["af_revenue0"]))
                old = by_material.get((content_id, material_id), MetricTotals())
                by_material[(content_id, material_id)] = MetricTotals(old.spend + spend, old.revenue + revenue)
                old_drama = by_drama.get(content_id, MetricTotals())
                by_drama[content_id] = MetricTotals(old_drama.spend + spend, old_drama.revenue + revenue)
            conn.commit()
        return MetricWindow(generation_ids, normalized, by_drama, by_material)

    def enqueue_due_slots(self, *, live_enabled: bool, at: Optional[datetime] = None, prepare_ahead_seconds: int = 14400, prebuild_days_ahead: int = 1, max_catchup_minutes: int = 180) -> Dict[str, Any]:
        """Fast durable scheduler: SQLite only, no MySQL/GPU/Graph work."""
        if not live_enabled:
            return {"ok": True, "status": "live_gate_closed", "enqueued": 0, "missed": 0, "skipped_today_templates": 0}
        current = (at or self.now_fn()).astimezone(UTC).replace(second=0, microsecond=0)
        try:
            days_ahead = int(prebuild_days_ahead)
        except (TypeError, ValueError):
            raise StoreError("fb_auto_prebuild_days_invalid", "按自然日提前制作配置无效", 500) from None
        if not 0 <= days_ahead <= 7:
            raise StoreError("fb_auto_prebuild_days_invalid", "按自然日提前制作配置无效", 500)
        if days_ahead:
            return self._enqueue_calendar_due_slots(current, days_ahead)
        return self._enqueue_rolling_due_slots(current, prepare_ahead_seconds, max_catchup_minutes)

    def _enqueue_calendar_due_slots(self, current: datetime, days_ahead: int) -> Dict[str, Any]:
        """Enumerate Beijing calendar slots directly; never scan the horizon minute by minute."""
        local_now = current.astimezone(BEIJING)
        local_dates = [(local_now.date() + timedelta(days=offset)).isoformat() for offset in range(days_ahead + 1)]
        today_start_utc = local_now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT t.id,t.current_version,t.updated_at_utc,v.config_json FROM fb_auto_template t JOIN fb_auto_template_version v ON v.template_id=t.id AND v.version=t.current_version WHERE t.status='enabled' ORDER BY t.id"
            ).fetchall()
        candidates: List[tuple[int, int, str, str, str, bool]] = []
        skipped_today_template_ids: set[int] = set()
        for row in rows:
            template_id, version = int(row["id"]), int(row["current_version"])
            config = _loads(row["config_json"], {})
            activation_stamp = str(row["updated_at_utc"] or "")
            try:
                activation_at = datetime.fromisoformat(activation_stamp)
                skip_today = activation_at.tzinfo is None or activation_at.astimezone(UTC) >= today_start_utc
            except (TypeError, ValueError):
                skip_today = True
            if skip_today:
                skipped_today_template_ids.add(template_id)
            for local_date in local_dates:
                is_today = local_date == local_dates[0]
                if is_today and skip_today:
                    continue
                for minute in self.schedule_times(template_id, version, config, local_date):
                    planned_local = datetime.fromisoformat(f"{local_date}T{minute}:00").replace(tzinfo=BEIJING)
                    planned_utc = planned_local.astimezone(UTC)
                    if planned_utc < current:
                        continue
                    candidates.append((template_id, version, f"auto:v{version}:{local_date}:{minute}", utc_iso(planned_utc), activation_stamp, is_today))
        now, enqueued = utc_iso(self.now_fn()), 0
        with self._lock, self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            enabled = {
                (int(item[0]), int(item[1])): str(item[2] or "")
                for item in conn.execute("SELECT id,current_version,updated_at_utc FROM fb_auto_template WHERE status='enabled'")
            }
            for template_id, version, slot_key, planned_at, activation_stamp, is_today in candidates:
                if (template_id, version) not in enabled:
                    continue
                if is_today and enabled[(template_id, version)] != activation_stamp:
                    skipped_today_template_ids.add(template_id)
                    continue
                conn.execute(
                    "INSERT OR IGNORE INTO fb_auto_due_slot(template_id,template_version,slot_key,planned_publish_at_utc,status,created_at_utc,updated_at_utc) VALUES(?,?,?,?, 'pending',?,?)",
                    (template_id, version, slot_key, planned_at, now, now),
                )
                enqueued += int(conn.execute("SELECT changes()").fetchone()[0] > 0)
            conn.commit()
        planned_through = (local_now + timedelta(days=days_ahead)).replace(hour=23, minute=59, second=0, microsecond=0).astimezone(UTC)
        return {
            "ok": True,
            "status": "scheduled",
            "schedule_mode": "beijing_calendar",
            "prebuild_days_ahead": days_ahead,
            "enqueued": enqueued,
            "missed": 0,
            "skipped_today_templates": len(skipped_today_template_ids),
            "planned_through_utc": utc_iso(planned_through),
            "planned_through_local_date": local_dates[-1],
        }

    def _enqueue_rolling_due_slots(self, current: datetime, prepare_ahead_seconds: int, max_catchup_minutes: int) -> Dict[str, Any]:
        if not 3600 <= int(prepare_ahead_seconds) <= 86400:
            raise StoreError("fb_auto_prepare_ahead_invalid", "提前制作窗口配置无效", 500)
        target = (current + timedelta(seconds=int(prepare_ahead_seconds))).replace(second=0, microsecond=0)
        state_key, now = "scheduler-v1", utc_iso(self.now_fn())
        with self._lock, self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            state = conn.execute("SELECT watermark_minute_utc FROM fb_auto_scheduler_state WHERE state_key=?", (state_key,)).fetchone()
            watermark = datetime.fromisoformat(state[0]).astimezone(UTC) if state else current - timedelta(minutes=1)
            first = watermark + timedelta(minutes=1)
            missed = 0
            if first < current - timedelta(minutes=max_catchup_minutes - 1):
                old_first = first
                first = current - timedelta(minutes=max_catchup_minutes - 1)
                rows = conn.execute("SELECT t.id,t.current_version FROM fb_auto_template t WHERE t.status='enabled'").fetchall()
                for row in rows:
                    key = f"missed:v{int(row['current_version'])}:{old_first.isoformat()}:{first.isoformat()}"
                    conn.execute("INSERT OR IGNORE INTO fb_auto_due_slot(template_id,template_version,slot_key,planned_publish_at_utc,status,error_code,created_at_utc,updated_at_utc) VALUES(?,?,?,?, 'missed','fb_auto_due_slot_too_old',?,?)", (int(row["id"]), int(row["current_version"]), key, utc_iso(first), now, now))
                    missed += int(conn.execute("SELECT changes()").fetchone()[0] > 0)
            conn.commit()
        enqueued = 0
        # Rescan the complete future prepare window every tick. Versioned slot keys
        # keep this cheap/idempotent and let a newly enabled version replace stale
        # future intentions even when the watermark already points beyond them.
        cursor = min(first, current)
        while cursor <= target:
            for due in self.due_templates(cursor):
                with self.connect() as conn:
                    conn.execute("INSERT OR IGNORE INTO fb_auto_due_slot(template_id,template_version,slot_key,planned_publish_at_utc,status,created_at_utc,updated_at_utc) VALUES(?,?,?,?, 'pending',?,?)", (due["template_id"], due["template_version"], due["slot_key"], utc_iso(cursor), now, now))
                    enqueued += int(conn.execute("SELECT changes()").fetchone()[0] > 0)
            cursor += timedelta(minutes=1)
        with self.connect() as conn:
            conn.execute("INSERT INTO fb_auto_scheduler_state(state_key,watermark_minute_utc,updated_at_utc) VALUES(?,?,?) ON CONFLICT(state_key) DO UPDATE SET watermark_minute_utc=excluded.watermark_minute_utc,updated_at_utc=excluded.updated_at_utc", (state_key, utc_iso(target), now))
        return {"ok": True, "status": "scheduled", "schedule_mode": "rolling", "prebuild_days_ahead": 0, "enqueued": enqueued, "missed": missed, "skipped_today_templates": 0, "watermark_minute_utc": utc_iso(target), "planned_through_utc": utc_iso(target)}

    def enqueue_manual_due_slot(self, template_id: int, actor: ActorScope, *, expected_template_version: int, operation_id: str) -> Dict[str, Any]:
        operation = str(operation_id or "").strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9._:-]{15,99}", operation):
            raise StoreError("invalid_request", "手动执行operation_id无效", 400)
        now = utc_iso(self.now_fn())
        with self._lock, self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            template = self._template_row(conn, template_id, actor)
            if int(template["current_version"]) != int(expected_template_version):
                raise StoreError("fb_auto_template_version_conflict", "模板版本冲突", 409)
            if template["status"] != "enabled":
                raise StoreError("fb_auto_manual_template_disabled", "模板已停用，请先重新启用后再手动执行", 409)
            slot_key = f"manual:v{int(template['current_version'])}:{operation}"
            conn.execute("INSERT OR IGNORE INTO fb_auto_due_slot(template_id,template_version,slot_key,planned_publish_at_utc,status,trigger_type,created_at_utc,updated_at_utc) VALUES(?,?,?,?, 'pending','manual',?,?)", (template_id, int(template["current_version"]), slot_key, now, now, now))
            inserted = bool(conn.execute("SELECT changes()").fetchone()[0])
            row = conn.execute("SELECT id,status,run_id FROM fb_auto_due_slot WHERE template_id=? AND slot_key=?", (template_id, slot_key)).fetchone()
            conn.commit()
        return {"ok": True, "due_slot_id": int(row["id"]), "status": row["status"], "run_id": int(row["run_id"]) if row["run_id"] else None, "operation_id": operation, "idempotent": not inserted}

    def claim_due_slot(self, worker_id: str, lease_seconds: int = 900, *, max_late_seconds: int | None = None) -> Optional[Dict[str, Any]]:
        now_dt, now = self.now_fn(), utc_iso(self.now_fn())
        lease = utc_iso(now_dt + timedelta(seconds=lease_seconds))
        cutoff = self._late_cutoff(max_late_seconds)
        with self._lock, self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._expire_late_auto_work(conn, now, cutoff)
            row = conn.execute("SELECT d.*,t.owner_user_id,t.scope_is_admin FROM fb_auto_due_slot d JOIN fb_auto_template t ON t.id=d.template_id AND t.status='enabled' AND t.current_version=d.template_version WHERE (((d.status='pending' AND (d.available_at_utc='' OR d.available_at_utc<=?)) OR (d.status='preparing' AND d.lease_expires_at_utc<?))) AND (d.trigger_type='manual' OR d.planned_publish_at_utc>=?) ORDER BY d.planned_publish_at_utc,d.id LIMIT 1", (now, now, cutoff)).fetchone()
            if row is None:
                conn.commit(); return None
            updated = conn.execute("UPDATE fb_auto_due_slot SET status='preparing',lease_owner=?,lease_expires_at_utc=?,updated_at_utc=? WHERE id=? AND EXISTS (SELECT 1 FROM fb_auto_template t WHERE t.id=fb_auto_due_slot.template_id AND t.status='enabled' AND t.current_version=fb_auto_due_slot.template_version)", (worker_id, lease, now, int(row["id"]))).rowcount
            if updated != 1:
                conn.rollback(); return None
            claimed = conn.execute("SELECT d.*,t.owner_user_id,t.scope_is_admin FROM fb_auto_due_slot d JOIN fb_auto_template t ON t.id=d.template_id WHERE d.id=?", (int(row["id"]),)).fetchone()
            conn.commit(); return dict(claimed)

    def complete_due_slot(self, due_id: int, *, run_id: int | None = None, error_code: str = "", expected_lease_owner: str | None = None, expected_lease_expires_at_utc: str | None = None) -> bool:
        now = utc_iso(self.now_fn())
        with self._lock, self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            due = conn.execute("SELECT template_id,template_version,status,lease_owner,lease_expires_at_utc FROM fb_auto_due_slot WHERE id=?", (due_id,)).fetchone()
            if (due is None or due["status"] != "preparing"
                    or (expected_lease_owner is not None and str(due["lease_owner"] or "") != str(expected_lease_owner))
                    or (expected_lease_expires_at_utc is not None and str(due["lease_expires_at_utc"] or "") != str(expected_lease_expires_at_utc))):
                conn.commit()
                return False
            template = conn.execute("SELECT current_version FROM fb_auto_template WHERE id=?", (int(due["template_id"]),)).fetchone()
            version_changed = template is None or int(template["current_version"]) != int(due["template_version"])
            status = "missed" if version_changed else ("prepared" if run_id else "failed")
            final_error = "fb_auto_due_slot_template_changed" if version_changed else str(error_code)[:96]
            updated = conn.execute("UPDATE fb_auto_due_slot SET status=?,run_id=?,error_code=?,lease_owner='',lease_expires_at_utc='',updated_at_utc=? WHERE id=? AND status='preparing' AND lease_owner=? AND lease_expires_at_utc=?", (status, run_id if not version_changed else None, final_error, now, due_id, str(due["lease_owner"] or ""), str(due["lease_expires_at_utc"] or ""))).rowcount
            conn.commit()
            return updated == 1

    def defer_due_slot(self, due_id: int, error_code: str, *, delay_seconds: int = 300, expected_lease_owner: str | None = None, expected_lease_expires_at_utc: str | None = None) -> bool:
        available = utc_iso(self.now_fn() + timedelta(seconds=max(60, min(int(delay_seconds), 3600))))
        with self._lock, self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            sql = "UPDATE fb_auto_due_slot SET status='pending',error_code=?,available_at_utc=?,lease_owner='',lease_expires_at_utc='',updated_at_utc=? WHERE id=? AND status='preparing'"
            params: List[Any] = [str(error_code)[:96], available, utc_iso(self.now_fn()), due_id]
            if expected_lease_owner is not None:
                sql += " AND lease_owner=?"
                params.append(str(expected_lease_owner))
            if expected_lease_expires_at_utc is not None:
                sql += " AND lease_expires_at_utc=?"
                params.append(str(expected_lease_expires_at_utc))
            updated = conn.execute(sql, params).rowcount
            conn.commit()
            return updated == 1

    def schedule_times(self, template_id: int, version: int, config: Mapping[str, Any], local_date: str) -> List[str]:
        schedule = config["schedule"]
        if schedule["mode"] == "fixed":
            return list(schedule["times"])
        with self._lock, self.connect() as conn:
            row = conn.execute("SELECT times_json FROM fb_auto_schedule_plan WHERE template_id=? AND template_version=? AND local_date=?", (template_id, version, local_date)).fetchone()
            if row:
                return list(_loads(row[0], []))
            start_h, start_m = map(int, schedule["start"].split(":")); end_h, end_m = map(int, schedule["end"].split(":"))
            choices = [minute for minute in range(start_h * 60 + start_m, end_h * 60 + end_m + 1) if minute % 60 != 0]
            count = int(schedule["daily_count"])
            if count > len(choices):
                raise StoreError("fb_auto_schedule_window_too_small", "随机发布时间窗口不足", 400)
            size = len(choices)
            next_index = [bisect_left(choices, minute + 60) for minute in choices]
            ways = [[0] * (count + 1) for _ in range(size + 1)]
            ways[size][0] = 1
            for index in range(size - 1, -1, -1):
                ways[index][0] = 1
                for remaining in range(1, count + 1):
                    ways[index][remaining] = (
                        ways[index + 1][remaining]
                        + ways[next_index[index]][remaining - 1]
                    )
            if ways[0][count] == 0:
                raise StoreError("fb_auto_schedule_window_too_small", "随机发布时间窗口无法生成安全间隔", 400)
            selected = []
            index, remaining = 0, count
            while remaining:
                take_ways = ways[next_index[index]][remaining - 1]
                skip_ways = ways[index + 1][remaining]
                if self.rng.randrange(take_ways + skip_ways) < take_ways:
                    selected.append(choices[index])
                    index = next_index[index]
                    remaining -= 1
                else:
                    index += 1
            times = [f"{minute // 60:02d}:{minute % 60:02d}" for minute in selected]
            conn.execute("INSERT OR IGNORE INTO fb_auto_schedule_plan VALUES(?,?,?,?,?)", (template_id, version, local_date, json.dumps(times), utc_iso(self.now_fn())))
            stored = conn.execute("SELECT times_json FROM fb_auto_schedule_plan WHERE template_id=? AND template_version=? AND local_date=?", (template_id, version, local_date)).fetchone()
            return list(_loads(stored[0], []))

    def due_templates(self, at: Optional[datetime] = None) -> List[Dict[str, Any]]:
        current = (at or self.now_fn()).astimezone(BEIJING)
        date, minute = current.strftime("%Y-%m-%d"), current.strftime("%H:%M")
        with self.connect() as conn:
            rows = conn.execute("SELECT t.*,v.config_json FROM fb_auto_template t JOIN fb_auto_template_version v ON v.template_id=t.id AND v.version=t.current_version WHERE t.status='enabled' ORDER BY t.id").fetchall()
        result = []
        for row in rows:
            config = _loads(row["config_json"], {})
            if minute in self.schedule_times(int(row["id"]), int(row["current_version"]), config, date):
                result.append({"template_id": int(row["id"]), "template_version": int(row["current_version"]), "slot_key": f"auto:v{int(row['current_version'])}:{date}:{minute}", "actor": ActorScope("scheduler", "自动调度", bool(row["scope_is_admin"]), str(row["owner_user_id"]))})
        return result


__all__ = ["ActorScope", "FBAutoPostStore", "StoreError", "utc_iso"]
