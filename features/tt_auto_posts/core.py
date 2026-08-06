"""Independent SQLite ledger for TikTok automatic publishing templates.

This module deliberately owns no legacy publishing-pool state.  Its database
contains immutable template versions, idempotent schedule runs/account tasks,
a permanent global material reservation ledger, and generation-scoped metric
facts.  The legacy TT Post database is read through ``legacy_reader.py`` only.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import secrets
import sqlite3
from collections.abc import Mapping as MappingABC
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Set, Tuple


UTC = timezone.utc
MAX_JSON_BYTES = 256 * 1024
MAX_NAME_CHARS = 200
MAX_TEXT_CHARS = 1000
MAX_ACCOUNT_ID_CHARS = 64
MAX_IDENTITY_CHARS = 128
METRIC_GENERATIONS_TO_KEEP = 3


def _nonnegative_decimal_text(value: Any, label: str) -> str:
    raw = "0" if value in (None, "") else str(value).strip()
    if not raw or len(raw) > 128:
        raise TTAutoPostStoreError(
            "tt_auto_metric_row_invalid", "%s is invalid" % label, 400
        )
    try:
        number = Decimal(raw)
    except InvalidOperation:
        raise TTAutoPostStoreError(
            "tt_auto_metric_row_invalid", "%s is invalid" % label, 400
        ) from None
    if not number.is_finite() or number < 0:
        raise TTAutoPostStoreError(
            "tt_auto_metric_row_invalid", "%s is invalid" % label, 400
        )
    normalized = format(number, "f")
    if len(normalized) > 128:
        raise TTAutoPostStoreError(
            "tt_auto_metric_row_invalid", "%s is invalid" % label, 400
        )
    return normalized

RUN_STATUSES = frozenset(
    {
        "queued",
        "running",
        "completed",
        "partial_failed",
        "failed",
        "canceled",
    }
)
TASK_STATUSES = frozenset(
    {
        "pending",
        "selecting",
        "no_candidate",
        "reserved",
        "preparing",
        "retry_wait",
        "ready",
        "publishing",
        "reconciling",
        "published",
        "failed",
        "unknown",
        "canceled",
        "skipped",
    }
)
ACTIVE_ACCOUNT_TASK_STATUSES = frozenset(
    {
        "selecting",
        "reserved",
        "preparing",
        "retry_wait",
        "ready",
        "publishing",
        "reconciling",
        "unknown",
    }
)
TERMINAL_TASK_STATUSES = frozenset(
    {
        "no_candidate",
        "published",
        "failed",
        "canceled",
        "skipped",
    }
)
TRIGGER_TYPES = frozenset({"auto", "manual"})
PUBLISH_LOG_STATUS_GROUPS = frozenset(
    {
        "scheduled",
        "processing",
        "published",
        "needs_review",
        "failed",
        "canceled",
        "no_candidate",
        "hold",
        "other",
    }
)
METRIC_GENERATION_STATUSES = frozenset({"building", "ready", "failed"})
METRIC_DIMENSION_TYPES = frozenset({"drama", "material"})


class TTAutoPostStoreError(RuntimeError):
    """Stable, non-secret storage-layer error."""

    def __init__(self, code: str, message: str, status: int = 400):
        self.code = str(code or "tt_auto_store_error")[:96]
        self.status = int(status)
        super().__init__(str(message or "TT auto publishing storage error")[:500])


@dataclass(frozen=True)
class AuditActor:
    user_id: str = ""
    name: str = ""

    @classmethod
    def from_values(cls, user_id: Any = "", name: Any = "") -> "AuditActor":
        return cls(
            user_id=_bounded_text(user_id, "actor user id", 128, allow_empty=True),
            name=_bounded_text(name, "actor name", 200, allow_empty=True),
        )


@dataclass(frozen=True)
class TemplateSnapshot:
    id: int
    name: str
    description: str
    enabled: bool
    enabled_at_utc: str
    version: int
    config: Dict[str, Any]
    config_sha256: str
    confirmed: bool
    confirmation: Dict[str, Any]
    created_at: str
    updated_at: str

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "enabled": self.enabled,
            "enabled_at_utc": self.enabled_at_utc,
            "version": self.version,
            "config": dict(self.config),
            "config_sha256": self.config_sha256,
            "confirmed": self.confirmed,
            "confirmation": dict(self.confirmation),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class RunRecord:
    id: int
    run_key: str
    template_id: int
    template_version: int
    trigger_type: str
    scheduled_at_utc: str
    shanghai_date: str
    publish_time: str
    status: str
    error_code: str
    error_message: str
    metric_generation_id: Optional[int]
    blacklist_snapshot: Dict[str, Any]
    created_at: str
    updated_at: str
    started_at_utc: str
    finished_at_utc: str

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "run_key": self.run_key,
            "template_id": self.template_id,
            "template_version": self.template_version,
            "trigger_type": self.trigger_type,
            "scheduled_at_utc": self.scheduled_at_utc,
            "shanghai_date": self.shanghai_date,
            "publish_time": self.publish_time,
            "status": self.status,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "metric_generation_id": self.metric_generation_id,
            "blacklist_snapshot": dict(self.blacklist_snapshot),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at_utc": self.started_at_utc,
            "finished_at_utc": self.finished_at_utc,
        }


@dataclass(frozen=True)
class TaskRecord:
    id: int
    run_id: int
    template_id: int
    template_version: int
    account_id: str
    account_username: str
    account_display_name: str
    trigger_priority: int
    scheduled_at_utc: str
    status: str
    drama_language: str
    account_setting_version: int
    account_settings: Dict[str, Any]
    content_id: str
    series_code: str
    material_id: str
    selection: Dict[str, Any]
    gpu_job_id: str
    source_media_url: str
    prepared_media_url: str
    prepared_output_sha256: str
    prepared_output_size: int
    prepared_duration_sec: float
    caption: str
    publish_id: str
    publish_url: str
    unknown_outcome: bool
    error_code: str
    error_message: str
    created_at: str
    updated_at: str
    reserved_at_utc: str
    published_at_utc: str

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "run_id": self.run_id,
            "template_id": self.template_id,
            "template_version": self.template_version,
            "account_id": self.account_id,
            "account_username": self.account_username,
            "account_display_name": self.account_display_name,
            "trigger_priority": self.trigger_priority,
            "scheduled_at_utc": self.scheduled_at_utc,
            "status": self.status,
            "drama_language": self.drama_language,
            "account_setting_version": self.account_setting_version,
            "account_settings": dict(self.account_settings),
            "content_id": self.content_id,
            "series_code": self.series_code,
            "material_id": self.material_id,
            "selection": dict(self.selection),
            "gpu_job_id": self.gpu_job_id,
            "source_media_url": self.source_media_url,
            "prepared_media_url": self.prepared_media_url,
            "prepared_output_sha256": self.prepared_output_sha256,
            "prepared_output_size": self.prepared_output_size,
            "prepared_duration_sec": self.prepared_duration_sec,
            "caption": self.caption,
            "publish_id": self.publish_id,
            "publish_url": self.publish_url,
            "unknown_outcome": self.unknown_outcome,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "reserved_at_utc": self.reserved_at_utc,
            "published_at_utc": self.published_at_utc,
        }


@dataclass(frozen=True)
class TaskClaim:
    task: TaskRecord
    claim_token: str
    claim_phase: str = "selection"
    claimed_from_status: str = "pending"

    def reveal_claim_token(self) -> str:
        return self.claim_token

    def __repr__(self) -> str:
        return (
            "TaskClaim(task_id=%r, claim_phase=%r, "
            "claimed_from_status=%r, claim_token=<redacted>)"
            % (self.task.id, self.claim_phase, self.claimed_from_status)
        )


@dataclass(frozen=True)
class MaterialReservation(MappingABC):
    material_id: str
    task_id: int
    run_id: int
    template_id: int
    content_id: str
    reserved_at_utc: str

    def as_dict(self) -> Dict[str, Any]:
        return {
            "material_id": self.material_id,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "template_id": self.template_id,
            "content_id": self.content_id,
            "reserved_at_utc": self.reserved_at_utc,
        }

    def __getitem__(self, key: str) -> Any:
        return self.as_dict()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.as_dict())

    def __len__(self) -> int:
        return len(self.as_dict())


@dataclass(frozen=True)
class MetricGeneration:
    id: int
    generation_key: str
    platform: int
    metric_date: str
    product: str
    status: str
    row_count: int
    checksum: str
    metadata: Dict[str, Any]
    created_at: str
    ready_at_utc: str
    failed_at_utc: str
    activated_at_utc: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "generation_key": self.generation_key,
            "platform": self.platform,
            "metric_date": self.metric_date,
            "product": self.product,
            "status": self.status,
            "row_count": self.row_count,
            "checksum": self.checksum,
            "metadata": dict(self.metadata),
            "created_at": self.created_at,
            "ready_at_utc": self.ready_at_utc,
            "failed_at_utc": self.failed_at_utc,
            "activated_at_utc": self.activated_at_utc,
        }


def _bounded_text(
    value: Any,
    label: str,
    limit: int,
    *,
    allow_empty: bool = False,
) -> str:
    text = str(value or "").strip()
    if (not text and not allow_empty) or len(text) > int(limit) or "\x00" in text:
        raise TTAutoPostStoreError(
            "tt_auto_invalid_%s" % re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_"),
            "%s is invalid" % label,
            400,
        )
    return text


def _positive_int(value: Any, label: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        result = 0
    if result <= 0:
        raise TTAutoPostStoreError("tt_auto_invalid_id", "%s is invalid" % label, 400)
    return result


def _nonnegative_int(value: Any, label: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        result = -1
    if result < 0:
        raise TTAutoPostStoreError("tt_auto_invalid_number", "%s is invalid" % label, 400)
    return result


def _canonical_json(value: Any, label: str) -> Tuple[str, str]:
    if not isinstance(value, Mapping):
        raise TTAutoPostStoreError("tt_auto_invalid_json", "%s must be an object" % label, 400)
    try:
        encoded = json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError):
        raise TTAutoPostStoreError("tt_auto_invalid_json", "%s is not valid JSON" % label, 400) from None
    raw = encoded.encode("utf-8")
    if len(raw) > MAX_JSON_BYTES:
        raise TTAutoPostStoreError("tt_auto_json_too_large", "%s is too large" % label, 413)
    return encoded, hashlib.sha256(raw).hexdigest()


def _json_object(raw: Any) -> Dict[str, Any]:
    try:
        value = json.loads(str(raw or "{}"))
    except (TypeError, ValueError):
        value = {}
    return dict(value) if isinstance(value, dict) else {}


def _utc_datetime(value: Any, label: str = "UTC time") -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except (TypeError, ValueError):
            raise TTAutoPostStoreError("tt_auto_invalid_time", "%s is invalid" % label, 400) from None
    if parsed.tzinfo is None:
        raise TTAutoPostStoreError("tt_auto_invalid_time", "%s must include a timezone" % label, 400)
    return parsed.astimezone(UTC)


def _utc_iso(value: Any, label: str = "UTC time") -> str:
    return _utc_datetime(value, label).isoformat(timespec="seconds")


def _shanghai_date(value: Any) -> str:
    text = str(value or "").strip()
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        raise TTAutoPostStoreError("tt_auto_invalid_date", "Shanghai date is invalid", 400) from None
    if parsed.isoformat() != text:
        raise TTAutoPostStoreError("tt_auto_invalid_date", "Shanghai date is invalid", 400)
    return text


def _publish_time(value: Any) -> str:
    text = str(value or "").strip()
    if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", text):
        raise TTAutoPostStoreError("tt_auto_invalid_publish_time", "publish time is invalid", 400)
    return text


def _connect(db_path: Any) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def ensure_storage(db_path: Any) -> None:
    """Create the independent ledger idempotently."""

    path = Path(str(db_path)).expanduser()
    if not path.is_absolute():
        raise TTAutoPostStoreError("tt_auto_db_path_invalid", "database path must be absolute", 500)
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(_connect(path)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            conn.executescript(
                """
                BEGIN IMMEDIATE;

                CREATE TABLE IF NOT EXISTS tt_auto_template (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
                    enabled_at_utc TEXT NOT NULL DEFAULT '',
                    current_version INTEGER NOT NULL CHECK(current_version>0),
                    created_by_user_id TEXT NOT NULL DEFAULT '',
                    created_by_name TEXT NOT NULL DEFAULT '',
                    updated_by_user_id TEXT NOT NULL DEFAULT '',
                    updated_by_name TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tt_auto_template_version (
                    template_id INTEGER NOT NULL,
                    version INTEGER NOT NULL CHECK(version>0),
                    config_json TEXT NOT NULL,
                    config_sha256 TEXT NOT NULL,
                    confirmed INTEGER NOT NULL DEFAULT 0 CHECK(confirmed IN (0,1)),
                    confirmation_json TEXT NOT NULL DEFAULT '{}',
                    confirmed_by_user_id TEXT NOT NULL DEFAULT '',
                    confirmed_by_name TEXT NOT NULL DEFAULT '',
                    confirmed_at_utc TEXT NOT NULL DEFAULT '',
                    created_by_user_id TEXT NOT NULL DEFAULT '',
                    created_by_name TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(template_id,version),
                    FOREIGN KEY(template_id) REFERENCES tt_auto_template(id)
                        ON DELETE RESTRICT
                );

                CREATE TABLE IF NOT EXISTS tt_auto_random_plan (
                    template_id INTEGER NOT NULL,
                    template_version INTEGER NOT NULL,
                    shanghai_date TEXT NOT NULL,
                    publish_times_json TEXT NOT NULL,
                    plan_sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(template_id,template_version,shanghai_date),
                    FOREIGN KEY(template_id,template_version)
                        REFERENCES tt_auto_template_version(template_id,version)
                        ON DELETE RESTRICT
                );

                CREATE TABLE IF NOT EXISTS tt_auto_metric_generation (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    generation_key TEXT NOT NULL UNIQUE,
                    platform INTEGER NOT NULL CHECK(platform>=0),
                    metric_date TEXT NOT NULL,
                    product TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'building'
                        CHECK(status IN ('building','ready','failed')),
                    row_count INTEGER NOT NULL DEFAULT 0 CHECK(row_count>=0),
                    checksum TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    error_code TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT '',
                    created_by_user_id TEXT NOT NULL DEFAULT '',
                    created_by_name TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    ready_at_utc TEXT NOT NULL DEFAULT '',
                    failed_at_utc TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS tt_auto_metric_daily (
                    generation_id INTEGER NOT NULL,
                    content_id TEXT NOT NULL COLLATE BINARY,
                    material_id TEXT NOT NULL COLLATE BINARY,
                    spend TEXT NOT NULL DEFAULT '0',
                    af_revenue0 TEXT NOT NULL DEFAULT '0',
                    PRIMARY KEY(generation_id,material_id),
                    FOREIGN KEY(generation_id)
                        REFERENCES tt_auto_metric_generation(id)
                        ON DELETE RESTRICT
                );

                CREATE TABLE IF NOT EXISTS tt_auto_metric_active_pointer (
                    platform INTEGER NOT NULL CHECK(platform>=0),
                    metric_date TEXT NOT NULL,
                    product TEXT NOT NULL,
                    generation_id INTEGER NOT NULL,
                    activated_by_user_id TEXT NOT NULL DEFAULT '',
                    activated_by_name TEXT NOT NULL DEFAULT '',
                    activated_at_utc TEXT NOT NULL,
                    FOREIGN KEY(generation_id)
                        REFERENCES tt_auto_metric_generation(id)
                        ON DELETE RESTRICT,
                    PRIMARY KEY(platform,metric_date,product)
                );

                CREATE TABLE IF NOT EXISTS tt_auto_run (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_key TEXT NOT NULL UNIQUE,
                    template_id INTEGER NOT NULL,
                    template_version INTEGER NOT NULL,
                    config_sha256 TEXT NOT NULL,
                    trigger_type TEXT NOT NULL CHECK(trigger_type IN ('auto','manual')),
                    scheduled_at_utc TEXT NOT NULL,
                    shanghai_date TEXT NOT NULL,
                    publish_time TEXT NOT NULL,
                    metric_generation_id INTEGER,
                    blacklist_snapshot_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'queued'
                        CHECK(status IN (
                            'queued','running','completed','partial_failed',
                            'failed','canceled'
                        )),
                    error_code TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT '',
                    created_by_user_id TEXT NOT NULL DEFAULT '',
                    created_by_name TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at_utc TEXT NOT NULL DEFAULT '',
                    finished_at_utc TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY(template_id,template_version)
                        REFERENCES tt_auto_template_version(template_id,version)
                        ON DELETE RESTRICT,
                    FOREIGN KEY(metric_generation_id)
                        REFERENCES tt_auto_metric_generation(id)
                        ON DELETE RESTRICT
                );

                CREATE TABLE IF NOT EXISTS tt_auto_task (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL,
                    template_id INTEGER NOT NULL,
                    template_version INTEGER NOT NULL,
                    account_id TEXT NOT NULL,
                    account_username TEXT NOT NULL DEFAULT '',
                    account_display_name TEXT NOT NULL DEFAULT '',
                    trigger_priority INTEGER NOT NULL CHECK(trigger_priority IN (0,1)),
                    scheduled_at_utc TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN (
                            'pending','selecting','no_candidate','reserved',
                            'preparing','retry_wait','ready','publishing',
                            'reconciling','published','failed','unknown',
                            'canceled','skipped'
                        )),
                    drama_language TEXT NOT NULL,
                    account_setting_version INTEGER NOT NULL DEFAULT 0
                        CHECK(account_setting_version>=0),
                    account_settings_json TEXT NOT NULL DEFAULT '{}',
                    content_id TEXT NOT NULL DEFAULT '',
                    series_code TEXT NOT NULL DEFAULT '',
                    material_id TEXT NOT NULL DEFAULT '',
                    selection_json TEXT NOT NULL DEFAULT '{}',
                    gpu_job_id TEXT NOT NULL DEFAULT '',
                    source_media_url TEXT NOT NULL DEFAULT '',
                    prepared_media_url TEXT NOT NULL DEFAULT '',
                    prepared_output_sha256 TEXT NOT NULL DEFAULT '',
                    prepared_output_size INTEGER NOT NULL DEFAULT 0
                        CHECK(prepared_output_size>=0),
                    prepared_duration_sec REAL NOT NULL DEFAULT 0
                        CHECK(prepared_duration_sec>=0),
                    caption TEXT NOT NULL DEFAULT '',
                    publish_id TEXT NOT NULL DEFAULT '',
                    publish_url TEXT NOT NULL DEFAULT '',
                    unknown_outcome INTEGER NOT NULL DEFAULT 0
                        CHECK(unknown_outcome IN (0,1)),
                    preparation_attempt_count INTEGER NOT NULL DEFAULT 0
                        CHECK(preparation_attempt_count>=0),
                    publish_attempt_count INTEGER NOT NULL DEFAULT 0
                        CHECK(publish_attempt_count>=0),
                    next_attempt_at_utc TEXT NOT NULL DEFAULT '',
                    claim_phase TEXT NOT NULL DEFAULT ''
                        CHECK(claim_phase IN ('','selection','prepare','publish','reconcile')),
                    claim_worker TEXT NOT NULL DEFAULT '',
                    claim_token TEXT NOT NULL DEFAULT '',
                    lease_expires_at_utc TEXT NOT NULL DEFAULT '',
                    error_code TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    selected_at_utc TEXT NOT NULL DEFAULT '',
                    reserved_at_utc TEXT NOT NULL DEFAULT '',
                    prepared_at_utc TEXT NOT NULL DEFAULT '',
                    published_at_utc TEXT NOT NULL DEFAULT '',
                    finished_at_utc TEXT NOT NULL DEFAULT '',
                    UNIQUE(run_id,account_id),
                    FOREIGN KEY(run_id) REFERENCES tt_auto_run(id)
                        ON DELETE RESTRICT,
                    FOREIGN KEY(template_id,template_version)
                        REFERENCES tt_auto_template_version(template_id,version)
                        ON DELETE RESTRICT
                );

                CREATE TABLE IF NOT EXISTS tt_auto_material_ledger (
                    material_id TEXT PRIMARY KEY COLLATE BINARY,
                    task_id INTEGER NOT NULL UNIQUE,
                    run_id INTEGER NOT NULL,
                    template_id INTEGER NOT NULL,
                    content_id TEXT NOT NULL COLLATE BINARY,
                    reserved_at_utc TEXT NOT NULL,
                    last_task_status TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES tt_auto_task(id)
                        ON DELETE RESTRICT,
                    FOREIGN KEY(run_id) REFERENCES tt_auto_run(id)
                        ON DELETE RESTRICT,
                    FOREIGN KEY(template_id) REFERENCES tt_auto_template(id)
                        ON DELETE RESTRICT
                );

                CREATE TABLE IF NOT EXISTS tt_auto_event (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER,
                    task_id INTEGER,
                    event_type TEXT NOT NULL,
                    from_status TEXT NOT NULL DEFAULT '',
                    to_status TEXT NOT NULL DEFAULT '',
                    message TEXT NOT NULL DEFAULT '',
                    details_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES tt_auto_run(id)
                        ON DELETE RESTRICT,
                    FOREIGN KEY(task_id) REFERENCES tt_auto_task(id)
                        ON DELETE RESTRICT
                );

                CREATE INDEX IF NOT EXISTS idx_tt_auto_template_enabled
                    ON tt_auto_template(enabled,id);
                CREATE INDEX IF NOT EXISTS idx_tt_auto_run_due
                    ON tt_auto_run(status,scheduled_at_utc,template_id,id);
                CREATE INDEX IF NOT EXISTS idx_tt_auto_task_dispatch
                    ON tt_auto_task(
                        status,trigger_priority,scheduled_at_utc,template_id,id
                    );
                CREATE INDEX IF NOT EXISTS idx_tt_auto_task_account
                    ON tt_auto_task(account_id,status,scheduled_at_utc,id);
                CREATE INDEX IF NOT EXISTS idx_tt_auto_task_lease
                    ON tt_auto_task(status,lease_expires_at_utc,id);
                CREATE UNIQUE INDEX IF NOT EXISTS ux_tt_auto_task_gpu_job
                    ON tt_auto_task(gpu_job_id) WHERE gpu_job_id<>'';
                CREATE UNIQUE INDEX IF NOT EXISTS ux_tt_auto_task_publish_id
                    ON tt_auto_task(publish_id) WHERE publish_id<>'';
                CREATE INDEX IF NOT EXISTS idx_tt_auto_material_drama_cooldown
                    ON tt_auto_material_ledger(
                        template_id,content_id,reserved_at_utc DESC
                    );
                CREATE INDEX IF NOT EXISTS idx_tt_auto_event_run
                    ON tt_auto_event(run_id,id);
                CREATE INDEX IF NOT EXISTS idx_tt_auto_event_task
                    ON tt_auto_event(task_id,id);
                CREATE INDEX IF NOT EXISTS idx_tt_auto_metric_lookup
                    ON tt_auto_metric_daily(
                        generation_id,content_id,material_id
                    );

                CREATE TRIGGER IF NOT EXISTS trg_tt_auto_material_no_delete
                BEFORE DELETE ON tt_auto_material_ledger
                BEGIN
                    SELECT RAISE(ABORT, 'tt_auto_material_ledger_is_permanent');
                END;

                CREATE TRIGGER IF NOT EXISTS trg_tt_auto_material_identity_immutable
                BEFORE UPDATE OF material_id,task_id,run_id,template_id,content_id,reserved_at_utc
                ON tt_auto_material_ledger
                BEGIN
                    SELECT RAISE(ABORT, 'tt_auto_material_identity_is_immutable');
                END;

                COMMIT;
                """
            )
            template_columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(tt_auto_template)")
            }
            if "enabled_at_utc" not in template_columns:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """
                    ALTER TABLE tt_auto_template
                    ADD COLUMN enabled_at_utc TEXT NOT NULL DEFAULT ''
                    """
                )
                conn.execute(
                    """
                    UPDATE tt_auto_template
                    SET enabled_at_utc=updated_at
                    WHERE enabled=1 AND enabled_at_utc=''
                    """
                )
                conn.commit()
            task_columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(tt_auto_task)")
            }
            missing_task_identity_columns = [
                column
                for column in ("account_username", "account_display_name")
                if column not in task_columns
            ]
            if missing_task_identity_columns:
                conn.execute("BEGIN IMMEDIATE")
                for column in missing_task_identity_columns:
                    conn.execute(
                        "ALTER TABLE tt_auto_task ADD COLUMN %s TEXT NOT NULL DEFAULT ''"
                        % column
                    )
                conn.commit()
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise


def _template_from_row(row: sqlite3.Row) -> TemplateSnapshot:
    return TemplateSnapshot(
        id=int(row["id"]),
        name=str(row["name"]),
        description=str(row["description"] or ""),
        enabled=bool(row["enabled"]),
        enabled_at_utc=str(row["enabled_at_utc"] or ""),
        version=int(row["version"]),
        config=_json_object(row["config_json"]),
        config_sha256=str(row["config_sha256"]),
        confirmed=bool(row["confirmed"]),
        confirmation=_json_object(row["confirmation_json"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _run_from_row(row: sqlite3.Row) -> RunRecord:
    raw_generation = row["metric_generation_id"]
    return RunRecord(
        id=int(row["id"]),
        run_key=str(row["run_key"]),
        template_id=int(row["template_id"]),
        template_version=int(row["template_version"]),
        trigger_type=str(row["trigger_type"]),
        scheduled_at_utc=str(row["scheduled_at_utc"]),
        shanghai_date=str(row["shanghai_date"]),
        publish_time=str(row["publish_time"]),
        status=str(row["status"]),
        error_code=str(row["error_code"] or ""),
        error_message=str(row["error_message"] or ""),
        metric_generation_id=(int(raw_generation) if raw_generation is not None else None),
        blacklist_snapshot=_json_object(row["blacklist_snapshot_json"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        started_at_utc=str(row["started_at_utc"] or ""),
        finished_at_utc=str(row["finished_at_utc"] or ""),
    )


def _task_from_row(row: sqlite3.Row) -> TaskRecord:
    return TaskRecord(
        id=int(row["id"]),
        run_id=int(row["run_id"]),
        template_id=int(row["template_id"]),
        template_version=int(row["template_version"]),
        account_id=str(row["account_id"]),
        account_username=str(row["account_username"] or ""),
        account_display_name=str(row["account_display_name"] or ""),
        trigger_priority=int(row["trigger_priority"]),
        scheduled_at_utc=str(row["scheduled_at_utc"]),
        status=str(row["status"]),
        drama_language=str(row["drama_language"]),
        account_setting_version=int(row["account_setting_version"]),
        account_settings=_json_object(row["account_settings_json"]),
        content_id=str(row["content_id"] or ""),
        series_code=str(row["series_code"] or ""),
        material_id=str(row["material_id"] or ""),
        selection=_json_object(row["selection_json"]),
        gpu_job_id=str(row["gpu_job_id"] or ""),
        source_media_url=str(row["source_media_url"] or ""),
        prepared_media_url=str(row["prepared_media_url"] or ""),
        prepared_output_sha256=str(row["prepared_output_sha256"] or ""),
        prepared_output_size=int(row["prepared_output_size"] or 0),
        prepared_duration_sec=float(row["prepared_duration_sec"] or 0),
        caption=str(row["caption"] or ""),
        publish_id=str(row["publish_id"] or ""),
        publish_url=str(row["publish_url"] or ""),
        unknown_outcome=bool(row["unknown_outcome"]),
        error_code=str(row["error_code"] or ""),
        error_message=str(row["error_message"] or ""),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        reserved_at_utc=str(row["reserved_at_utc"] or ""),
        published_at_utc=str(row["published_at_utc"] or ""),
    )


def _metric_generation_from_row(row: sqlite3.Row) -> MetricGeneration:
    keys = set(row.keys())
    return MetricGeneration(
        id=int(row["id"]),
        generation_key=str(row["generation_key"]),
        platform=int(row["platform"]),
        metric_date=str(row["metric_date"]),
        product=str(row["product"]),
        status=str(row["status"]),
        row_count=int(row["row_count"] or 0),
        checksum=str(row["checksum"] or ""),
        metadata=_json_object(row["metadata_json"]),
        created_at=str(row["created_at"]),
        ready_at_utc=str(row["ready_at_utc"] or ""),
        failed_at_utc=str(row["failed_at_utc"] or ""),
        activated_at_utc=(str(row["activated_at_utc"] or "") if "activated_at_utc" in keys else ""),
    )


class TTAutoPostStore:
    """Transactional API for the independent automatic publishing ledger."""

    def __init__(
        self,
        db_path: Any,
        *,
        now_fn: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        path = Path(str(db_path)).expanduser()
        if not path.is_absolute():
            raise TTAutoPostStoreError("tt_auto_db_path_invalid", "database path must be absolute", 500)
        self.db_path = str(path)
        self._now_fn = now_fn
        ensure_storage(path)

    def _now(self) -> datetime:
        value = self._now_fn()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise TTAutoPostStoreError("tt_auto_clock_invalid", "clock must return a timezone-aware datetime", 500)
        return value.astimezone(UTC)

    def _now_iso(self) -> str:
        return self._now().isoformat(timespec="seconds")

    @contextlib.contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with contextlib.closing(_connect(self.db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    @contextlib.contextmanager
    def _reader(self) -> Iterator[sqlite3.Connection]:
        with contextlib.closing(_connect(self.db_path)) as conn:
            yield conn

    @staticmethod
    def _event(
        conn: sqlite3.Connection,
        *,
        event_type: Any,
        created_at: str,
        run_id: Optional[int] = None,
        task_id: Optional[int] = None,
        from_status: Any = "",
        to_status: Any = "",
        message: Any = "",
        details: Optional[Mapping[str, Any]] = None,
    ) -> None:
        event = _bounded_text(event_type, "event type", 96)
        details_json, _ = _canonical_json(details or {}, "event details")
        conn.execute(
            """
            INSERT INTO tt_auto_event(
                run_id,task_id,event_type,from_status,to_status,
                message,details_json,created_at
            ) VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                run_id,
                task_id,
                event,
                _bounded_text(from_status, "from status", 64, allow_empty=True),
                _bounded_text(to_status, "to status", 64, allow_empty=True),
                _bounded_text(message, "event message", MAX_TEXT_CHARS, allow_empty=True),
                details_json,
                created_at,
            ),
        )

    @staticmethod
    def _template_row(conn: sqlite3.Connection, template_id: int, version: Optional[int] = None) -> Optional[sqlite3.Row]:
        version_expression = "t.current_version" if version is None else "?"
        params: Tuple[Any, ...] = (template_id,) if version is None else (version, template_id)
        return conn.execute(
            """
            SELECT
                t.id,t.name,t.description,t.enabled,t.enabled_at_utc,
                v.version,v.config_json,v.config_sha256,
                v.confirmed,v.confirmation_json,
                t.created_at,t.updated_at
            FROM tt_auto_template t
            JOIN tt_auto_template_version v
              ON v.template_id=t.id AND v.version=%s
            WHERE t.id=?
            """ % version_expression,
            params,
        ).fetchone()

    def create_template(
        self,
        *,
        name: Any,
        config: Mapping[str, Any],
        description: Any = "",
        actor: AuditActor = AuditActor(),
        confirmation: Optional[Mapping[str, Any]] = None,
    ) -> TemplateSnapshot:
        clean_name = _bounded_text(name, "template name", MAX_NAME_CHARS)
        clean_description = _bounded_text(description, "template description", MAX_TEXT_CHARS, allow_empty=True)
        config_json, config_sha = _canonical_json(config, "template config")
        confirmation_json, _ = _canonical_json(confirmation or {}, "template confirmation")
        confirmed = bool(isinstance(confirmation, Mapping) and confirmation.get("accepted") is True)
        actor = AuditActor.from_values(actor.user_id, actor.name)
        now = self._now_iso()
        with self._transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO tt_auto_template(
                    name,description,enabled,current_version,
                    created_by_user_id,created_by_name,
                    updated_by_user_id,updated_by_name,created_at,updated_at
                ) VALUES(?,?,0,1,?,?,?,?,?,?)
                """,
                (
                    clean_name,
                    clean_description,
                    actor.user_id,
                    actor.name,
                    actor.user_id,
                    actor.name,
                    now,
                    now,
                ),
            )
            template_id = int(cursor.lastrowid)
            conn.execute(
                """
                INSERT INTO tt_auto_template_version(
                    template_id,version,config_json,config_sha256,
                    confirmed,confirmation_json,
                    confirmed_by_user_id,confirmed_by_name,confirmed_at_utc,
                    created_by_user_id,created_by_name,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    template_id,
                    1,
                    config_json,
                    config_sha,
                    int(confirmed),
                    confirmation_json,
                    actor.user_id if confirmed else "",
                    actor.name if confirmed else "",
                    now if confirmed else "",
                    actor.user_id,
                    actor.name,
                    now,
                ),
            )
            self._event(
                conn,
                event_type="template_created",
                created_at=now,
                details={"template_id": template_id, "version": 1, "confirmed": confirmed},
            )
            row = self._template_row(conn, template_id)
        assert row is not None
        return _template_from_row(row)

    def get_template(self, template_id: Any, *, version: Optional[Any] = None) -> TemplateSnapshot:
        normalized_id = _positive_int(template_id, "template id")
        normalized_version = None if version is None else _positive_int(version, "template version")
        with self._reader() as conn:
            row = self._template_row(conn, normalized_id, normalized_version)
        if row is None:
            raise TTAutoPostStoreError("tt_auto_template_not_found", "template was not found", 404)
        return _template_from_row(row)

    def list_templates(self, *, enabled: Optional[bool] = None) -> List[TemplateSnapshot]:
        sql = """
            SELECT
                t.id,t.name,t.description,t.enabled,t.enabled_at_utc,
                v.version,v.config_json,v.config_sha256,
                v.confirmed,v.confirmation_json,
                t.created_at,t.updated_at
            FROM tt_auto_template t
            JOIN tt_auto_template_version v
              ON v.template_id=t.id AND v.version=t.current_version
        """
        params: Tuple[Any, ...] = ()
        if enabled is not None:
            sql += " WHERE t.enabled=?"
            params = (int(bool(enabled)),)
        sql += " ORDER BY t.id"
        with self._reader() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_template_from_row(row) for row in rows]

    def update_template(
        self,
        template_id: Any,
        *,
        expected_version: Any,
        config: Mapping[str, Any],
        name: Optional[Any] = None,
        description: Optional[Any] = None,
        actor: AuditActor = AuditActor(),
        confirmation: Optional[Mapping[str, Any]] = None,
    ) -> TemplateSnapshot:
        normalized_id = _positive_int(template_id, "template id")
        expected = _positive_int(expected_version, "expected version")
        config_json, config_sha = _canonical_json(config, "template config")
        confirmation_json, _ = _canonical_json(confirmation or {}, "template confirmation")
        confirmed = bool(isinstance(confirmation, Mapping) and confirmation.get("accepted") is True)
        actor = AuditActor.from_values(actor.user_id, actor.name)
        now = self._now_iso()
        with self._transaction() as conn:
            current = conn.execute("SELECT * FROM tt_auto_template WHERE id=?", (normalized_id,)).fetchone()
            if current is None:
                raise TTAutoPostStoreError("tt_auto_template_not_found", "template was not found", 404)
            if int(current["current_version"]) != expected:
                raise TTAutoPostStoreError("tt_auto_template_version_conflict", "template version changed", 409)
            next_version = expected + 1
            clean_name = str(current["name"]) if name is None else _bounded_text(name, "template name", MAX_NAME_CHARS)
            clean_description = (
                str(current["description"] or "")
                if description is None
                else _bounded_text(description, "template description", MAX_TEXT_CHARS, allow_empty=True)
            )
            conn.execute(
                """
                INSERT INTO tt_auto_template_version(
                    template_id,version,config_json,config_sha256,
                    confirmed,confirmation_json,
                    confirmed_by_user_id,confirmed_by_name,confirmed_at_utc,
                    created_by_user_id,created_by_name,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    normalized_id,
                    next_version,
                    config_json,
                    config_sha,
                    int(confirmed),
                    confirmation_json,
                    actor.user_id if confirmed else "",
                    actor.name if confirmed else "",
                    now if confirmed else "",
                    actor.user_id,
                    actor.name,
                    now,
                ),
            )
            conn.execute(
                """
                UPDATE tt_auto_template
                SET name=?,description=?,current_version=?,enabled=0,
                    enabled_at_utc='',updated_by_user_id=?,updated_by_name=?,updated_at=?
                WHERE id=?
                """,
                (
                    clean_name,
                    clean_description,
                    next_version,
                    actor.user_id,
                    actor.name,
                    now,
                    normalized_id,
                ),
            )
            self._event(
                conn,
                event_type="template_updated",
                created_at=now,
                details={"template_id": normalized_id, "from_version": expected, "version": next_version, "confirmed": confirmed},
            )
            row = self._template_row(conn, normalized_id)
        assert row is not None
        return _template_from_row(row)

    def confirm_template_version(
        self,
        template_id: Any,
        version: Any,
        *,
        confirmation: Mapping[str, Any],
        actor: AuditActor = AuditActor(),
    ) -> TemplateSnapshot:
        normalized_id = _positive_int(template_id, "template id")
        normalized_version = _positive_int(version, "template version")
        if confirmation.get("accepted") is not True:
            raise TTAutoPostStoreError("tt_auto_confirmation_required", "explicit confirmation is required", 400)
        confirmation_json, _ = _canonical_json(confirmation, "template confirmation")
        actor = AuditActor.from_values(actor.user_id, actor.name)
        now = self._now_iso()
        with self._transaction() as conn:
            current = conn.execute(
                "SELECT current_version FROM tt_auto_template WHERE id=?",
                (normalized_id,),
            ).fetchone()
            if current is None:
                raise TTAutoPostStoreError("tt_auto_template_not_found", "template was not found", 404)
            if int(current["current_version"]) != normalized_version:
                raise TTAutoPostStoreError("tt_auto_template_version_conflict", "only the current template version can be confirmed", 409)
            cursor = conn.execute(
                """
                UPDATE tt_auto_template_version
                SET confirmed=1,confirmation_json=?,confirmed_by_user_id=?,
                    confirmed_by_name=?,confirmed_at_utc=?
                WHERE template_id=? AND version=?
                """,
                (confirmation_json, actor.user_id, actor.name, now, normalized_id, normalized_version),
            )
            if cursor.rowcount != 1:
                raise TTAutoPostStoreError("tt_auto_template_not_found", "template version was not found", 404)
            self._event(
                conn,
                event_type="template_confirmed",
                created_at=now,
                details={"template_id": normalized_id, "version": normalized_version},
            )
            row = self._template_row(conn, normalized_id)
        assert row is not None
        return _template_from_row(row)

    def set_template_enabled(
        self,
        template_id: Any,
        enabled: bool,
        *,
        expected_version: Any,
        actor: AuditActor = AuditActor(),
    ) -> TemplateSnapshot:
        normalized_id = _positive_int(template_id, "template id")
        expected = _positive_int(expected_version, "expected version")
        if type(enabled) is not bool:
            raise TTAutoPostStoreError("tt_auto_enabled_invalid", "enabled must be a boolean", 400)
        actor = AuditActor.from_values(actor.user_id, actor.name)
        now = self._now_iso()
        with self._transaction() as conn:
            row = self._template_row(conn, normalized_id)
            if row is None:
                raise TTAutoPostStoreError("tt_auto_template_not_found", "template was not found", 404)
            if int(row["version"]) != expected:
                raise TTAutoPostStoreError("tt_auto_template_version_conflict", "template version changed", 409)
            if enabled and not bool(row["confirmed"]):
                raise TTAutoPostStoreError("tt_auto_template_version_unconfirmed", "template version must be confirmed before enabling", 409)
            if bool(row["enabled"]) is enabled:
                return _template_from_row(row)
            conn.execute(
                """
                UPDATE tt_auto_template
                SET enabled=?,enabled_at_utc=?,updated_by_user_id=?,updated_by_name=?,updated_at=?
                WHERE id=?
                """,
                (
                    int(enabled),
                    now if enabled else "",
                    actor.user_id,
                    actor.name,
                    now,
                    normalized_id,
                ),
            )
            self._event(
                conn,
                event_type="template_enabled" if enabled else "template_disabled",
                created_at=now,
                details={"template_id": normalized_id, "version": expected},
            )
            result = self._template_row(conn, normalized_id)
        assert result is not None
        return _template_from_row(result)

    def copy_template(
        self,
        template_id: Any,
        *,
        name: Optional[Any] = None,
        actor: AuditActor = AuditActor(),
    ) -> TemplateSnapshot:
        source = self.get_template(template_id)
        copy_name = name if name is not None else "%s copy" % source.name
        result = self.create_template(
            name=copy_name,
            description=source.description,
            config=source.config,
            actor=actor,
            confirmation=None,
        )
        now = self._now_iso()
        with self._transaction() as conn:
            self._event(
                conn,
                event_type="template_copied",
                created_at=now,
                details={"source_template_id": source.id, "template_id": result.id},
            )
        return result

    def put_random_plan(
        self,
        template_id: Any,
        template_version: Any,
        shanghai_date: Any,
        publish_times: Sequence[Any],
    ) -> List[str]:
        normalized_id = _positive_int(template_id, "template id")
        normalized_version = _positive_int(template_version, "template version")
        normalized_date = _shanghai_date(shanghai_date)
        if isinstance(publish_times, (str, bytes)) or not isinstance(publish_times, Sequence):
            raise TTAutoPostStoreError("tt_auto_publish_times_invalid", "publish times must be a list", 400)
        normalized_times = sorted({_publish_time(value) for value in publish_times})
        if not normalized_times or len(normalized_times) > 24:
            raise TTAutoPostStoreError("tt_auto_publish_times_invalid", "publish times count is invalid", 400)
        encoded = json.dumps(normalized_times, separators=(",", ":"))
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        now = self._now_iso()
        with self._transaction() as conn:
            version_row = conn.execute(
                "SELECT 1 FROM tt_auto_template_version WHERE template_id=? AND version=?",
                (normalized_id, normalized_version),
            ).fetchone()
            if version_row is None:
                raise TTAutoPostStoreError("tt_auto_template_not_found", "template version was not found", 404)
            existing = conn.execute(
                """
                SELECT publish_times_json,plan_sha256
                FROM tt_auto_random_plan
                WHERE template_id=? AND template_version=? AND shanghai_date=?
                """,
                (normalized_id, normalized_version, normalized_date),
            ).fetchone()
            if existing is not None:
                if secrets.compare_digest(str(existing["plan_sha256"]), digest):
                    return list(json.loads(existing["publish_times_json"]))
                raise TTAutoPostStoreError("tt_auto_random_plan_conflict", "random plan already exists with different times", 409)
            conn.execute(
                """
                INSERT INTO tt_auto_random_plan(
                    template_id,template_version,shanghai_date,
                    publish_times_json,plan_sha256,created_at
                ) VALUES(?,?,?,?,?,?)
                """,
                (normalized_id, normalized_version, normalized_date, encoded, digest, now),
            )
        return normalized_times

    def get_random_plan(self, template_id: Any, template_version: Any, shanghai_date: Any) -> Optional[List[str]]:
        normalized_id = _positive_int(template_id, "template id")
        normalized_version = _positive_int(template_version, "template version")
        normalized_date = _shanghai_date(shanghai_date)
        with self._reader() as conn:
            row = conn.execute(
                """
                SELECT publish_times_json FROM tt_auto_random_plan
                WHERE template_id=? AND template_version=? AND shanghai_date=?
                """,
                (normalized_id, normalized_version, normalized_date),
            ).fetchone()
        return None if row is None else list(json.loads(row["publish_times_json"]))

    def create_run(
        self,
        *,
        run_key: Any,
        template_id: Any,
        template_version: Any,
        trigger_type: Any,
        scheduled_at_utc: Any,
        shanghai_date: Any,
        publish_time: Any,
        metric_generation_id: Optional[Any] = None,
        blacklist_snapshot: Optional[Mapping[str, Any]] = None,
        actor: AuditActor = AuditActor(),
    ) -> RunRecord:
        clean_key = _bounded_text(run_key, "run key", 255)
        normalized_template_id = _positive_int(template_id, "template id")
        normalized_version = _positive_int(template_version, "template version")
        clean_trigger = str(trigger_type or "").strip()
        if clean_trigger not in TRIGGER_TYPES:
            raise TTAutoPostStoreError("tt_auto_trigger_invalid", "trigger type is invalid", 400)
        scheduled = _utc_iso(scheduled_at_utc, "scheduled time")
        normalized_date = _shanghai_date(shanghai_date)
        normalized_publish_time = _publish_time(publish_time)
        generation_id = None if metric_generation_id is None else _positive_int(metric_generation_id, "metric generation id")
        blacklist_json, _ = _canonical_json(blacklist_snapshot or {}, "blacklist snapshot")
        actor = AuditActor.from_values(actor.user_id, actor.name)
        now = self._now_iso()
        with self._transaction() as conn:
            version_row = conn.execute(
                """
                SELECT config_sha256,confirmed
                FROM tt_auto_template_version
                WHERE template_id=? AND version=?
                """,
                (normalized_template_id, normalized_version),
            ).fetchone()
            if version_row is None:
                raise TTAutoPostStoreError("tt_auto_template_not_found", "template version was not found", 404)
            if not bool(version_row["confirmed"]):
                raise TTAutoPostStoreError("tt_auto_template_version_unconfirmed", "template version must be confirmed before execution", 409)
            if clean_trigger == "auto":
                current_template = conn.execute(
                    """
                    SELECT enabled,current_version,enabled_at_utc
                    FROM tt_auto_template
                    WHERE id=?
                    """,
                    (normalized_template_id,),
                ).fetchone()
                enabled_at = str(
                    current_template["enabled_at_utc"]
                    if current_template is not None
                    else ""
                )
                if (
                    current_template is None
                    or not bool(current_template["enabled"])
                    or int(current_template["current_version"])
                    != normalized_version
                    or not enabled_at
                    or _utc_datetime(scheduled)
                    < _utc_datetime(enabled_at, "template enabled time")
                ):
                    raise TTAutoPostStoreError(
                        "tt_auto_template_not_enabled_for_slot",
                        "template is not enabled for this schedule slot",
                        409,
                    )
            if generation_id is not None:
                generation = conn.execute(
                    "SELECT status FROM tt_auto_metric_generation WHERE id=?",
                    (generation_id,),
                ).fetchone()
                if generation is None or str(generation["status"]) != "ready":
                    raise TTAutoPostStoreError("tt_auto_metric_generation_not_ready", "metric generation is not ready", 409)
            existing = conn.execute("SELECT * FROM tt_auto_run WHERE run_key=?", (clean_key,)).fetchone()
            immutable = (
                normalized_template_id,
                normalized_version,
                str(version_row["config_sha256"]),
                clean_trigger,
                scheduled,
                normalized_date,
                normalized_publish_time,
                generation_id,
                blacklist_json,
            )
            if existing is not None:
                existing_facts = (
                    int(existing["template_id"]),
                    int(existing["template_version"]),
                    str(existing["config_sha256"]),
                    str(existing["trigger_type"]),
                    str(existing["scheduled_at_utc"]),
                    str(existing["shanghai_date"]),
                    str(existing["publish_time"]),
                    int(existing["metric_generation_id"]) if existing["metric_generation_id"] is not None else None,
                    str(existing["blacklist_snapshot_json"]),
                )
                if existing_facts != immutable:
                    raise TTAutoPostStoreError("tt_auto_run_idempotency_conflict", "run key belongs to different facts", 409)
                return _run_from_row(existing)
            cursor = conn.execute(
                """
                INSERT INTO tt_auto_run(
                    run_key,template_id,template_version,config_sha256,
                    trigger_type,scheduled_at_utc,shanghai_date,publish_time,
                    metric_generation_id,blacklist_snapshot_json,status,
                    created_by_user_id,created_by_name,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,'queued',?,?,?,?)
                """,
                (
                    clean_key,
                    normalized_template_id,
                    normalized_version,
                    str(version_row["config_sha256"]),
                    clean_trigger,
                    scheduled,
                    normalized_date,
                    normalized_publish_time,
                    generation_id,
                    blacklist_json,
                    actor.user_id,
                    actor.name,
                    now,
                    now,
                ),
            )
            run_id = int(cursor.lastrowid)
            self._event(
                conn,
                run_id=run_id,
                event_type="run_created",
                created_at=now,
                to_status="queued",
                details={"trigger_type": clean_trigger, "template_id": normalized_template_id, "template_version": normalized_version},
            )
            row = conn.execute("SELECT * FROM tt_auto_run WHERE id=?", (run_id,)).fetchone()
        assert row is not None
        return _run_from_row(row)

    def get_run(self, run_id: Any) -> RunRecord:
        normalized_id = _positive_int(run_id, "run id")
        with self._reader() as conn:
            row = conn.execute("SELECT * FROM tt_auto_run WHERE id=?", (normalized_id,)).fetchone()
        if row is None:
            raise TTAutoPostStoreError("tt_auto_run_not_found", "run was not found", 404)
        return _run_from_row(row)

    def get_run_by_key(self, run_key: Any) -> Optional[RunRecord]:
        clean_key = _bounded_text(run_key, "run key", 255)
        with self._reader() as conn:
            row = conn.execute(
                "SELECT * FROM tt_auto_run WHERE run_key=?", (clean_key,)
            ).fetchone()
        return None if row is None else _run_from_row(row)

    def list_runs(
        self,
        *,
        template_id: Optional[Any] = None,
        trigger_type: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[RunRecord]:
        clauses: List[str] = []
        params: List[Any] = []
        if template_id is not None:
            clauses.append("template_id=?")
            params.append(_positive_int(template_id, "template id"))
        if trigger_type is not None:
            if trigger_type not in TRIGGER_TYPES:
                raise TTAutoPostStoreError("tt_auto_trigger_invalid", "trigger type is invalid", 400)
            clauses.append("trigger_type=?")
            params.append(trigger_type)
        if status is not None:
            if status not in RUN_STATUSES:
                raise TTAutoPostStoreError("tt_auto_run_status_invalid", "run status is invalid", 400)
            clauses.append("status=?")
            params.append(status)
        sql = "SELECT * FROM tt_auto_run"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY scheduled_at_utc DESC,id DESC"
        with self._reader() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [_run_from_row(row) for row in rows]

    def set_run_status(
        self,
        run_id: Any,
        to_status: Any,
        *,
        expected_statuses: Optional[Iterable[str]] = None,
        error_code: Any = "",
        error_message: Any = "",
    ) -> RunRecord:
        normalized_id = _positive_int(run_id, "run id")
        target = str(to_status or "").strip()
        if target not in RUN_STATUSES:
            raise TTAutoPostStoreError("tt_auto_run_status_invalid", "run status is invalid", 400)
        expected = None if expected_statuses is None else {str(value) for value in expected_statuses}
        if expected is not None and not expected.issubset(RUN_STATUSES):
            raise TTAutoPostStoreError("tt_auto_run_status_invalid", "expected run status is invalid", 400)
        now = self._now_iso()
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM tt_auto_run WHERE id=?", (normalized_id,)).fetchone()
            if row is None:
                raise TTAutoPostStoreError("tt_auto_run_not_found", "run was not found", 404)
            current = str(row["status"])
            if expected is not None and current not in expected:
                raise TTAutoPostStoreError("tt_auto_run_status_conflict", "run status changed", 409)
            started = str(row["started_at_utc"] or "") or (now if target == "running" else "")
            finished = str(row["finished_at_utc"] or "") or (now if target in {"completed", "partial_failed", "failed", "canceled"} else "")
            conn.execute(
                """
                UPDATE tt_auto_run
                SET status=?,error_code=?,error_message=?,updated_at=?,
                    started_at_utc=?,finished_at_utc=?
                WHERE id=?
                """,
                (
                    target,
                    _bounded_text(error_code, "error code", 128, allow_empty=True),
                    _bounded_text(error_message, "error message", MAX_TEXT_CHARS, allow_empty=True),
                    now,
                    started,
                    finished,
                    normalized_id,
                ),
            )
            self._event(
                conn,
                run_id=normalized_id,
                event_type="run_status_changed",
                created_at=now,
                from_status=current,
                to_status=target,
            )
            result = conn.execute("SELECT * FROM tt_auto_run WHERE id=?", (normalized_id,)).fetchone()
        assert result is not None
        return _run_from_row(result)

    def create_task(
        self,
        *,
        run_id: Any,
        account_id: Any,
        account_username: Any = "",
        account_display_name: Any = "",
        drama_language: Any,
        account_settings: Mapping[str, Any],
        account_setting_version: Any = 0,
    ) -> TaskRecord:
        normalized_run_id = _positive_int(run_id, "run id")
        clean_account_id = _bounded_text(account_id, "account id", MAX_ACCOUNT_ID_CHARS)
        clean_account_username = _bounded_text(
            account_username,
            "account username",
            MAX_ACCOUNT_ID_CHARS,
            allow_empty=True,
        )
        clean_account_display_name = _bounded_text(
            account_display_name,
            "account display name",
            512,
            allow_empty=True,
        )
        clean_language = _bounded_text(drama_language, "drama language", 32).lower()
        settings_json, settings_sha = _canonical_json(account_settings, "account settings")
        setting_version = _nonnegative_int(account_setting_version, "account setting version")
        now = self._now_iso()
        with self._transaction() as conn:
            run = conn.execute("SELECT * FROM tt_auto_run WHERE id=?", (normalized_run_id,)).fetchone()
            if run is None:
                raise TTAutoPostStoreError("tt_auto_run_not_found", "run was not found", 404)
            existing = conn.execute(
                "SELECT * FROM tt_auto_task WHERE run_id=? AND account_id=?",
                (normalized_run_id, clean_account_id),
            ).fetchone()
            priority = 0 if str(run["trigger_type"]) == "manual" else 1
            if existing is not None:
                existing_settings_sha = hashlib.sha256(str(existing["account_settings_json"]).encode("utf-8")).hexdigest()
                if (
                    str(existing["drama_language"]) != clean_language
                    or str(existing["account_username"] or "")
                    != clean_account_username
                    or str(existing["account_display_name"] or "")
                    != clean_account_display_name
                    or int(existing["account_setting_version"]) != setting_version
                    or not secrets.compare_digest(existing_settings_sha, settings_sha)
                ):
                    raise TTAutoPostStoreError("tt_auto_task_idempotency_conflict", "run/account task belongs to different facts", 409)
                return _task_from_row(existing)
            cursor = conn.execute(
                """
                INSERT INTO tt_auto_task(
                    run_id,template_id,template_version,account_id,
                    account_username,account_display_name,
                    trigger_priority,scheduled_at_utc,status,drama_language,
                    account_setting_version,account_settings_json,
                    created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,'pending',?,?,?,?,?)
                """,
                (
                    normalized_run_id,
                    int(run["template_id"]),
                    int(run["template_version"]),
                    clean_account_id,
                    clean_account_username,
                    clean_account_display_name,
                    priority,
                    str(run["scheduled_at_utc"]),
                    clean_language,
                    setting_version,
                    settings_json,
                    now,
                    now,
                ),
            )
            task_id = int(cursor.lastrowid)
            self._event(
                conn,
                run_id=normalized_run_id,
                task_id=task_id,
                event_type="task_created",
                created_at=now,
                to_status="pending",
                details={"account_id": clean_account_id, "trigger_priority": priority},
            )
            row = conn.execute("SELECT * FROM tt_auto_task WHERE id=?", (task_id,)).fetchone()
        assert row is not None
        return _task_from_row(row)

    def get_task(self, task_id: Any) -> TaskRecord:
        normalized_id = _positive_int(task_id, "task id")
        with self._reader() as conn:
            row = conn.execute("SELECT * FROM tt_auto_task WHERE id=?", (normalized_id,)).fetchone()
        if row is None:
            raise TTAutoPostStoreError("tt_auto_task_not_found", "task was not found", 404)
        return _task_from_row(row)

    def list_tasks(
        self,
        *,
        run_id: Optional[Any] = None,
        account_id: Optional[Any] = None,
        status: Optional[str] = None,
    ) -> List[TaskRecord]:
        clauses: List[str] = []
        params: List[Any] = []
        if run_id is not None:
            clauses.append("run_id=?")
            params.append(_positive_int(run_id, "run id"))
        if account_id is not None:
            clauses.append("account_id=?")
            params.append(_bounded_text(account_id, "account id", MAX_ACCOUNT_ID_CHARS))
        if status is not None:
            if status not in TASK_STATUSES:
                raise TTAutoPostStoreError("tt_auto_task_status_invalid", "task status is invalid", 400)
            clauses.append("status=?")
            params.append(status)
        sql = "SELECT * FROM tt_auto_task"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id"
        with self._reader() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [_task_from_row(row) for row in rows]

    def list_publish_logs(
        self,
        *,
        trigger_type: str = "",
        account_id: str = "",
        template_id: Optional[Any] = None,
        material_id: str = "",
        content_id: str = "",
        status_group: str = "",
        from_utc: str = "",
        to_utc: str = "",
        limit: int = 200,
    ) -> Dict[str, Any]:
        """Return a bounded task-level projection for the unified log page."""

        if trigger_type and trigger_type not in TRIGGER_TYPES:
            raise TTAutoPostStoreError(
                "tt_auto_publish_log_trigger_invalid",
                "publish log trigger type is invalid",
                400,
            )
        if status_group and status_group not in PUBLISH_LOG_STATUS_GROUPS:
            raise TTAutoPostStoreError(
                "tt_auto_publish_log_status_invalid",
                "publish log status is invalid",
                400,
            )
        try:
            bounded_limit = int(limit)
        except (TypeError, ValueError, OverflowError):
            bounded_limit = 0
        if not 1 <= bounded_limit <= 10_200:
            raise TTAutoPostStoreError(
                "tt_auto_publish_log_limit_invalid",
                "publish log limit is invalid",
                400,
            )

        clauses: List[str] = []
        params: List[Any] = []
        if trigger_type:
            clauses.append("t.trigger_type=?")
            params.append(trigger_type)
        if account_id:
            clauses.append("t.account_id=?")
            params.append(_bounded_text(account_id, "account id", MAX_ACCOUNT_ID_CHARS))
        if template_id is not None:
            clauses.append("t.template_id=?")
            params.append(_positive_int(template_id, "template id"))
        if material_id:
            clauses.append("t.material_id=?")
            params.append(_bounded_text(material_id, "material id", 128))
        if content_id:
            clauses.append("t.content_id=?")
            params.append(_bounded_text(content_id, "content id", 128))
        if status_group:
            clauses.append("status_group=?")
            params.append(status_group)
        if from_utc:
            clauses.append("t.scheduled_at_utc>=?")
            params.append(str(from_utc))
        if to_utc:
            clauses.append("t.scheduled_at_utc<?")
            params.append(str(to_utc))
        where_sql = " WHERE " + " AND ".join(clauses) if clauses else ""
        cte = """
            WITH classified AS (
                SELECT
                    t.*,
                    r.trigger_type,
                    r.publish_time,
                    r.status AS run_status,
                    r.created_at AS run_created_at,
                    p.name AS template_name,
                    CASE
                        WHEN t.unknown_outcome=1 OR t.status='unknown'
                            THEN 'needs_review'
                        WHEN t.status='pending' THEN 'scheduled'
                        WHEN t.status IN (
                            'selecting','reserved','preparing','retry_wait',
                            'ready','publishing','reconciling'
                        ) THEN 'processing'
                        WHEN t.status='published' THEN 'published'
                        WHEN t.status='failed' THEN 'failed'
                        WHEN t.status='canceled' THEN 'canceled'
                        WHEN t.status IN ('no_candidate','skipped')
                            THEN 'no_candidate'
                        ELSE 'other'
                    END AS status_group
                FROM tt_auto_task t
                JOIN tt_auto_run r ON r.id=t.run_id
                JOIN tt_auto_template p ON p.id=t.template_id
            )
        """
        with self._reader() as conn:
            conn.execute("BEGIN")
            summary_row = conn.execute(
                cte
                + """
                    SELECT COUNT(*) AS total,
                           COALESCE(SUM(status_group='scheduled'),0) AS scheduled,
                           COALESCE(SUM(status_group='processing'),0) AS processing,
                           COALESCE(SUM(status_group='published'),0) AS published,
                           COALESCE(SUM(status_group='needs_review'),0) AS needs_review,
                           COALESCE(SUM(status_group='failed'),0) AS failed,
                           COALESCE(SUM(status_group='canceled'),0) AS canceled,
                           COALESCE(SUM(status_group='no_candidate'),0) AS no_candidate,
                           0 AS hold
                    FROM classified t
                """
                + where_sql,
                tuple(params),
            ).fetchone()
            rows = conn.execute(
                cte
                + "SELECT * FROM classified t"
                + where_sql
                + " ORDER BY scheduled_at_utc DESC,id DESC LIMIT ?",
                tuple([*params, bounded_limit]),
            ).fetchall()

        items: List[Dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            task_id = int(item.pop("id"))
            try:
                selection = _json_object(item.pop("selection_json"))
                settings = _json_object(item.pop("account_settings_json"))
            except TTAutoPostStoreError:
                raise
            drama = selection.get("drama") if isinstance(selection.get("drama"), Mapping) else {}
            material = selection.get("material") if isinstance(selection.get("material"), Mapping) else {}
            item.update(
                {
                    "publish_source": "auto_template",
                    "source_task_type": "auto_task",
                    "task_id": task_id,
                    "task_key": "auto_template:auto_task:%s" % task_id,
                    "task_at_utc": str(item.get("scheduled_at_utc") or ""),
                    "source_account_id": str(item.get("account_id") or ""),
                    "drama_name": str(drama.get("name") or ""),
                    "material_name": str(material.get("material_name") or ""),
                    "material_language": str(
                        material.get("language")
                        or drama.get("language")
                        or item.get("drama_language")
                        or ""
                    ),
                    "code": "",
                    "privacy_level": str(settings.get("privacy_level") or ""),
                    "allow_comment": bool(settings.get("allow_comment")),
                    "allow_duet": bool(settings.get("allow_duet")),
                    "allow_stitch": bool(settings.get("allow_stitch")),
                    "brand_content_toggle": bool(
                        settings.get("brand_content_toggle")
                    ),
                    "brand_organic_toggle": bool(
                        settings.get("brand_organic_toggle")
                    ),
                    "is_aigc": bool(settings.get("is_aigc")),
                    "selection": selection,
                    "unknown_outcome": bool(item.get("unknown_outcome")),
                }
            )
            for key in (
                "source_media_url",
                "prepared_media_url",
                "claim_token",
                "claim_worker",
                "lease_expires_at_utc",
            ):
                item.pop(key, None)
            items.append(item)
        summary = dict(summary_row or {})
        return {
            "items": items,
            "total": int(summary.get("total") or 0),
            "summary": {
                key: int(summary.get(key) or 0)
                for key in (
                    "total",
                    "scheduled",
                    "processing",
                    "published",
                    "needs_review",
                    "failed",
                    "canceled",
                    "no_candidate",
                    "hold",
                )
            },
        }

    def claim_next_pending_task(
        self,
        *,
        worker_id: Any,
        lease_seconds: Any,
        now: Optional[Any] = None,
    ) -> Optional[TaskClaim]:
        worker = _bounded_text(worker_id, "worker id", 128)
        seconds = _positive_int(lease_seconds, "lease seconds")
        if seconds > 10800:
            raise TTAutoPostStoreError("tt_auto_lease_invalid", "lease seconds is too large", 400)
        current_dt = self._now() if now is None else _utc_datetime(now)
        current = current_dt.isoformat(timespec="seconds")
        lease_expires = (current_dt + timedelta(seconds=seconds)).isoformat(timespec="seconds")
        placeholders = ",".join("?" for _ in ACTIVE_ACCOUNT_TASK_STATUSES)
        with self._transaction() as conn:
            row = conn.execute(
                """
                SELECT t.*
                FROM tt_auto_task t
                WHERE t.status='pending'
                  AND t.scheduled_at_utc<=?
                  AND NOT EXISTS (
                      SELECT 1 FROM tt_auto_task active
                      WHERE active.account_id=t.account_id
                        AND active.id<>t.id
                        AND active.status IN (%s)
                  )
                ORDER BY
                    t.trigger_priority ASC,
                    t.scheduled_at_utc ASC,
                    t.template_id ASC,
                    t.id ASC
                LIMIT 1
                """ % placeholders,
                (current, *sorted(ACTIVE_ACCOUNT_TASK_STATUSES)),
            ).fetchone()
            if row is None:
                return None
            token = secrets.token_urlsafe(32)
            task_id = int(row["id"])
            conn.execute(
                """
                UPDATE tt_auto_task
                SET status='selecting',claim_phase='selection',claim_worker=?,
                    claim_token=?,lease_expires_at_utc=?,updated_at=?,
                    selected_at_utc=CASE WHEN selected_at_utc='' THEN ? ELSE selected_at_utc END
                WHERE id=? AND status='pending'
                """,
                (worker, token, lease_expires, current, current, task_id),
            )
            self._event(
                conn,
                run_id=int(row["run_id"]),
                task_id=task_id,
                event_type="task_claimed_for_selection",
                created_at=current,
                from_status="pending",
                to_status="selecting",
                details={"worker_id": worker, "lease_expires_at_utc": lease_expires},
            )
            claimed = conn.execute("SELECT * FROM tt_auto_task WHERE id=?", (task_id,)).fetchone()
        assert claimed is not None
        return TaskClaim(
            _task_from_row(claimed),
            token,
            claim_phase="selection",
            claimed_from_status="pending",
        )

    def claim_next_executable_task(
        self,
        *,
        worker_id: Any,
        lease_seconds: Any,
        now: Optional[Any] = None,
    ) -> Optional[TaskClaim]:
        """Claim the next safe phase, including expired-crash recovery.

        An expired ``publishing`` task is converted to ``unknown`` and claimed
        only for reconciliation.  It can therefore never be initialized again.
        Active work for one account remains serialized inside the same
        ``BEGIN IMMEDIATE`` transaction.
        """

        worker = _bounded_text(worker_id, "worker id", 128)
        seconds = _positive_int(lease_seconds, "lease seconds")
        if seconds > 10800:
            raise TTAutoPostStoreError("tt_auto_lease_invalid", "lease seconds is too large", 400)
        current_dt = self._now() if now is None else _utc_datetime(now)
        current = current_dt.isoformat(timespec="seconds")
        lease_expires = (current_dt + timedelta(seconds=seconds)).isoformat(timespec="seconds")
        active_placeholders = ",".join("?" for _ in ACTIVE_ACCOUNT_TASK_STATUSES)
        with self._transaction() as conn:
            candidates = conn.execute(
                """
                SELECT * FROM tt_auto_task
                WHERE
                    (status='pending' AND scheduled_at_utc<=?)
                    OR (status='selecting' AND (lease_expires_at_utc='' OR lease_expires_at_utc<=?))
                    OR (
                        status='reserved'
                        AND (claim_token='' OR lease_expires_at_utc='' OR lease_expires_at_utc<=?)
                    )
                    OR (status='preparing' AND (lease_expires_at_utc='' OR lease_expires_at_utc<=?))
                    OR (
                        status='retry_wait'
                        AND (next_attempt_at_utc='' OR next_attempt_at_utc<=?)
                        AND (lease_expires_at_utc='' OR lease_expires_at_utc<=?)
                    )
                    OR (status='ready' AND (lease_expires_at_utc='' OR lease_expires_at_utc<=?))
                    OR (
                        status IN ('publishing','reconciling','unknown')
                        AND (lease_expires_at_utc='' OR lease_expires_at_utc<=?)
                    )
                ORDER BY
                    CASE
                        WHEN status IN ('publishing','reconciling','unknown') THEN 0
                        WHEN status='ready' THEN 1
                        WHEN status IN ('reserved','preparing','retry_wait') THEN 2
                        ELSE 3
                    END,
                    trigger_priority ASC,
                    scheduled_at_utc ASC,
                    template_id ASC,
                    id ASC
                LIMIT 500
                """,
                (current, current, current, current, current, current, current, current),
            ).fetchall()
            selected: Optional[sqlite3.Row] = None
            for candidate in candidates:
                candidate_id = int(candidate["id"])
                candidate_status = str(candidate["status"])
                others = conn.execute(
                    """
                    SELECT id,status,scheduled_at_utc,claim_token,lease_expires_at_utc
                    FROM tt_auto_task
                    WHERE account_id=? AND id<>? AND status IN (%s)
                    ORDER BY scheduled_at_utc,id
                    """ % active_placeholders,
                    (
                        str(candidate["account_id"]),
                        candidate_id,
                        *sorted(ACTIVE_ACCOUNT_TASK_STATUSES),
                    ),
                ).fetchall()
                if candidate_status == "pending" and others:
                    continue
                if candidate_status in ACTIVE_ACCOUNT_TASK_STATUSES and others:
                    if any(
                        str(other["claim_token"] or "")
                        and str(other["lease_expires_at_utc"] or "") > current
                        for other in others
                    ):
                        continue
                    ordering = [
                        (str(candidate["scheduled_at_utc"]), candidate_id)
                    ] + [
                        (str(other["scheduled_at_utc"]), int(other["id"]))
                        for other in others
                    ]
                    if min(ordering) != (str(candidate["scheduled_at_utc"]), candidate_id):
                        continue
                selected = candidate
                break
            if selected is None:
                return None

            task_id = int(selected["id"])
            previous_status = str(selected["status"])
            previous_phase = str(selected["claim_phase"] or "")
            if previous_status in {"pending", "selecting"}:
                phase = "selection"
                target_status = "selecting"
            elif previous_status in {"reserved", "preparing"}:
                phase = "prepare"
                target_status = "preparing"
            elif previous_status == "retry_wait":
                phase = previous_phase
                if phase not in {"selection", "prepare", "publish", "reconcile"}:
                    phase = (
                        "publish"
                        if str(selected["prepared_media_url"] or "")
                        else "prepare"
                        if str(selected["material_id"] or "")
                        else "selection"
                    )
                target_status = {
                    "selection": "selecting",
                    "prepare": "preparing",
                    "publish": "publishing",
                    "reconcile": "reconciling",
                }[phase]
            elif previous_status == "ready":
                phase = "publish"
                target_status = "publishing"
            else:
                phase = "reconcile"
                target_status = "unknown" if previous_status in {"publishing", "unknown"} else "reconciling"

            token = secrets.token_urlsafe(32)
            assignments = [
                "status=?",
                "claim_phase=?",
                "claim_worker=?",
                "claim_token=?",
                "lease_expires_at_utc=?",
                "updated_at=?",
            ]
            params: List[Any] = [
                target_status,
                phase,
                worker,
                token,
                lease_expires,
                current,
            ]
            if phase == "selection":
                assignments.append(
                    "selected_at_utc=CASE WHEN selected_at_utc='' THEN ? ELSE selected_at_utc END"
                )
                params.append(current)
            elif phase == "prepare":
                assignments.append("preparation_attempt_count=preparation_attempt_count+1")
            elif phase == "publish":
                assignments.append("publish_attempt_count=publish_attempt_count+1")
            if previous_status == "publishing":
                assignments.append("unknown_outcome=1")
                assignments.append("error_code='tt_auto_publish_outcome_unknown'")
                assignments.append(
                    "error_message='publish worker lease expired; reconcile without reinitializing'"
                )
            params.append(task_id)
            conn.execute(
                "UPDATE tt_auto_task SET %s WHERE id=?" % ",".join(assignments),
                tuple(params),
            )
            self._event(
                conn,
                run_id=int(selected["run_id"]),
                task_id=task_id,
                event_type="task_claimed_%s" % phase,
                created_at=current,
                from_status=previous_status,
                to_status=target_status,
                details={
                    "worker_id": worker,
                    "claim_phase": phase,
                    "lease_expires_at_utc": lease_expires,
                    "recovered": previous_status not in {"pending", "reserved", "ready", "retry_wait"},
                },
            )
            claimed = conn.execute("SELECT * FROM tt_auto_task WHERE id=?", (task_id,)).fetchone()
        assert claimed is not None
        return TaskClaim(
            _task_from_row(claimed),
            token,
            claim_phase=phase,
            claimed_from_status=previous_status,
        )

    def renew_task_claim(
        self,
        task_id: Any,
        claim_token: Any,
        *,
        lease_seconds: Any,
        now: Optional[Any] = None,
    ) -> TaskRecord:
        normalized_id = _positive_int(task_id, "task id")
        token = _bounded_text(claim_token, "claim token", 512)
        seconds = _positive_int(lease_seconds, "lease seconds")
        if seconds > 10800:
            raise TTAutoPostStoreError("tt_auto_lease_invalid", "lease seconds is too large", 400)
        current_dt = self._now() if now is None else _utc_datetime(now)
        current = current_dt.isoformat(timespec="seconds")
        lease_expires = (current_dt + timedelta(seconds=seconds)).isoformat(timespec="seconds")
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM tt_auto_task WHERE id=?", (normalized_id,)).fetchone()
            if row is None:
                raise TTAutoPostStoreError("tt_auto_task_not_found", "task was not found", 404)
            if (
                not secrets.compare_digest(str(row["claim_token"] or ""), token)
                or not str(row["claim_phase"] or "")
                or (str(row["lease_expires_at_utc"] or "") and str(row["lease_expires_at_utc"]) < current)
            ):
                raise TTAutoPostStoreError("tt_auto_task_claim_conflict", "task claim is no longer valid", 409)
            conn.execute(
                "UPDATE tt_auto_task SET lease_expires_at_utc=?,updated_at=? WHERE id=?",
                (lease_expires, current, normalized_id),
            )
            result = conn.execute("SELECT * FROM tt_auto_task WHERE id=?", (normalized_id,)).fetchone()
        assert result is not None
        return _task_from_row(result)

    def release_task_claim(
        self,
        task_id: Any,
        claim_token: Any,
        *,
        message: Any = "",
    ) -> TaskRecord:
        normalized_id = _positive_int(task_id, "task id")
        token = _bounded_text(claim_token, "claim token", 512)
        now = self._now_iso()
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM tt_auto_task WHERE id=?", (normalized_id,)).fetchone()
            if row is None:
                raise TTAutoPostStoreError("tt_auto_task_not_found", "task was not found", 404)
            if not secrets.compare_digest(str(row["claim_token"] or ""), token):
                raise TTAutoPostStoreError("tt_auto_task_claim_conflict", "task claim is no longer valid", 409)
            conn.execute(
                """
                UPDATE tt_auto_task
                SET claim_worker='',claim_token='',lease_expires_at_utc='',updated_at=?
                WHERE id=?
                """,
                (now, normalized_id),
            )
            self._event(
                conn,
                run_id=int(row["run_id"]),
                task_id=normalized_id,
                event_type="task_claim_released",
                created_at=now,
                from_status=str(row["status"]),
                to_status=str(row["status"]),
                message=message,
                details={"claim_phase": str(row["claim_phase"] or "")},
            )
            result = conn.execute("SELECT * FROM tt_auto_task WHERE id=?", (normalized_id,)).fetchone()
        assert result is not None
        return _task_from_row(result)

    def reserve_material(
        self,
        task_id: Any,
        *,
        run_id: Optional[Any] = None,
        template_id: Optional[Any] = None,
        template_version: Optional[Any] = None,
        account_id: Optional[Any] = None,
        material_id: Any,
        content_id: Any,
        series_code: Any = "",
        selection: Optional[Mapping[str, Any]] = None,
        selection_snapshot: Optional[Mapping[str, Any]] = None,
        reserved_at_utc: Optional[Any] = None,
        cooldown_since_utc: Optional[Any] = None,
        claim_token: Optional[Any] = None,
    ) -> MaterialReservation:
        normalized_task_id = _positive_int(task_id, "task id")
        clean_material_id = _bounded_text(material_id, "material id", MAX_IDENTITY_CHARS)
        clean_content_id = _bounded_text(content_id, "content id", MAX_IDENTITY_CHARS)
        clean_series_code = _bounded_text(series_code, "series code", MAX_IDENTITY_CHARS, allow_empty=True)
        if selection is not None and selection_snapshot is not None:
            raise TTAutoPostStoreError(
                "tt_auto_selection_invalid",
                "selection and selection_snapshot cannot both be supplied",
                400,
            )
        selection_json, _ = _canonical_json(
            selection if selection is not None else selection_snapshot or {},
            "selection facts",
        )
        supplied_token = None if claim_token is None else _bounded_text(claim_token, "claim token", 512)
        now = self._now_iso() if reserved_at_utc is None else _utc_iso(reserved_at_utc, "reserved time")
        with self._transaction() as conn:
            task = conn.execute("SELECT * FROM tt_auto_task WHERE id=?", (normalized_task_id,)).fetchone()
            if task is None:
                raise TTAutoPostStoreError("tt_auto_task_not_found", "task was not found", 404)
            supplied_facts = (
                None if run_id is None else _positive_int(run_id, "run id"),
                None if template_id is None else _positive_int(template_id, "template id"),
                None if template_version is None else _positive_int(template_version, "template version"),
                None if account_id is None else _bounded_text(account_id, "account id", MAX_ACCOUNT_ID_CHARS),
            )
            actual_facts = (
                int(task["run_id"]),
                int(task["template_id"]),
                int(task["template_version"]),
                str(task["account_id"]),
            )
            if any(
                supplied is not None and supplied != actual
                for supplied, actual in zip(supplied_facts, actual_facts)
            ):
                raise TTAutoPostStoreError(
                    "tt_auto_task_identity_conflict",
                    "material reservation facts do not match the task",
                    409,
                )
            if supplied_token is not None and not secrets.compare_digest(str(task["claim_token"] or ""), supplied_token):
                raise TTAutoPostStoreError("tt_auto_task_claim_conflict", "task claim token is invalid", 409)
            if supplied_token is None and str(task["claim_token"] or ""):
                raise TTAutoPostStoreError(
                    "tt_auto_task_claim_required",
                    "the active task claim token is required for reservation",
                    409,
                )
            existing_material = str(task["material_id"] or "")
            if existing_material:
                if not secrets.compare_digest(existing_material, clean_material_id) or str(task["content_id"]) != clean_content_id:
                    raise TTAutoPostStoreError("tt_auto_task_material_conflict", "task already reserved another material", 409)
                ledger = conn.execute("SELECT * FROM tt_auto_material_ledger WHERE task_id=?", (normalized_task_id,)).fetchone()
                if ledger is None:
                    raise TTAutoPostStoreError("tt_auto_material_ledger_inconsistent", "material reservation ledger is incomplete", 500)
                return MaterialReservation(
                    material_id=str(ledger["material_id"]),
                    task_id=int(ledger["task_id"]),
                    run_id=int(ledger["run_id"]),
                    template_id=int(ledger["template_id"]),
                    content_id=str(ledger["content_id"]),
                    reserved_at_utc=str(ledger["reserved_at_utc"]),
                )
            if str(task["status"]) not in {"pending", "selecting"}:
                raise TTAutoPostStoreError("tt_auto_task_not_selectable", "task cannot reserve a material in its current status", 409)
            if cooldown_since_utc is not None:
                cooldown_start = _utc_iso(cooldown_since_utc, "cooldown start")
                cooldown_hit = conn.execute(
                    """
                    SELECT 1 FROM tt_auto_material_ledger
                    WHERE template_id=? AND content_id=? AND reserved_at_utc>=?
                    LIMIT 1
                    """,
                    (int(task["template_id"]), clean_content_id, cooldown_start),
                ).fetchone()
                if cooldown_hit is not None:
                    raise TTAutoPostStoreError(
                        "tt_auto_drama_in_cooldown",
                        "drama is still in this template's cooldown window",
                        409,
                    )
            existing_ledger = conn.execute(
                "SELECT task_id FROM tt_auto_material_ledger WHERE material_id=?",
                (clean_material_id,),
            ).fetchone()
            if existing_ledger is not None:
                raise TTAutoPostStoreError("tt_auto_material_already_reserved", "material has already appeared in the automatic publishing ledger", 409)
            conn.execute(
                """
                INSERT INTO tt_auto_material_ledger(
                    material_id,task_id,run_id,template_id,content_id,
                    reserved_at_utc,last_task_status,updated_at
                ) VALUES(?,?,?,?,? ,?,'reserved',?)
                """,
                (
                    clean_material_id,
                    normalized_task_id,
                    int(task["run_id"]),
                    int(task["template_id"]),
                    clean_content_id,
                    now,
                    now,
                ),
            )
            if supplied_token is None:
                claim_assignments = (
                    "claim_phase='',claim_worker='',claim_token='',"
                    "lease_expires_at_utc=''"
                )
            else:
                claim_assignments = "claim_phase='selection'"
            conn.execute(
                """
                UPDATE tt_auto_task
                SET status='reserved',content_id=?,series_code=?,material_id=?,
                    selection_json=?,reserved_at_utc=?,updated_at=?,%s
                WHERE id=?
                """ % claim_assignments,
                (
                    clean_content_id,
                    clean_series_code,
                    clean_material_id,
                    selection_json,
                    now,
                    now,
                    normalized_task_id,
                ),
            )
            self._event(
                conn,
                run_id=int(task["run_id"]),
                task_id=normalized_task_id,
                event_type="material_reserved",
                created_at=now,
                from_status=str(task["status"]),
                to_status="reserved",
                details={"material_id": clean_material_id, "content_id": clean_content_id},
            )
        return MaterialReservation(
            material_id=clean_material_id,
            task_id=normalized_task_id,
            run_id=int(task["run_id"]),
            template_id=int(task["template_id"]),
            content_id=clean_content_id,
            reserved_at_utc=now,
        )

    def material_is_reserved(self, material_id: Any) -> bool:
        clean_material_id = _bounded_text(material_id, "material id", MAX_IDENTITY_CHARS)
        with self._reader() as conn:
            row = conn.execute("SELECT 1 FROM tt_auto_material_ledger WHERE material_id=?", (clean_material_id,)).fetchone()
        return row is not None

    def reserved_material_ids(self, material_ids: Sequence[Any]) -> Set[str]:
        if isinstance(material_ids, (str, bytes)) or not isinstance(material_ids, Sequence):
            raise TTAutoPostStoreError("tt_auto_material_ids_invalid", "material ids must be a list", 400)
        keys = list(
            dict.fromkeys(
                _bounded_text(value, "material id", MAX_IDENTITY_CHARS)
                for value in material_ids
            )
        )
        if not keys:
            return set()
        result: Set[str] = set()
        with self._reader() as conn:
            for offset in range(0, len(keys), 500):
                batch = keys[offset : offset + 500]
                placeholders = ",".join("?" for _ in batch)
                rows = conn.execute(
                    "SELECT material_id FROM tt_auto_material_ledger WHERE material_id IN (%s)"
                    % placeholders,
                    tuple(batch),
                ).fetchall()
                result.update(str(row["material_id"]) for row in rows)
        return result

    def get_task_reservation(self, task_id: Any) -> Optional[MaterialReservation]:
        normalized_task_id = _positive_int(task_id, "task id")
        with self._reader() as conn:
            row = conn.execute(
                "SELECT * FROM tt_auto_material_ledger WHERE task_id=?",
                (normalized_task_id,),
            ).fetchone()
        if row is None:
            return None
        return MaterialReservation(
            material_id=str(row["material_id"]),
            task_id=int(row["task_id"]),
            run_id=int(row["run_id"]),
            template_id=int(row["template_id"]),
            content_id=str(row["content_id"]),
            reserved_at_utc=str(row["reserved_at_utc"]),
        )

    def last_template_drama_reserved_at(self, template_id: Any, content_id: Any) -> Optional[str]:
        normalized_template_id = _positive_int(template_id, "template id")
        clean_content_id = _bounded_text(content_id, "content id", MAX_IDENTITY_CHARS)
        with self._reader() as conn:
            row = conn.execute(
                """
                SELECT reserved_at_utc
                FROM tt_auto_material_ledger
                WHERE template_id=? AND content_id=?
                ORDER BY reserved_at_utc DESC
                LIMIT 1
                """,
                (normalized_template_id, clean_content_id),
            ).fetchone()
        return None if row is None else str(row["reserved_at_utc"])

    def template_drama_in_cooldown(
        self,
        template_id: Any,
        content_id: Any,
        cooldown_days: Any,
        *,
        now: Optional[Any] = None,
    ) -> bool:
        days = _nonnegative_int(cooldown_days, "cooldown days")
        if days == 0:
            return False
        last = self.last_template_drama_reserved_at(template_id, content_id)
        if not last:
            return False
        current = self._now() if now is None else _utc_datetime(now)
        return _utc_datetime(last) > current - timedelta(days=days)

    def cooldown_content_ids(
        self,
        template_id: Any,
        content_ids: Sequence[Any],
        since_utc: Any,
    ) -> Set[str]:
        """Return dramas reserved by this template at or after ``since_utc``."""

        normalized_template_id = _positive_int(template_id, "template id")
        since = _utc_iso(since_utc, "cooldown start")
        if isinstance(content_ids, (str, bytes)) or not isinstance(content_ids, Sequence):
            raise TTAutoPostStoreError("tt_auto_content_ids_invalid", "content ids must be a list", 400)
        keys = list(
            dict.fromkeys(
                _bounded_text(value, "content id", MAX_IDENTITY_CHARS)
                for value in content_ids
            )
        )
        if not keys:
            return set()
        result: Set[str] = set()
        with self._reader() as conn:
            for offset in range(0, len(keys), 500):
                batch = keys[offset : offset + 500]
                placeholders = ",".join("?" for _ in batch)
                rows = conn.execute(
                    """
                    SELECT DISTINCT content_id
                    FROM tt_auto_material_ledger
                    WHERE template_id=? AND reserved_at_utc>=?
                      AND content_id IN (%s)
                    """ % placeholders,
                    (normalized_template_id, since, *batch),
                ).fetchall()
                result.update(str(row["content_id"]) for row in rows)
        return result

    def transition_task(
        self,
        task_id: Any,
        to_status: Any,
        *,
        expected_statuses: Optional[Iterable[str]] = None,
        claim_token: Optional[Any] = None,
        updates: Optional[Mapping[str, Any]] = None,
        event_type: Any = "task_status_changed",
        message: Any = "",
    ) -> TaskRecord:
        normalized_id = _positive_int(task_id, "task id")
        target = str(to_status or "").strip()
        if target not in TASK_STATUSES:
            raise TTAutoPostStoreError("tt_auto_task_status_invalid", "task status is invalid", 400)
        expected = None if expected_statuses is None else {str(value) for value in expected_statuses}
        if expected is not None and not expected.issubset(TASK_STATUSES):
            raise TTAutoPostStoreError("tt_auto_task_status_invalid", "expected task status is invalid", 400)
        allowed_updates = {
            "gpu_job_id",
            "source_media_url",
            "prepared_media_url",
            "prepared_output_sha256",
            "prepared_output_size",
            "prepared_duration_sec",
            "caption",
            "publish_id",
            "publish_url",
            "unknown_outcome",
            "preparation_attempt_count",
            "publish_attempt_count",
            "next_attempt_at_utc",
            "claim_phase",
            "claim_worker",
            "claim_token",
            "lease_expires_at_utc",
            "error_code",
            "error_message",
        }
        supplied_updates = dict(updates or {})
        if set(supplied_updates) - allowed_updates:
            raise TTAutoPostStoreError("tt_auto_task_update_invalid", "task update contains unsupported fields", 400)
        now = self._now_iso()
        supplied_claim_token = (
            None
            if claim_token is None
            else _bounded_text(claim_token, "claim token", 512)
        )
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM tt_auto_task WHERE id=?", (normalized_id,)).fetchone()
            if row is None:
                raise TTAutoPostStoreError("tt_auto_task_not_found", "task was not found", 404)
            current = str(row["status"])
            if expected is not None and current not in expected:
                raise TTAutoPostStoreError("tt_auto_task_status_conflict", "task status changed", 409)
            stored_claim_token = str(row["claim_token"] or "")
            if stored_claim_token and (
                supplied_claim_token is None
                or not secrets.compare_digest(stored_claim_token, supplied_claim_token)
            ):
                raise TTAutoPostStoreError(
                    "tt_auto_task_claim_conflict",
                    "the active task claim token is required",
                    409,
                )
            for immutable_field in (
                "gpu_job_id",
                "source_media_url",
                "prepared_media_url",
                "prepared_output_sha256",
                "caption",
                "publish_id",
            ):
                stored_value = str(row[immutable_field] or "")
                if immutable_field in supplied_updates and stored_value:
                    requested_value = str(supplied_updates[immutable_field] or "")
                    if not secrets.compare_digest(stored_value, requested_value):
                        raise TTAutoPostStoreError(
                            "tt_auto_task_identity_immutable",
                            "%s cannot change once recorded" % immutable_field,
                            409,
                        )

            stored_publish_id = str(row["publish_id"] or "")
            effective_publish_id = str(
                supplied_updates.get("publish_id", stored_publish_id) or ""
            )
            stored_unknown = bool(row["unknown_outcome"])
            requested_unknown = supplied_updates.get(
                "unknown_outcome", stored_unknown
            )
            effective_unknown = (
                requested_unknown
                if type(requested_unknown) is bool
                else stored_unknown
            )
            protected_publish = bool(
                effective_publish_id
                or effective_unknown
                or current in {"unknown", "reconciling"}
            )
            if stored_unknown and not effective_unknown and not effective_publish_id:
                raise TTAutoPostStoreError(
                    "tt_auto_publish_reconcile_required",
                    "unknown publish outcome cannot be cleared without publish_id",
                    409,
                )
            if protected_publish:
                allowed = {
                    "unknown",
                    "reconciling",
                    "retry_wait",
                    "published",
                    "failed",
                }
                if target not in allowed:
                    raise TTAutoPostStoreError(
                        "tt_auto_publish_reconcile_required",
                        "task with publish evidence may only reconcile",
                        409,
                    )
                if target == "retry_wait" and str(
                    supplied_updates.get("claim_phase") or row["claim_phase"] or ""
                ) != "reconcile":
                    raise TTAutoPostStoreError(
                        "tt_auto_publish_reconcile_required",
                        "publish-evidence retry must remain in reconcile phase",
                        409,
                    )
            assignments = ["status=?", "updated_at=?"]
            params: List[Any] = [target, now]
            integer_fields = {"prepared_output_size", "preparation_attempt_count", "publish_attempt_count"}
            float_fields = {"prepared_duration_sec"}
            bool_fields = {"unknown_outcome"}
            time_fields = {"next_attempt_at_utc", "lease_expires_at_utc"}
            for key, value in supplied_updates.items():
                if key in integer_fields:
                    normalized: Any = _nonnegative_int(value, key)
                elif key in float_fields:
                    try:
                        normalized = float(value)
                    except (TypeError, ValueError, OverflowError):
                        normalized = -1.0
                    if normalized < 0 or normalized != normalized:
                        raise TTAutoPostStoreError("tt_auto_task_update_invalid", "%s is invalid" % key, 400)
                elif key in bool_fields:
                    if type(value) is not bool:
                        raise TTAutoPostStoreError("tt_auto_task_update_invalid", "%s must be a boolean" % key, 400)
                    normalized = int(value)
                elif key in time_fields:
                    normalized = "" if value in (None, "") else _utc_iso(value, key)
                else:
                    limit = MAX_TEXT_CHARS if key in {"caption", "error_message"} else 512
                    normalized = _bounded_text(value, key, limit, allow_empty=True)
                assignments.append("%s=?" % key)
                params.append(normalized)
            if target == "ready":
                assignments.append("prepared_at_utc=CASE WHEN prepared_at_utc='' THEN ? ELSE prepared_at_utc END")
                params.append(now)
            if target == "published":
                assignments.append("published_at_utc=CASE WHEN published_at_utc='' THEN ? ELSE published_at_utc END")
                params.append(now)
            if target in TERMINAL_TASK_STATUSES:
                assignments.append("finished_at_utc=CASE WHEN finished_at_utc='' THEN ? ELSE finished_at_utc END")
                params.append(now)
            if target in TERMINAL_TASK_STATUSES | {
                "ready",
                "retry_wait",
                "unknown",
                "reconciling",
            }:
                assignments.extend(
                    [
                        "claim_worker=''",
                        "claim_token=''",
                        "lease_expires_at_utc=''",
                    ]
                )
            params.append(normalized_id)
            conn.execute("UPDATE tt_auto_task SET %s WHERE id=?" % ",".join(assignments), tuple(params))
            if str(row["material_id"] or ""):
                conn.execute(
                    """
                    UPDATE tt_auto_material_ledger
                    SET last_task_status=?,updated_at=?
                    WHERE task_id=?
                    """,
                    (target, now, normalized_id),
                )
            self._event(
                conn,
                run_id=int(row["run_id"]),
                task_id=normalized_id,
                event_type=event_type,
                created_at=now,
                from_status=current,
                to_status=target,
                message=message,
            )
            result = conn.execute("SELECT * FROM tt_auto_task WHERE id=?", (normalized_id,)).fetchone()
        assert result is not None
        return _task_from_row(result)

    def list_events(
        self,
        *,
        run_id: Optional[Any] = None,
        task_id: Optional[Any] = None,
    ) -> List[Dict[str, Any]]:
        clauses: List[str] = []
        params: List[Any] = []
        if run_id is not None:
            clauses.append("run_id=?")
            params.append(_positive_int(run_id, "run id"))
        if task_id is not None:
            clauses.append("task_id=?")
            params.append(_positive_int(task_id, "task id"))
        sql = "SELECT * FROM tt_auto_event"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id"
        with self._reader() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [
            {
                "id": int(row["id"]),
                "run_id": int(row["run_id"]) if row["run_id"] is not None else None,
                "task_id": int(row["task_id"]) if row["task_id"] is not None else None,
                "event_type": str(row["event_type"]),
                "from_status": str(row["from_status"] or ""),
                "to_status": str(row["to_status"] or ""),
                "message": str(row["message"] or ""),
                "details": _json_object(row["details_json"]),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]

    def create_metric_generation(
        self,
        *,
        generation_key: Any,
        platform: Any,
        metric_date: Any,
        product: Any,
        metadata: Optional[Mapping[str, Any]] = None,
        actor: AuditActor = AuditActor(),
    ) -> MetricGeneration:
        clean_key = _bounded_text(generation_key, "metric generation key", 255)
        normalized_platform = _nonnegative_int(platform, "platform")
        normalized_date = _shanghai_date(metric_date)
        clean_product = _bounded_text(product, "product", 128)
        metadata_json, metadata_sha = _canonical_json(metadata or {}, "metric generation metadata")
        actor = AuditActor.from_values(actor.user_id, actor.name)
        now = self._now_iso()
        with self._transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM tt_auto_metric_generation WHERE generation_key=?",
                (clean_key,),
            ).fetchone()
            if existing is not None:
                existing_sha = hashlib.sha256(str(existing["metadata_json"]).encode("utf-8")).hexdigest()
                if (
                    int(existing["platform"]) != normalized_platform
                    or str(existing["metric_date"]) != normalized_date
                    or str(existing["product"]) != clean_product
                    or not secrets.compare_digest(existing_sha, metadata_sha)
                ):
                    raise TTAutoPostStoreError("tt_auto_metric_generation_conflict", "generation key belongs to different metadata", 409)
                return _metric_generation_from_row(existing)
            cursor = conn.execute(
                """
                INSERT INTO tt_auto_metric_generation(
                    generation_key,platform,metric_date,product,
                    status,row_count,checksum,metadata_json,
                    created_by_user_id,created_by_name,created_at,updated_at
                ) VALUES(?,?,?,?,'building',0,'',?,?,?,?,?)
                """,
                (
                    clean_key,
                    normalized_platform,
                    normalized_date,
                    clean_product,
                    metadata_json,
                    actor.user_id,
                    actor.name,
                    now,
                    now,
                ),
            )
            generation_id = int(cursor.lastrowid)
            self._prune_metric_scope(
                conn,
                normalized_platform,
                normalized_date,
                clean_product,
            )
            row = conn.execute("SELECT * FROM tt_auto_metric_generation WHERE id=?", (generation_id,)).fetchone()
        assert row is not None
        return _metric_generation_from_row(row)

    def upsert_metric_daily(self, generation_id: Any, rows: Iterable[Mapping[str, Any]]) -> int:
        normalized_generation_id = _positive_int(generation_id, "metric generation id")
        with self._transaction() as conn:
            generation = conn.execute("SELECT status FROM tt_auto_metric_generation WHERE id=?", (normalized_generation_id,)).fetchone()
            if generation is None:
                raise TTAutoPostStoreError("tt_auto_metric_generation_not_found", "metric generation was not found", 404)
            if str(generation["status"]) != "building":
                raise TTAutoPostStoreError("tt_auto_metric_generation_closed", "metric generation is no longer writable", 409)
            upsert_sql = """
                INSERT INTO tt_auto_metric_daily(
                    generation_id,content_id,material_id,spend,af_revenue0
                ) VALUES(?,?,?,?,?)
                ON CONFLICT(generation_id,material_id) DO UPDATE SET
                    content_id=excluded.content_id,
                    spend=excluded.spend,
                    af_revenue0=excluded.af_revenue0
                """
            batch: List[Tuple[Any, ...]] = []
            for raw in rows:
                if not isinstance(raw, Mapping):
                    raise TTAutoPostStoreError("tt_auto_metric_row_invalid", "metric row must be an object", 400)
                content_id = _bounded_text(raw.get("content_id"), "content id", MAX_IDENTITY_CHARS)
                material_id = _bounded_text(raw.get("material_id"), "material id", MAX_IDENTITY_CHARS)
                spend = _nonnegative_decimal_text(raw.get("spend"), "spend")
                revenue0 = _nonnegative_decimal_text(
                    raw.get("af_revenue0", raw.get("revenue0", 0)),
                    "af_revenue0",
                )
                batch.append((normalized_generation_id, content_id, material_id, spend, revenue0))
                if len(batch) >= 1_000:
                    conn.executemany(upsert_sql, batch)
                    batch.clear()
            if batch:
                conn.executemany(upsert_sql, batch)
            count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM tt_auto_metric_daily WHERE generation_id=?",
                    (normalized_generation_id,),
                ).fetchone()[0]
            )
            conn.execute(
                "UPDATE tt_auto_metric_generation SET row_count=?,updated_at=? WHERE id=?",
                (count, self._now_iso(), normalized_generation_id),
            )
        return count

    def mark_metric_generation_ready(self, generation_id: Any, *, checksum: Any) -> MetricGeneration:
        normalized_id = _positive_int(generation_id, "metric generation id")
        clean_checksum = _bounded_text(checksum, "metric checksum", 128)
        now = self._now_iso()
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM tt_auto_metric_generation WHERE id=?", (normalized_id,)).fetchone()
            if row is None:
                raise TTAutoPostStoreError("tt_auto_metric_generation_not_found", "metric generation was not found", 404)
            if str(row["status"]) == "ready":
                if secrets.compare_digest(str(row["checksum"]), clean_checksum):
                    return _metric_generation_from_row(row)
                raise TTAutoPostStoreError("tt_auto_metric_generation_conflict", "ready generation checksum differs", 409)
            if str(row["status"]) != "building":
                raise TTAutoPostStoreError("tt_auto_metric_generation_closed", "metric generation cannot become ready", 409)
            count = int(conn.execute("SELECT COUNT(*) FROM tt_auto_metric_daily WHERE generation_id=?", (normalized_id,)).fetchone()[0])
            conn.execute(
                """
                UPDATE tt_auto_metric_generation
                SET status='ready',row_count=?,checksum=?,updated_at=?,ready_at_utc=?
                WHERE id=?
                """,
                (count, clean_checksum, now, now, normalized_id),
            )
            result = conn.execute("SELECT * FROM tt_auto_metric_generation WHERE id=?", (normalized_id,)).fetchone()
        assert result is not None
        return _metric_generation_from_row(result)

    @staticmethod
    def _prune_metric_scope(
        conn: sqlite3.Connection,
        platform: int,
        metric_date: str,
        product: str,
    ) -> int:
        rows = conn.execute(
            """
            SELECT id
            FROM tt_auto_metric_generation
            WHERE platform=? AND metric_date=? AND product=?
            ORDER BY id DESC
            """,
            (int(platform), str(metric_date), str(product)),
        ).fetchall()
        newest = {int(row["id"]) for row in rows[:METRIC_GENERATIONS_TO_KEEP]}
        protected = {
            int(row["generation_id"])
            for row in conn.execute(
                "SELECT generation_id FROM tt_auto_metric_active_pointer"
            ).fetchall()
        }
        protected.update(
            int(row["metric_generation_id"])
            for row in conn.execute(
                """
                SELECT DISTINCT metric_generation_id
                FROM tt_auto_run
                WHERE metric_generation_id IS NOT NULL
                """
            ).fetchall()
        )
        doomed = [
            int(row["id"])
            for row in rows
            if int(row["id"]) not in newest
            and int(row["id"]) not in protected
        ]
        if not doomed:
            return 0
        placeholders = ",".join("?" for _ in doomed)
        conn.execute(
            "DELETE FROM tt_auto_metric_daily WHERE generation_id IN (%s)"
            % placeholders,
            tuple(doomed),
        )
        conn.execute(
            "DELETE FROM tt_auto_metric_generation WHERE id IN (%s)"
            % placeholders,
            tuple(doomed),
        )
        return len(doomed)

    def fail_metric_generation(self, generation_id: Any, *, error_code: Any, error_message: Any) -> MetricGeneration:
        normalized_id = _positive_int(generation_id, "metric generation id")
        now = self._now_iso()
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM tt_auto_metric_generation WHERE id=?", (normalized_id,)).fetchone()
            if row is None:
                raise TTAutoPostStoreError("tt_auto_metric_generation_not_found", "metric generation was not found", 404)
            if str(row["status"]) == "ready":
                raise TTAutoPostStoreError("tt_auto_metric_generation_closed", "ready generation cannot be failed", 409)
            conn.execute(
                """
                UPDATE tt_auto_metric_generation
                SET status='failed',error_code=?,error_message=?,
                    updated_at=?,failed_at_utc=?
                WHERE id=?
                """,
                (
                    _bounded_text(error_code, "error code", 128),
                    _bounded_text(error_message, "error message", MAX_TEXT_CHARS),
                    now,
                    now,
                    normalized_id,
                ),
            )
            result = conn.execute("SELECT * FROM tt_auto_metric_generation WHERE id=?", (normalized_id,)).fetchone()
        assert result is not None
        return _metric_generation_from_row(result)

    def activate_metric_generation(
        self,
        generation_id: Any,
        *,
        actor: AuditActor = AuditActor(),
    ) -> MetricGeneration:
        normalized_id = _positive_int(generation_id, "metric generation id")
        actor = AuditActor.from_values(actor.user_id, actor.name)
        now = self._now_iso()
        with self._transaction() as conn:
            generation = conn.execute("SELECT * FROM tt_auto_metric_generation WHERE id=?", (normalized_id,)).fetchone()
            if generation is None:
                raise TTAutoPostStoreError("tt_auto_metric_generation_not_found", "metric generation was not found", 404)
            if str(generation["status"]) != "ready":
                raise TTAutoPostStoreError("tt_auto_metric_generation_not_ready", "only a ready generation can be activated", 409)
            conn.execute(
                """
                INSERT INTO tt_auto_metric_active_pointer(
                    platform,metric_date,product,generation_id,activated_by_user_id,
                    activated_by_name,activated_at_utc
                ) VALUES(?,?,?,?,?, ?,?)
                ON CONFLICT(platform,metric_date,product) DO UPDATE SET
                    generation_id=excluded.generation_id,
                    activated_by_user_id=excluded.activated_by_user_id,
                    activated_by_name=excluded.activated_by_name,
                    activated_at_utc=excluded.activated_at_utc
                """,
                (
                    int(generation["platform"]),
                    str(generation["metric_date"]),
                    str(generation["product"]),
                    normalized_id,
                    actor.user_id,
                    actor.name,
                    now,
                ),
            )
            self._prune_metric_scope(
                conn,
                int(generation["platform"]),
                str(generation["metric_date"]),
                str(generation["product"]),
            )
            row = conn.execute(
                """
                SELECT g.*,p.activated_at_utc
                FROM tt_auto_metric_generation g
                JOIN tt_auto_metric_active_pointer p ON p.generation_id=g.id
                WHERE p.platform=? AND p.metric_date=? AND p.product=?
                """
                ,
                (
                    int(generation["platform"]),
                    str(generation["metric_date"]),
                    str(generation["product"]),
                ),
            ).fetchone()
        assert row is not None
        return _metric_generation_from_row(row)

    def get_active_metric_generation(
        self,
        *,
        platform: Optional[Any] = None,
        metric_date: Optional[Any] = None,
        product: Optional[Any] = None,
    ) -> Optional[MetricGeneration]:
        clauses: List[str] = []
        params: List[Any] = []
        if platform is not None:
            clauses.append("p.platform=?")
            params.append(_nonnegative_int(platform, "platform"))
        if metric_date is not None:
            clauses.append("p.metric_date=?")
            params.append(_shanghai_date(metric_date))
        if product is not None:
            clauses.append("p.product=?")
            params.append(_bounded_text(product, "product", 128))
        sql = """
            SELECT g.*,p.activated_at_utc
            FROM tt_auto_metric_active_pointer p
            JOIN tt_auto_metric_generation g ON g.id=p.generation_id
        """
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY p.activated_at_utc DESC,p.generation_id DESC LIMIT 1"
        with self._reader() as conn:
            row = conn.execute(sql, tuple(params)).fetchone()
        return None if row is None else _metric_generation_from_row(row)

    def aggregate_metrics(
        self,
        *,
        generation_id: Any,
        platform: Any,
        dimension_type: Any,
        dimension_keys: Sequence[Any],
        start_date: Any,
        end_date: Any,
    ) -> Dict[str, Dict[str, Any]]:
        normalized_generation_id = _positive_int(generation_id, "metric generation id")
        normalized_platform = _nonnegative_int(platform, "platform")
        clean_type = str(dimension_type or "").strip()
        if clean_type not in METRIC_DIMENSION_TYPES:
            raise TTAutoPostStoreError("tt_auto_metric_dimension_invalid", "metric dimension type is invalid", 400)
        start = _shanghai_date(start_date)
        end = _shanghai_date(end_date)
        if start > end:
            raise TTAutoPostStoreError("tt_auto_metric_window_invalid", "metric window is invalid", 400)
        keys = list(dict.fromkeys(_bounded_text(value, "metric dimension key", MAX_IDENTITY_CHARS) for value in dimension_keys))
        if not keys:
            return {}
        totals: Dict[str, Tuple[Decimal, Decimal]] = {}
        dimension_column = "content_id" if clean_type == "drama" else "material_id"
        with self._reader() as conn:
            generation = conn.execute(
                "SELECT platform,metric_date,status FROM tt_auto_metric_generation WHERE id=?",
                (normalized_generation_id,),
            ).fetchone()
            if (
                generation is None
                or int(generation["platform"]) != normalized_platform
                or str(generation["status"]) != "ready"
                or not start <= str(generation["metric_date"]) <= end
            ):
                raise TTAutoPostStoreError(
                    "tt_auto_metric_generation_not_ready",
                    "metric generation does not match the requested window",
                    409,
                )
            for offset in range(0, len(keys), 500):
                batch = keys[offset : offset + 500]
                placeholders = ",".join("?" for _ in batch)
                rows = conn.execute(
                    """
                    SELECT %s AS dimension_key,spend,af_revenue0
                    FROM tt_auto_metric_daily
                    WHERE generation_id=? AND %s IN (%s)
                    """ % (
                        dimension_column,
                        dimension_column,
                        placeholders,
                    ),
                    (normalized_generation_id, *batch),
                ).fetchall()
                for row in rows:
                    key = str(row["dimension_key"])
                    spend, revenue0 = totals.get(key, (Decimal("0"), Decimal("0")))
                    totals[key] = (
                        spend + Decimal(str(row["spend"] or "0")),
                        revenue0 + Decimal(str(row["af_revenue0"] or "0")),
                    )
        result: Dict[str, Dict[str, Any]] = {}
        for key, (spend, revenue0) in totals.items():
            result[key] = {
                "spend": spend,
                "revenue0": revenue0,
                "d0_roas": (revenue0 / spend * Decimal("100")) if spend else None,
            }
        return result

    def record_metric_generation(
        self,
        *,
        platform: Any,
        metric_date: Any,
        product: Any,
        rows: Iterable[Mapping[str, Any]],
        refreshed_at_utc: Any,
        actor: AuditActor = AuditActor(),
    ) -> MetricGeneration:
        """Write one complete inactive day generation and mark it ready.

        Activation is intentionally separate, so a partially written or failed
        generation can never replace the active day snapshot.
        """

        normalized_platform = _nonnegative_int(platform, "platform")
        normalized_date = _shanghai_date(metric_date)
        clean_product = _bounded_text(product, "product", 128)
        refreshed = _utc_iso(refreshed_at_utc, "metric refresh time")
        identity = json.dumps(
            [normalized_platform, normalized_date, clean_product, refreshed],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        generation_key = "tt-auto-metric-v1-" + hashlib.sha256(
            identity.encode("utf-8")
        ).hexdigest()
        generation = self.create_metric_generation(
            generation_key=generation_key,
            platform=normalized_platform,
            metric_date=normalized_date,
            product=clean_product,
            metadata={"refreshed_at_utc": refreshed},
            actor=actor,
        )
        if generation.status == "ready":
            return generation
        self.upsert_metric_daily(generation.id, rows)
        digest = hashlib.sha256()
        with self._reader() as conn:
            stored = conn.execute(
                """
                SELECT content_id,material_id,spend,af_revenue0
                FROM tt_auto_metric_daily
                WHERE generation_id=?
                ORDER BY material_id
                """,
                (generation.id,),
            )
            for row in stored:
                digest.update(
                    json.dumps(
                        [
                            str(row["content_id"]),
                            str(row["material_id"]),
                            str(row["spend"]),
                            str(row["af_revenue0"]),
                        ],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                )
                digest.update(b"\n")
        return self.mark_metric_generation_ready(
            generation.id,
            checksum=digest.hexdigest(),
        )

    def ready_metric_dates(
        self,
        platform: Any,
        dates: Optional[Sequence[Any]] = None,
        *,
        metric_dates: Optional[Sequence[Any]] = None,
        product: Optional[Any] = None,
    ) -> Set[str]:
        normalized_platform = _nonnegative_int(platform, "platform")
        if dates is not None and metric_dates is not None:
            raise TTAutoPostStoreError(
                "tt_auto_metric_dates_invalid",
                "dates and metric_dates cannot both be supplied",
                400,
            )
        dates = dates if dates is not None else metric_dates
        if isinstance(dates, (str, bytes)) or not isinstance(dates, Sequence):
            raise TTAutoPostStoreError("tt_auto_metric_dates_invalid", "metric dates must be a list", 400)
        normalized_dates = list(dict.fromkeys(_shanghai_date(value) for value in dates))
        if not normalized_dates:
            return set()
        clean_product = None if product is None else _bounded_text(product, "product", 128)
        result: Set[str] = set()
        with self._reader() as conn:
            for offset in range(0, len(normalized_dates), 500):
                batch = normalized_dates[offset : offset + 500]
                placeholders = ",".join("?" for _ in batch)
                sql = """
                    SELECT p.metric_date,COUNT(*) AS pointer_count
                    FROM tt_auto_metric_active_pointer p
                    JOIN tt_auto_metric_generation g ON g.id=p.generation_id
                    WHERE p.platform=? AND p.metric_date IN (%s)
                      AND g.status='ready'
                """ % placeholders
                params: List[Any] = [normalized_platform, *batch]
                if clean_product is not None:
                    sql += " AND p.product=?"
                    params.append(clean_product)
                sql += " GROUP BY p.metric_date"
                rows = conn.execute(sql, tuple(params)).fetchall()
                for row in rows:
                    if clean_product is None and int(row["pointer_count"]) != 1:
                        raise TTAutoPostStoreError(
                            "tt_auto_metric_product_ambiguous",
                            "multiple active products exist for a metric date",
                            409,
                        )
                    result.add(str(row["metric_date"]))
        return result

    def iter_ready_metric_rows(
        self,
        platform: Any,
        dates: Optional[Sequence[Any]] = None,
        content_ids: Optional[Sequence[Any]] = None,
        *,
        metric_dates: Optional[Sequence[Any]] = None,
        product: Optional[Any] = None,
    ) -> Iterator[Dict[str, Any]]:
        """Yield active rows only when every requested day is READY."""

        normalized_platform = _nonnegative_int(platform, "platform")
        if dates is not None and metric_dates is not None:
            raise TTAutoPostStoreError(
                "tt_auto_metric_dates_invalid",
                "dates and metric_dates cannot both be supplied",
                400,
            )
        dates = dates if dates is not None else metric_dates
        if isinstance(dates, (str, bytes)) or not isinstance(dates, Sequence):
            raise TTAutoPostStoreError("tt_auto_metric_dates_invalid", "metric dates must be a list", 400)
        normalized_dates = list(dict.fromkeys(_shanghai_date(value) for value in dates))
        ready = self.ready_metric_dates(
            normalized_platform,
            normalized_dates,
            product=product,
        )
        if ready != set(normalized_dates):
            raise TTAutoPostStoreError(
                "tt_auto_metric_window_incomplete",
                "one or more requested metric dates are not ready",
                409,
            )
        clean_content_ids: Optional[List[str]]
        if content_ids is None:
            clean_content_ids = None
        else:
            if isinstance(content_ids, (str, bytes)) or not isinstance(content_ids, Sequence):
                raise TTAutoPostStoreError("tt_auto_content_ids_invalid", "content ids must be a list", 400)
            clean_content_ids = list(
                dict.fromkeys(
                    _bounded_text(value, "content id", MAX_IDENTITY_CHARS)
                    for value in content_ids
                )
            )
            if not clean_content_ids:
                return iter(())
        clean_product = None if product is None else _bounded_text(product, "product", 128)
        items: List[Dict[str, Any]] = []
        with self._reader() as conn:
            date_placeholders = ",".join("?" for _ in normalized_dates)
            sql = """
                SELECT
                    p.metric_date,p.platform,p.product,
                    d.content_id,d.material_id,d.spend,d.af_revenue0
                FROM tt_auto_metric_active_pointer p
                JOIN tt_auto_metric_generation g ON g.id=p.generation_id
                JOIN tt_auto_metric_daily d ON d.generation_id=g.id
                WHERE p.platform=? AND p.metric_date IN (%s)
                  AND g.status='ready'
            """ % date_placeholders
            params: List[Any] = [normalized_platform, *normalized_dates]
            if clean_product is not None:
                sql += " AND p.product=?"
                params.append(clean_product)
            if clean_content_ids is not None:
                content_placeholders = ",".join("?" for _ in clean_content_ids)
                sql += " AND d.content_id IN (%s)" % content_placeholders
                params.extend(clean_content_ids)
            sql += " ORDER BY p.metric_date,d.content_id,d.material_id"
            for row in conn.execute(sql, tuple(params)):
                items.append(
                    {
                        "metric_date": str(row["metric_date"]),
                        "platform": int(row["platform"]),
                        "product": str(row["product"]),
                        "content_id": str(row["content_id"]),
                        "material_id": str(row["material_id"]),
                        "spend": str(row["spend"] or "0"),
                        "af_revenue0": str(row["af_revenue0"] or "0"),
                    }
                )
        return iter(items)


__all__ = [
    "ACTIVE_ACCOUNT_TASK_STATUSES",
    "AuditActor",
    "MaterialReservation",
    "MetricGeneration",
    "RunRecord",
    "TASK_STATUSES",
    "TTAutoPostStore",
    "TTPostAutoStore",
    "TTAutoPostStoreError",
    "TaskClaim",
    "TaskRecord",
    "TemplateSnapshot",
    "ensure_storage",
]


# Prefer the project-facing name while retaining the original spelling for
# callers created during the initial implementation split.
TTPostAutoStore = TTAutoPostStore
