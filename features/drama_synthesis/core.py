"""Durable contracts for the drama-synthesis upgrade.

The existing ``drama_material_job`` row remains the source of truth for the
legacy job state machine.  These tables are additive and contain only the new
immutable recipe, short-link and YouTube ledgers.  Credentials never enter
SQLite or API DTOs.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Union
from urllib.parse import quote, urlencode, urlsplit


RECIPE_VERSION = 1
RECIPE_PROFILE = "drama-random-overlay-h264-720x1280-v1"
RECIPE_CATEGORIES = ("border", "opacity_video", "corners", "tint")
UPLOAD_SCOPES = frozenset(
    {
        "https://www.googleapis.com/auth/youtube.upload",
        "https://www.googleapis.com/auth/youtube",
        "https://www.googleapis.com/auth/youtubepartner",
        "https://www.googleapis.com/auth/youtube.force-ssl",
        "youtube.upload",
        "youtube",
        "youtubepartner",
        "youtube.force-ssl",
    }
)
IDENTITY_SCOPES = frozenset(
    {
        "https://www.googleapis.com/auth/youtube.readonly",
        "https://www.googleapis.com/auth/youtube",
        "https://www.googleapis.com/auth/youtubepartner",
        "https://www.googleapis.com/auth/youtube.force-ssl",
        "youtube.readonly",
        "youtube",
        "youtubepartner",
        "youtube.force-ssl",
    }
)
COMMENT_SCOPE = "https://www.googleapis.com/auth/youtube.force-ssl"
SHORT_BASE_URL = "https://gy.g2flow.com/s2l/youtube"
W2A_BASE_URL = "https://www.dramawavew2a.com/ads/101/2284/view"
JOB_ID_RE = re.compile(r"[0-9a-f]{32}")
OPERATION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}")
ASSET_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
CHANNEL_ID_RE = re.compile(r"UC[A-Za-z0-9_-]{20,30}")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class DramaSynthesisError(RuntimeError):
    def __init__(self, code: str, message: str, status: int = 400, **details: Any):
        self.code = str(code)
        self.status = int(status)
        self.details = dict(details)
        super().__init__(message)


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_channel_scopes(value: Any) -> frozenset[str]:
    """Accept stored OAuth scope metadata without accepting a missing scope."""
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return frozenset()
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = text.replace(",", " ").split()
        return normalize_channel_scopes(parsed)
    if isinstance(value, Mapping):
        for key in ("scopes", "scope"):
            if key in value:
                return normalize_channel_scopes(value.get(key))
        return frozenset()
    if not isinstance(value, (list, tuple, set, frozenset)):
        return frozenset()
    normalized = set()
    for item in value:
        scope = str(item or "").strip()
        if not scope:
            continue
        normalized.add(scope)
        if scope.startswith("https://www.googleapis.com/auth/"):
            normalized.add(scope.rsplit("/", 1)[-1])
    return frozenset(normalized)


def scope_capabilities(scopes: Iterable[str]) -> Dict[str, bool]:
    normalized = normalize_channel_scopes(list(scopes))
    upload = bool(normalized & UPLOAD_SCOPES)
    identity = bool(normalized & IDENTITY_SCOPES)
    comment = COMMENT_SCOPE in normalized or "youtube.force-ssl" in normalized
    return {"upload_eligible": upload, "identity_eligible": identity, "comment_eligible": comment}


def _catalog_assets(catalog: Mapping[str, Any]) -> Dict[str, tuple[Dict[str, Any], ...]]:
    if int(catalog.get("version") or 0) != RECIPE_VERSION or str(catalog.get("profile") or "") != RECIPE_PROFILE:
        raise DramaSynthesisError("drama_template_catalog_invalid", "随机模板目录版本无效", 503)
    fingerprint = str(catalog.get("manifest_sha256") or "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise DramaSynthesisError("drama_template_catalog_invalid", "随机模板目录指纹无效", 503)
    categories = catalog.get("categories")
    if not isinstance(categories, Mapping):
        raise DramaSynthesisError("drama_template_catalog_invalid", "随机模板目录无效", 503)
    result: Dict[str, tuple[Dict[str, Any], ...]] = {}
    for category in RECIPE_CATEGORIES:
        rows = categories.get(category)
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)) or not rows:
            raise DramaSynthesisError("drama_template_catalog_invalid", "随机模板层级不完整", 503)
        normalized = []
        seen = set()
        for raw in rows:
            if not isinstance(raw, Mapping):
                raise DramaSynthesisError("drama_template_catalog_invalid", "随机模板素材无效", 503)
            name = str(raw.get("name") or "")
            sha = str(raw.get("sha256") or "").lower()
            media_type = str(raw.get("media_type") or "")
            try:
                size = int(raw.get("size") or 0)
            except (TypeError, ValueError, OverflowError):
                size = 0
            if (
                not ASSET_NAME_RE.fullmatch(name)
                or name in seen
                or not re.fullmatch(r"[0-9a-f]{64}", sha)
                or media_type not in {"image/png", "video/webm"}
                or not 0 < size <= 2 * 1024 * 1024 * 1024
            ):
                raise DramaSynthesisError("drama_template_catalog_invalid", "随机模板素材元数据无效", 503)
            normalized.append({"name": name, "sha256": sha, "media_type": media_type, "size": size})
            seen.add(name)
        result[category] = tuple(normalized)
    return result


def _stable_int(identity: Mapping[str, Any], label: str, low: int, high: int) -> int:
    raw = _canonical_json({"identity": identity, "label": label}).encode("utf-8")
    return low + int.from_bytes(hashlib.sha256(raw).digest()[:8], "big") % (high - low + 1)


def freeze_random_recipe(
    *,
    job_id: str,
    content_id: str,
    request: Any,
    catalog: Mapping[str, Any],
) -> Dict[str, Any]:
    """Resolve auto/manual input exactly once into an immutable recipe."""
    if not JOB_ID_RE.fullmatch(str(job_id or "")):
        raise DramaSynthesisError("invalid_request", "任务ID无效")
    if not isinstance(request, Mapping):
        raise DramaSynthesisError("drama_random_template_required", "随机模板配置必填")
    mode = str(request.get("mode") or "").strip().lower()
    if mode not in {"auto", "manual"}:
        raise DramaSynthesisError("drama_random_template_mode_invalid", "随机模板模式无效")
    source = str(request.get("source") or "").strip()
    if source not in {"concat_video", "no_bgm_video"}:
        raise DramaSynthesisError("drama_random_template_source_invalid", "随机模板源视频必填且无效")
    assets = _catalog_assets(catalog)
    manifest_sha = str(catalog["manifest_sha256"]).lower()
    identity = {
        "job_id": str(job_id),
        "content_id": str(content_id or ""),
        "manifest_sha256": manifest_sha,
        "profile": RECIPE_PROFILE,
        "version": RECIPE_VERSION,
        "source": source,
    }
    requested_layers = request.get("layers") if isinstance(request.get("layers"), Mapping) else {}
    if mode == "manual" and set(requested_layers) != set(RECIPE_CATEGORIES):
        raise DramaSynthesisError("drama_random_template_layers_required", "手动模式必须选择全部模板层级")
    selected: Dict[str, Dict[str, Any]] = {}
    for category in RECIPE_CATEGORIES:
        rows = assets[category]
        if mode == "auto":
            row = rows[_stable_int(identity, "asset:" + category, 0, len(rows) - 1)]
        else:
            name = str(requested_layers.get(category) or "")
            matches = [item for item in rows if item["name"] == name]
            if len(matches) != 1:
                raise DramaSynthesisError("drama_random_template_layer_invalid", "手动模板层级不存在")
            row = matches[0]
        selected[category] = dict(row)
    recipe = {
        "version": RECIPE_VERSION,
        "profile": RECIPE_PROFILE,
        "mode": mode,
        "source": source,
        "asset_set_sha256": manifest_sha,
        "assets": selected,
        "rotation_millidegrees": _stable_int(identity, "rotation", -2000, 2000),
        "scale_bp": _stable_int(identity, "scale", 9800, 10200),
        "tint_opacity_bp": _stable_int(identity, "tint-opacity", 100, 1000),
    }
    recipe["recipe_sha256"] = _sha256_text(_canonical_json(recipe))
    return recipe


def build_long_url(job_id: str, content_id: str) -> str:
    if not JOB_ID_RE.fullmatch(str(job_id or "")):
        raise DramaSynthesisError("invalid_request", "任务ID无效")
    normalized_content_id = str(content_id or "").strip()
    if not normalized_content_id or len(normalized_content_id) > 256:
        raise DramaSynthesisError("invalid_request", "content_id无效")
    query = urlencode(
        (("af_dp", normalized_content_id), ("c", "ai_youtube"), ("af_channel", "ai_youtube"), ("af_c_id", str(job_id))),
        quote_via=quote,
        safe="",
    )
    target = W2A_BASE_URL + "?" + query
    parsed = urlsplit(target)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "www.dramawavew2a.com"
        or parsed.path != "/ads/101/2284/view"
        or parsed.fragment
    ):
        raise DramaSynthesisError("drama_short_link_target_invalid", "短链目标地址无效", 500)
    return target


def render_wrapper_html(job_id: str, content_id: str) -> bytes:
    target = build_long_url(job_id, content_id)
    escaped = html.escape(target, quote=True)
    document = (
        "<!doctype html>\n<html><head><meta charset=\"utf-8\">"
        "<meta name=\"referrer\" content=\"no-referrer\">"
        f"<meta http-equiv=\"refresh\" content=\"0;url={escaped}\">"
        f"<link rel=\"canonical\" href=\"{escaped}\">"
        "<title>DramaWave</title>"
        "<script>(function(){var u=new URL(document.querySelector('link[rel=canonical]').href);"
        "var p=new URLSearchParams(location.search),f=p.get('fbclid');"
        "if(f&&f.length<=512){u.searchParams.set('fbclid',f)}location.replace(u.toString())})()</script>"
        "</head><body></body></html>\n"
    )
    return document.encode("utf-8")


class ImmutableFilesystemPublisher:
    """Publish only ``<id>.html`` below an explicitly configured origin root."""

    def __init__(self, root: Union[str, os.PathLike]):
        path = Path(root)
        if not path.is_absolute():
            raise ValueError("short-link root must be absolute")
        self.root = path

    def publish(self, link_id: int, body: bytes) -> Dict[str, Any]:
        if not 1 <= int(link_id) <= 9_223_372_036_854_775_807:
            raise DramaSynthesisError("drama_short_link_id_invalid", "短链ID无效", 500)
        self.root.mkdir(mode=0o750, parents=True, exist_ok=True)
        target = self.root / (str(int(link_id)) + ".html")
        if target.is_symlink():
            raise DramaSynthesisError("drama_short_link_path_invalid", "短链路径无效", 500)
        if target.exists():
            existing = target.read_bytes()
            if existing != body:
                raise DramaSynthesisError("drama_short_link_immutable_conflict", "短链ID已存在且目标不一致", 409)
            return {"reused": True, "sha256": hashlib.sha256(body).hexdigest()}
        temporary = target.with_suffix(".html.tmp.%s.%s" % (os.getpid(), threading.get_ident()))
        created = False
        try:
            with temporary.open("xb") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, target)
                created = True
            except FileExistsError:
                if target.is_symlink() or target.read_bytes() != body:
                    raise DramaSynthesisError("drama_short_link_immutable_conflict", "短链ID已存在且目标不一致", 409)
        finally:
            temporary.unlink(missing_ok=True)
        return {"reused": not created, "sha256": hashlib.sha256(body).hexdigest()}


class DramaSynthesisStore:
    def __init__(self, db_path: Union[str, os.PathLike]):
        self.db_path = str(db_path)
        self._lock = threading.RLock()

    def _connect(self) -> sqlite3.Connection:
        parent = os.path.dirname(os.path.abspath(self.db_path))
        os.makedirs(parent, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def ensure_storage(self) -> None:
        statements = (
            """CREATE TABLE IF NOT EXISTS drama_synthesis_recipe(
                job_id TEXT PRIMARY KEY, recipe_version INTEGER NOT NULL,
                recipe_profile TEXT NOT NULL, recipe_sha256 TEXT NOT NULL,
                recipe_json TEXT NOT NULL, output_url TEXT NOT NULL DEFAULT '',
                output_sha256 TEXT NOT NULL DEFAULT '', output_profile TEXT NOT NULL DEFAULT '',
                created_at_utc TEXT NOT NULL, completed_at_utc TEXT NOT NULL DEFAULT ''
            )""",
            """CREATE TABLE IF NOT EXISTS drama_material_short_link(
                id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
                material_kind TEXT NOT NULL, content_id TEXT NOT NULL,
                short_url TEXT NOT NULL DEFAULT '', long_url TEXT NOT NULL,
                wrapper_sha256 TEXT NOT NULL, publish_state TEXT NOT NULL,
                error_code TEXT NOT NULL DEFAULT '', error_message TEXT NOT NULL DEFAULT '',
                created_at_utc TEXT NOT NULL, published_at_utc TEXT NOT NULL DEFAULT ''
            )""",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_drama_material_short_identity ON drama_material_short_link(job_id,material_kind)",
            """CREATE TABLE IF NOT EXISTS drama_youtube_publish(
                id INTEGER PRIMARY KEY AUTOINCREMENT, operation_id TEXT NOT NULL UNIQUE,
                job_id TEXT NOT NULL, app_id TEXT NOT NULL, channel_local_id TEXT NOT NULL,
                channel_id TEXT NOT NULL, youtube_account_id TEXT NOT NULL,
                source_kind TEXT NOT NULL, source_url TEXT NOT NULL,
                title TEXT NOT NULL, description_template TEXT NOT NULL, description_rendered TEXT NOT NULL, comment_text TEXT NOT NULL,
                operator_user_id TEXT NOT NULL DEFAULT '', operator_name TEXT NOT NULL DEFAULT '', privacy_status TEXT NOT NULL DEFAULT 'public',
                duplicate_confirmed INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL, video_state TEXT NOT NULL, comment_status TEXT NOT NULL, sync_status TEXT NOT NULL DEFAULT 'pending',
                video_id TEXT NOT NULL DEFAULT '', comment_id TEXT NOT NULL DEFAULT '',
                resumable_session_uri TEXT NOT NULL DEFAULT '', source_size INTEGER NOT NULL DEFAULT 0,
                next_byte INTEGER NOT NULL DEFAULT 0, unknown_outcome INTEGER NOT NULL DEFAULT 0,
                video_attempt_count INTEGER NOT NULL DEFAULT 0, comment_attempt_count INTEGER NOT NULL DEFAULT 0,
                source_sha256 TEXT NOT NULL DEFAULT '', source_duration_ms INTEGER NOT NULL DEFAULT 0,
                error_code TEXT NOT NULL DEFAULT '', error_message TEXT NOT NULL DEFAULT '',
                lease_owner TEXT NOT NULL DEFAULT '', lease_expires_at_utc TEXT NOT NULL DEFAULT '',
                lease_generation INTEGER NOT NULL DEFAULT 0,
                created_at_utc TEXT NOT NULL, updated_at_utc TEXT NOT NULL,
                video_published_at_utc TEXT NOT NULL DEFAULT '', comment_published_at_utc TEXT NOT NULL DEFAULT ''
            )""",
            "CREATE INDEX IF NOT EXISTS idx_drama_youtube_job_channel ON drama_youtube_publish(job_id,channel_id,id)",
            "CREATE INDEX IF NOT EXISTS idx_drama_youtube_status ON drama_youtube_publish(status,unknown_outcome,id)",
            """CREATE TABLE IF NOT EXISTS drama_youtube_sync_outbox(
                id INTEGER PRIMARY KEY AUTOINCREMENT, publish_id INTEGER NOT NULL,
                entity_kind TEXT NOT NULL, external_id TEXT NOT NULL, payload_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', attempt_count INTEGER NOT NULL DEFAULT 0,
                error_code TEXT NOT NULL DEFAULT '', error_message TEXT NOT NULL DEFAULT '',
                lease_owner TEXT NOT NULL DEFAULT '', lease_generation INTEGER NOT NULL DEFAULT 0,
                lease_expires_at_utc TEXT NOT NULL DEFAULT '', created_at_utc TEXT NOT NULL, updated_at_utc TEXT NOT NULL,
                UNIQUE(entity_kind,external_id), FOREIGN KEY(publish_id) REFERENCES drama_youtube_publish(id)
            )""",
            """CREATE TABLE IF NOT EXISTS drama_youtube_publish_event(
                id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER NOT NULL,
                phase TEXT NOT NULL, outcome TEXT NOT NULL, safe_detail TEXT NOT NULL DEFAULT '',
                created_at_utc TEXT NOT NULL,
                FOREIGN KEY(task_id) REFERENCES drama_youtube_publish(id)
            )""",
        )
        with self._lock:
            conn = self._connect()
            try:
                for statement in statements:
                    conn.execute(statement)
                columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(drama_youtube_publish)")}
                if "lease_generation" not in columns:
                    conn.execute(
                        "ALTER TABLE drama_youtube_publish ADD COLUMN lease_generation INTEGER NOT NULL DEFAULT 0"
                    )
                conn.commit()
            finally:
                conn.close()

    @staticmethod
    def _row(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
        return dict(row) if row is not None else None

    def freeze_recipe(self, job_id: str, recipe: Mapping[str, Any]) -> Dict[str, Any]:
        if (
            not JOB_ID_RE.fullmatch(str(job_id or ""))
            or int(recipe.get("version") or 0) != RECIPE_VERSION
            or str(recipe.get("profile") or "") != RECIPE_PROFILE
        ):
            raise DramaSynthesisError("drama_recipe_identity_invalid", "随机模板配方身份无效")
        encoded = _canonical_json(recipe)
        sha = str(recipe.get("recipe_sha256") or "")
        if sha != _sha256_text(_canonical_json({k: v for k, v in recipe.items() if k != "recipe_sha256"})):
            raise DramaSynthesisError("drama_recipe_hash_invalid", "随机模板配方指纹无效")
        now = utc_now()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                existing = conn.execute("SELECT * FROM drama_synthesis_recipe WHERE job_id=?", (job_id,)).fetchone()
                if existing is not None:
                    if existing["recipe_json"] != encoded:
                        raise DramaSynthesisError("drama_recipe_immutable_conflict", "任务随机模板配方已冻结", 409)
                    conn.commit()
                    return dict(existing)
                conn.execute(
                    "INSERT INTO drama_synthesis_recipe(job_id,recipe_version,recipe_profile,recipe_sha256,recipe_json,created_at_utc) VALUES(?,?,?,?,?,?)",
                    (job_id, int(recipe["version"]), str(recipe["profile"]), sha, encoded, now),
                )
                conn.commit()
                return dict(conn.execute("SELECT * FROM drama_synthesis_recipe WHERE job_id=?", (job_id,)).fetchone())
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def recipe(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute("SELECT * FROM drama_synthesis_recipe WHERE job_id=?", (job_id,)).fetchone()
                if row is None:
                    return None
                item = dict(row)
                item["recipe"] = json.loads(item.pop("recipe_json"))
                return item
            finally:
                conn.close()

    def complete_recipe(self, job_id: str, *, output_url: str, output_sha256: str, output_profile: str, recipe_sha256: str) -> Dict[str, Any]:
        parsed = urlsplit(str(output_url or ""))
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise DramaSynthesisError("drama_random_output_invalid", "随机模板成片地址无效", 502)
        if not re.fullmatch(r"[0-9a-f]{64}", str(output_sha256 or "").lower()):
            raise DramaSynthesisError("drama_random_output_invalid", "随机模板成片指纹无效", 502)
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT * FROM drama_synthesis_recipe WHERE job_id=?", (job_id,)).fetchone()
                if row is None or row["recipe_sha256"] != recipe_sha256 or row["recipe_profile"] != output_profile:
                    raise DramaSynthesisError("drama_random_output_identity_mismatch", "随机模板成片身份不一致", 409)
                if row["output_url"] and (row["output_url"] != output_url or row["output_sha256"] != output_sha256):
                    raise DramaSynthesisError("drama_random_output_immutable_conflict", "随机模板成片已冻结且不一致", 409)
                conn.execute(
                    "UPDATE drama_synthesis_recipe SET output_url=?,output_sha256=?,output_profile=?,completed_at_utc=? WHERE job_id=?",
                    (output_url, output_sha256.lower(), output_profile, utc_now(), job_id),
                )
                conn.commit()
                return dict(conn.execute("SELECT * FROM drama_synthesis_recipe WHERE job_id=?", (job_id,)).fetchone())
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def ensure_short_link(self, job_id: str, material_kind: str, content_id: str, publisher: Optional[ImmutableFilesystemPublisher]) -> Dict[str, Any]:
        if material_kind not in {"concat_video", "no_bgm_video", "random_template"}:
            raise DramaSynthesisError("drama_short_link_material_invalid", "短链只支持视频素材")
        body = render_wrapper_html(job_id, content_id)
        long_url = build_long_url(job_id, content_id)
        body_sha = hashlib.sha256(body).hexdigest()
        now = utc_now()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT * FROM drama_material_short_link WHERE job_id=? AND material_kind=?", (job_id, material_kind)).fetchone()
                if row is None:
                    cursor = conn.execute(
                        "INSERT INTO drama_material_short_link(job_id,material_kind,content_id,long_url,wrapper_sha256,publish_state,created_at_utc) VALUES(?,?,?,?,?,?,?)",
                        (job_id, material_kind, content_id, long_url, body_sha, "pending", now),
                    )
                    link_id = int(cursor.lastrowid)
                else:
                    if row["content_id"] != content_id or row["long_url"] != long_url or row["wrapper_sha256"] != body_sha:
                        raise DramaSynthesisError("drama_short_link_immutable_conflict", "短链目标已冻结且不一致", 409)
                    link_id = int(row["id"])
                    if row["publish_state"] == "published":
                        conn.commit()
                        published = dict(row)
                        published["reused"] = True
                        return published
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
        short_url = f"{SHORT_BASE_URL}/{link_id}.html"
        if publisher is None:
            self._short_failure(link_id, "drama_short_link_publisher_not_configured", "短链发布器尚未配置")
            raise DramaSynthesisError("drama_short_link_publisher_not_configured", "短链发布器尚未配置", 503, link_id=link_id)
        try:
            result = publisher.publish(link_id, body)
        except DramaSynthesisError as exc:
            self._short_failure(link_id, exc.code, str(exc))
            raise
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE drama_material_short_link SET short_url=?,publish_state='published',error_code='',error_message='',published_at_utc=? WHERE id=? AND long_url=? AND wrapper_sha256=?",
                    (short_url, utc_now(), link_id, long_url, body_sha),
                )
                conn.commit()
                row = dict(conn.execute("SELECT * FROM drama_material_short_link WHERE id=?", (link_id,)).fetchone())
                row["reused"] = bool(result.get("reused"))
                return row
            finally:
                conn.close()

    def _short_failure(self, link_id: int, code: str, message: str) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE drama_material_short_link SET publish_state='failed',error_code=?,error_message=? WHERE id=? AND publish_state<>'published'",
                    (str(code)[:96], str(message)[:500], int(link_id)),
                )
                conn.commit()
            finally:
                conn.close()

    def short_link(self, job_id: str, material_kind: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            try:
                return self._row(conn.execute("SELECT * FROM drama_material_short_link WHERE job_id=? AND material_kind=?", (job_id, material_kind)).fetchone())
            finally:
                conn.close()

    def short_links_for_job(self, job_id: str) -> list[Dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            try:
                return [dict(row) for row in conn.execute("SELECT * FROM drama_material_short_link WHERE job_id=? ORDER BY id", (job_id,)).fetchall()]
            finally:
                conn.close()

    def enqueue_youtube(
        self,
        *,
        operation_id: str,
        job_id: str,
        app_id: str,
        channel_local_id: str,
        channel_id: str,
        youtube_account_id: str,
        source_kind: str,
        source_url: str,
        title: str,
        description_template: str,
        description_rendered: str,
        comment_text: str,
        duplicate_confirmed: bool,
        scopes: Iterable[str],
        operator_user_id: str = "",
        operator_name: str = "",
    ) -> Dict[str, Any]:
        if not OPERATION_ID_RE.fullmatch(str(operation_id or "")) or not JOB_ID_RE.fullmatch(str(job_id or "")):
            raise DramaSynthesisError("invalid_request", "发布操作ID或任务ID无效")
        if not CHANNEL_ID_RE.fullmatch(str(channel_id or "")):
            raise DramaSynthesisError("youtube_channel_invalid", "YouTube频道无效")
        parsed = urlsplit(str(source_url or ""))
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise DramaSynthesisError("youtube_source_invalid", "待发布视频地址无效")
        title = str(title or "").strip()
        description_template = str(description_template or "").strip()
        description_rendered = str(description_rendered or "").strip()
        comment_text = str(comment_text or "").strip()
        if (not 1 <= len(title) <= 100 or not description_template or not description_rendered
                or len(description_template.encode("utf-8")) > 5000
                or len(description_rendered.encode("utf-8")) > 5000 or len(comment_text) > 10000):
            raise DramaSynthesisError("youtube_metadata_invalid", "YouTube标题、描述或评论超出限制")
        capabilities = scope_capabilities(scopes)
        if not capabilities["upload_eligible"]:
            raise DramaSynthesisError("youtube_upload_scope_missing", "频道授权缺少视频上传权限", 409)
        if not capabilities["identity_eligible"]:
            raise DramaSynthesisError("youtube_identity_scope_missing", "频道授权缺少身份核验权限", 409)
        if comment_text and not capabilities["comment_eligible"]:
            raise DramaSynthesisError("youtube_comment_scope_missing", "频道授权缺少评论权限", 409)
        now = utc_now()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                existing = conn.execute("SELECT * FROM drama_youtube_publish WHERE operation_id=?", (operation_id,)).fetchone()
                if existing is not None:
                    immutable = (job_id, channel_id, source_kind, source_url, title, description_template, description_rendered, comment_text)
                    stored = tuple(existing[key] for key in ("job_id", "channel_id", "source_kind", "source_url", "title", "description_template", "description_rendered", "comment_text"))
                    if immutable != stored:
                        raise DramaSynthesisError("youtube_operation_conflict", "发布操作ID已用于不同请求", 409)
                    conn.commit()
                    return dict(existing)
                risky = conn.execute(
                    "SELECT id,status,video_id,unknown_outcome FROM drama_youtube_publish WHERE job_id=? AND channel_id=? AND (video_state='published' OR status IN ('submitted','processing') OR unknown_outcome=1) ORDER BY id DESC LIMIT 1",
                    (job_id, channel_id),
                ).fetchone()
                if risky is not None and int(risky["unknown_outcome"]):
                    raise DramaSynthesisError("youtube_previous_outcome_unknown", "该任务在此频道存在结果未知的发布，禁止创建替代视频", 409, prior_task_id=int(risky["id"]))
                if risky is not None and risky["status"] in {"submitted", "processing"}:
                    raise DramaSynthesisError("youtube_previous_publish_in_progress", "该任务在此频道仍在处理，禁止重复发布", 409, prior_task_id=int(risky["id"]))
                if risky is not None and not duplicate_confirmed:
                    raise DramaSynthesisError("youtube_duplicate_confirmation_required", "该任务已在此频道发布过，需二次确认", 409, prior_task_id=int(risky["id"]))
                comment_status = "queued" if comment_text else "skipped"
                cursor = conn.execute(
                    """INSERT INTO drama_youtube_publish(
                        operation_id,job_id,app_id,channel_local_id,channel_id,youtube_account_id,
                        source_kind,source_url,title,description_template,description_rendered,comment_text,duplicate_confirmed,
                        operator_user_id,operator_name,privacy_status,status,video_state,comment_status,sync_status,created_at_utc,updated_at_utc
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        operation_id, job_id, app_id, channel_local_id, channel_id, youtube_account_id,
                        source_kind, source_url, title, description_template, description_rendered, comment_text, int(bool(duplicate_confirmed)),
                        str(operator_user_id)[:128], str(operator_name)[:128], "public", "queued", "queued", comment_status, "pending", now, now,
                    ),
                )
                task_id = int(cursor.lastrowid)
                conn.execute(
                    "INSERT INTO drama_youtube_publish_event(task_id,phase,outcome,safe_detail,created_at_utc) VALUES(?,?,?,?,?)",
                    (task_id, "enqueue", "accepted", "", now),
                )
                conn.commit()
                return dict(conn.execute("SELECT * FROM drama_youtube_publish WHERE id=?", (task_id,)).fetchone())
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def youtube_task(self, task_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            try:
                return self._row(conn.execute("SELECT * FROM drama_youtube_publish WHERE id=?", (int(task_id),)).fetchone())
            finally:
                conn.close()

    def youtube_tasks_for_job(self, job_id: str, limit: int = 50) -> list[Dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT * FROM drama_youtube_publish WHERE job_id=? ORDER BY id DESC LIMIT ?",
                    (job_id, max(1, min(int(limit), 200))),
                ).fetchall()
                return [dict(row) for row in rows]
            finally:
                conn.close()

    def claim_youtube(self, worker_id: str, lease_expires_at_utc: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    """SELECT * FROM drama_youtube_publish
                       WHERE unknown_outcome=0
                         AND (lease_owner='' OR lease_expires_at_utc='' OR lease_expires_at_utc<?)
                         AND (status IN ('queued','validating','downloading','uploading','submitted','processing')
                              OR (status='published' AND comment_status IN ('queued','retry','publishing')))
                       ORDER BY id LIMIT 1""", (utc_now(),)
                ).fetchone()
                if row is None:
                    conn.commit()
                    return None
                if row["comment_status"] == "publishing" and int(row["comment_attempt_count"] or 0) > 0:
                    now = utc_now()
                    conn.execute(
                        "UPDATE drama_youtube_publish SET status='unknown',comment_status='unknown',unknown_outcome=1,error_code='youtube_comment_worker_interrupted_unknown',error_message='YouTube评论发布结果未知',lease_owner='',lease_expires_at_utc='',updated_at_utc=? WHERE id=? AND comment_status='publishing'",
                        (now, int(row["id"])),
                    )
                    conn.execute(
                        "INSERT INTO drama_youtube_publish_event(task_id,phase,outcome,safe_detail,created_at_utc) VALUES(?,?,?,?,?)",
                        (int(row["id"]), "comment", "unknown", "youtube_comment_worker_interrupted_unknown", now),
                    )
                    conn.commit()
                    return None
                next_status = "validating" if row["status"] == "queued" else row["status"]
                next_comment = "publishing" if row["video_state"] == "published" else row["comment_status"]
                updated = conn.execute(
                    "UPDATE drama_youtube_publish SET status=?,comment_status=?,lease_owner=?,lease_expires_at_utc=?,lease_generation=lease_generation+1,updated_at_utc=? WHERE id=? AND status=? AND lease_generation=?",
                    (next_status, next_comment, worker_id, lease_expires_at_utc, utc_now(), int(row["id"]), row["status"], int(row["lease_generation"])),
                ).rowcount
                if updated != 1:
                    conn.rollback()
                    return None
                conn.commit()
                return dict(conn.execute("SELECT * FROM drama_youtube_publish WHERE id=?", (int(row["id"]),)).fetchone())
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    @staticmethod
    def _stale_youtube_claim() -> DramaSynthesisError:
        return DramaSynthesisError("youtube_stale_claim", "YouTube发布任务租约已失效", 409)

    def renew_youtube_lease(
        self,
        task_id: int,
        worker_id: str,
        lease_generation: int,
        lease_expires_at_utc: str,
    ) -> Dict[str, Any]:
        with self._lock:
            conn = self._connect()
            try:
                updated = conn.execute(
                    """UPDATE drama_youtube_publish
                       SET lease_expires_at_utc=?,updated_at_utc=?
                       WHERE id=? AND lease_owner=? AND lease_generation=?
                          AND status IN ('validating','downloading','uploading','submitted','processing','published')""",
                    (lease_expires_at_utc, utc_now(), int(task_id), str(worker_id), int(lease_generation)),
                ).rowcount
                if updated != 1:
                    conn.rollback()
                    raise self._stale_youtube_claim()
                conn.commit()
                return dict(conn.execute("SELECT * FROM drama_youtube_publish WHERE id=?", (int(task_id),)).fetchone())
            finally:
                conn.close()

    def advance_youtube(self, task_id: int, status: str, *, worker_id: str, lease_generation: int, source_sha256: str = "", source_duration_ms: int = 0) -> Dict[str, Any]:
        if status not in {"validating", "downloading", "uploading"}:
            raise ValueError("invalid YouTube state")
        if source_sha256 and not re.fullmatch(r"[0-9a-f]{64}", source_sha256):
            raise DramaSynthesisError("youtube_source_identity_invalid", "视频素材指纹无效", 409)
        with self._lock:
            conn = self._connect()
            try:
                updated = conn.execute(
                    "UPDATE drama_youtube_publish SET status=?,video_state=?,source_sha256=CASE WHEN ?<>'' THEN ? ELSE source_sha256 END,source_duration_ms=CASE WHEN ?>0 THEN ? ELSE source_duration_ms END,updated_at_utc=? WHERE id=? AND lease_owner=? AND lease_generation=?",
                    (status, status, source_sha256, source_sha256, int(source_duration_ms), int(source_duration_ms), utc_now(), int(task_id), str(worker_id), int(lease_generation)),
                ).rowcount
                if updated != 1:
                    raise self._stale_youtube_claim()
                conn.commit()
                return dict(conn.execute("SELECT * FROM drama_youtube_publish WHERE id=?", (int(task_id),)).fetchone())
            finally:
                conn.close()

    def set_upload_session(
        self,
        task_id: int,
        session_uri: str,
        source_size: int,
        *,
        worker_id: str,
        lease_generation: int,
    ) -> None:
        parsed = urlsplit(str(session_uri or ""))
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment or source_size <= 0:
            raise DramaSynthesisError("youtube_resumable_session_invalid", "YouTube断点续传会话无效", 502)
        with self._lock:
            conn = self._connect()
            try:
                updated = conn.execute(
                    """UPDATE drama_youtube_publish
                       SET status='uploading',video_state='uploading',resumable_session_uri=?,source_size=?,video_attempt_count=video_attempt_count+1,updated_at_utc=?
                       WHERE id=? AND lease_owner=? AND lease_generation=? AND status IN ('validating','downloading','uploading') AND video_state<>'published'""",
                    (session_uri, int(source_size), utc_now(), int(task_id), str(worker_id), int(lease_generation)),
                ).rowcount
                if updated != 1:
                    conn.rollback()
                    raise self._stale_youtube_claim()
                conn.commit()
            finally:
                conn.close()

    def set_upload_offset(self, task_id: int, next_byte: int, *, worker_id: str, lease_generation: int) -> None:
        with self._lock:
            conn = self._connect()
            try:
                updated = conn.execute(
                    """UPDATE drama_youtube_publish SET next_byte=?,updated_at_utc=?
                       WHERE id=? AND lease_owner=? AND lease_generation=? AND status IN ('uploading','submitted') AND video_state<>'published'""",
                    (max(0, int(next_byte)), utc_now(), int(task_id), str(worker_id), int(lease_generation)),
                ).rowcount
                if updated != 1:
                    conn.rollback()
                    raise self._stale_youtube_claim()
                conn.commit()
            finally:
                conn.close()

    def video_submitted(self, task_id: int, video_id: str, *, worker_id: str, lease_generation: int) -> Dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z0-9_-]{6,32}", str(video_id or "")):
            raise DramaSynthesisError("youtube_video_identity_invalid", "YouTube视频ID无效", 502)
        with self._lock:
            conn = self._connect()
            try:
                updated = conn.execute(
                    """UPDATE drama_youtube_publish SET status='submitted',video_state='submitted',video_id=?,next_byte=source_size,
                       lease_owner='',lease_expires_at_utc='',updated_at_utc=?
                       WHERE id=? AND lease_owner=? AND lease_generation=? AND video_state<>'published'""",
                    (video_id, utc_now(), int(task_id), str(worker_id), int(lease_generation)),
                ).rowcount
                if updated != 1:
                    raise self._stale_youtube_claim()
                conn.commit()
                return dict(conn.execute("SELECT * FROM drama_youtube_publish WHERE id=?", (int(task_id),)).fetchone())
            finally:
                conn.close()

    def video_processing(self, task_id: int, *, worker_id: str, lease_generation: int) -> Dict[str, Any]:
        with self._lock:
            conn = self._connect()
            try:
                updated = conn.execute(
                    "UPDATE drama_youtube_publish SET status='processing',video_state='processing',lease_owner='',lease_expires_at_utc='',updated_at_utc=? WHERE id=? AND lease_owner=? AND lease_generation=?",
                    (utc_now(), int(task_id), str(worker_id), int(lease_generation)),
                ).rowcount
                if updated != 1:
                    raise self._stale_youtube_claim()
                conn.commit()
                return dict(conn.execute("SELECT * FROM drama_youtube_publish WHERE id=?", (int(task_id),)).fetchone())
            finally:
                conn.close()

    def video_published(self, task_id: int, video_id: str, *, worker_id: str, lease_generation: int) -> Dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z0-9_-]{6,32}", str(video_id or "")):
            raise DramaSynthesisError("youtube_video_identity_invalid", "YouTube视频ID无效", 502)
        now = utc_now()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT * FROM drama_youtube_publish WHERE id=? AND lease_owner=? AND lease_generation=?",
                    (int(task_id), str(worker_id), int(lease_generation)),
                ).fetchone()
                if row is None:
                    raise self._stale_youtube_claim()
                if row["video_id"] and row["video_id"] != video_id:
                    raise DramaSynthesisError("youtube_video_identity_conflict", "YouTube视频身份冲突", 409)
                terminal = row["comment_status"] == "skipped"
                if terminal:
                    updated = conn.execute(
                        """UPDATE drama_youtube_publish
                           SET status='published',video_state='published',video_id=?,sync_status='pending',unknown_outcome=0,error_code='',error_message='',lease_owner='',lease_expires_at_utc='',video_published_at_utc=?,updated_at_utc=?
                            WHERE id=? AND lease_owner=? AND lease_generation=? AND status IN ('submitted','processing')""",
                        (video_id, now, now, int(task_id), str(worker_id), int(lease_generation)),
                    ).rowcount
                else:
                    updated = conn.execute(
                        """UPDATE drama_youtube_publish
                           SET status='published',comment_status='publishing',video_state='published',video_id=?,sync_status='pending',unknown_outcome=0,error_code='',error_message='',video_published_at_utc=?,updated_at_utc=?
                            WHERE id=? AND lease_owner=? AND lease_generation=? AND status IN ('submitted','processing')""",
                        (video_id, now, now, int(task_id), str(worker_id), int(lease_generation)),
                    ).rowcount
                if updated != 1:
                    raise self._stale_youtube_claim()
                conn.execute(
                    "INSERT INTO drama_youtube_publish_event(task_id,phase,outcome,safe_detail,created_at_utc) VALUES(?,?,?,?,?)",
                    (int(task_id), "video", "published", video_id, now),
                )
                payload = _canonical_json({"publish_id": int(task_id), "video_id": video_id})
                for entity_kind in ("video", "publish_log"):
                    conn.execute(
                        "INSERT OR IGNORE INTO drama_youtube_sync_outbox(publish_id,entity_kind,external_id,payload_json,status,created_at_utc,updated_at_utc) VALUES(?,?,?,?,?,?,?)",
                        (int(task_id), entity_kind, video_id, payload, "pending", now, now),
                    )
                conn.commit()
                return dict(conn.execute("SELECT * FROM drama_youtube_publish WHERE id=?", (int(task_id),)).fetchone())
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def comment_published(self, task_id: int, comment_id: str, *, worker_id: str, lease_generation: int) -> Dict[str, Any]:
        if not str(comment_id or "").strip():
            raise DramaSynthesisError("youtube_comment_identity_invalid", "YouTube评论ID无效", 502)
        now = utc_now()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT * FROM drama_youtube_publish WHERE id=? AND lease_owner=? AND lease_generation=?",
                    (int(task_id), str(worker_id), int(lease_generation)),
                ).fetchone()
                if row is None:
                    raise self._stale_youtube_claim()
                if row["video_state"] != "published" or not row["video_id"]:
                    raise DramaSynthesisError("youtube_video_not_confirmed", "视频未确认发布，禁止评论", 409)
                if row["comment_id"] and row["comment_id"] != comment_id:
                    raise DramaSynthesisError("youtube_comment_identity_conflict", "YouTube评论身份冲突", 409)
                updated = conn.execute(
                    """UPDATE drama_youtube_publish
                       SET status='published',comment_status='published',comment_id=?,sync_status='pending',unknown_outcome=0,error_code='',error_message='',lease_owner='',lease_expires_at_utc='',comment_published_at_utc=?,updated_at_utc=?
                       WHERE id=? AND lease_owner=? AND lease_generation=? AND status='published' AND comment_status='publishing'""",
                    (comment_id, now, now, int(task_id), str(worker_id), int(lease_generation)),
                ).rowcount
                if updated != 1:
                    raise self._stale_youtube_claim()
                conn.execute(
                    "INSERT INTO drama_youtube_publish_event(task_id,phase,outcome,safe_detail,created_at_utc) VALUES(?,?,?,?,?)",
                    (int(task_id), "comment", "published", str(comment_id)[:200], now),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO drama_youtube_sync_outbox(publish_id,entity_kind,external_id,payload_json,status,created_at_utc,updated_at_utc) VALUES(?,?,?,?,?,?,?)",
                    (int(task_id), "comment", str(comment_id), _canonical_json({"publish_id": int(task_id), "video_id": row["video_id"], "comment_id": str(comment_id)}), "pending", now, now),
                )
                conn.commit()
                return dict(conn.execute("SELECT * FROM drama_youtube_publish WHERE id=?", (int(task_id),)).fetchone())
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def fail_youtube(
        self,
        task_id: int,
        *,
        worker_id: str,
        lease_generation: int,
        phase: str,
        code: str,
        message: str,
        unknown: bool,
        retryable: bool = False,
    ) -> Dict[str, Any]:
        if phase not in {"video", "comment"}:
            raise ValueError("invalid YouTube phase")
        now = utc_now()
        column = "video_state" if phase == "video" else "comment_status"
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT * FROM drama_youtube_publish WHERE id=? AND lease_owner=? AND lease_generation=?",
                    (int(task_id), str(worker_id), int(lease_generation)),
                ).fetchone()
                if row is None:
                    raise self._stale_youtube_claim()
                if unknown:
                    status, state = "unknown", "unknown"
                elif retryable and phase == "comment":
                    status, state = "published", "retry"
                elif retryable and row["video_id"]:
                    status, state = "processing", "processing"
                elif retryable:
                    status, state = "queued", "queued"
                else:
                    status, state = "failed", "failed"
                updated = conn.execute(
                    f"""UPDATE drama_youtube_publish
                        SET status=?,{column}=?,unknown_outcome=?,error_code=?,error_message=?,lease_owner='',lease_expires_at_utc='',updated_at_utc=?
                        WHERE id=? AND lease_owner=? AND lease_generation=?""",
                    (
                        status, state, int(bool(unknown)), str(code)[:96], str(message)[:500], now,
                        int(task_id), str(worker_id), int(lease_generation),
                    ),
                ).rowcount
                if updated != 1:
                    raise self._stale_youtube_claim()
                conn.execute(
                    "INSERT INTO drama_youtube_publish_event(task_id,phase,outcome,safe_detail,created_at_utc) VALUES(?,?,?,?,?)",
                    (int(task_id), phase, status, str(code)[:96], now),
                )
                conn.commit()
                return dict(conn.execute("SELECT * FROM drama_youtube_publish WHERE id=?", (int(task_id),)).fetchone())
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def mark_comment_attempt(self, task_id: int, *, worker_id: str, lease_generation: int) -> None:
        with self._lock:
            conn = self._connect()
            try:
                updated = conn.execute(
                    """UPDATE drama_youtube_publish
                       SET comment_attempt_count=comment_attempt_count+1,updated_at_utc=?
                       WHERE id=? AND lease_owner=? AND lease_generation=? AND status='published' AND comment_status='publishing'
                         AND video_state='published' AND comment_status<>'published'""",
                    (utc_now(), int(task_id), str(worker_id), int(lease_generation)),
                ).rowcount
                if updated != 1:
                    conn.rollback()
                    raise self._stale_youtube_claim()
                conn.commit()
            finally:
                conn.close()

    def retry_youtube_comment(self, task_id: int) -> Dict[str, Any]:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT * FROM drama_youtube_publish WHERE id=?", (int(task_id),)).fetchone()
                if row is None:
                    raise DramaSynthesisError("youtube_publish_not_found", "YouTube发布任务不存在", 404)
                if row["video_state"] != "published" or int(row["unknown_outcome"] or 0):
                    raise DramaSynthesisError("youtube_comment_retry_unsafe", "评论结果不安全，禁止自动重试", 409)
                if row["comment_status"] == "published":
                    conn.commit()
                    return dict(row)
                if row["comment_status"] != "failed":
                    raise DramaSynthesisError("youtube_comment_retry_invalid", "评论当前不可重试", 409)
                conn.execute(
                    "UPDATE drama_youtube_publish SET status='published',comment_status='queued',error_code='',error_message='',updated_at_utc=? WHERE id=?",
                    (utc_now(), int(task_id)),
                )
                conn.commit()
                return dict(conn.execute("SELECT * FROM drama_youtube_publish WHERE id=?", (int(task_id),)).fetchone())
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def claim_youtube_sync(self, worker_id: str, lease_expires_at_utc: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT * FROM drama_youtube_sync_outbox WHERE (status IN ('pending','failed') OR (status='syncing' AND lease_expires_at_utc<>'' AND lease_expires_at_utc<?)) AND (lease_owner='' OR lease_expires_at_utc='' OR lease_expires_at_utc<?) ORDER BY id LIMIT 1",
                    (utc_now(), utc_now()),
                ).fetchone()
                if row is None:
                    conn.commit()
                    return None
                updated = conn.execute(
                    "UPDATE drama_youtube_sync_outbox SET status='syncing',lease_owner=?,lease_expires_at_utc=?,lease_generation=lease_generation+1,attempt_count=attempt_count+1,updated_at_utc=? WHERE id=? AND lease_generation=?",
                    (worker_id, lease_expires_at_utc, utc_now(), int(row["id"]), int(row["lease_generation"])),
                ).rowcount
                if updated != 1:
                    conn.rollback()
                    return None
                conn.commit()
                return dict(conn.execute("SELECT * FROM drama_youtube_sync_outbox WHERE id=?", (int(row["id"]),)).fetchone())
            finally:
                conn.close()

    def finish_youtube_sync(self, outbox_id: int, *, worker_id: str, lease_generation: int, success: bool, code: str = "", message: str = "") -> Dict[str, Any]:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT * FROM drama_youtube_sync_outbox WHERE id=? AND lease_owner=? AND lease_generation=?", (int(outbox_id), worker_id, int(lease_generation))).fetchone()
                if row is None:
                    raise self._stale_youtube_claim()
                state = "synced" if success else "failed"
                conn.execute(
                    "UPDATE drama_youtube_sync_outbox SET status=?,error_code=?,error_message=?,lease_owner='',lease_expires_at_utc='',updated_at_utc=? WHERE id=? AND lease_owner=? AND lease_generation=?",
                    (state, str(code)[:96], str(message)[:500], utc_now(), int(outbox_id), worker_id, int(lease_generation)),
                )
                pending = conn.execute("SELECT COUNT(*) FROM drama_youtube_sync_outbox WHERE publish_id=? AND status<>'synced'", (int(row["publish_id"]),)).fetchone()[0]
                conn.execute("UPDATE drama_youtube_publish SET sync_status=?,updated_at_utc=? WHERE id=?", ("synced" if pending == 0 else "failed", utc_now(), int(row["publish_id"])))
                conn.commit()
                return dict(conn.execute("SELECT * FROM drama_youtube_sync_outbox WHERE id=?", (int(outbox_id),)).fetchone())
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()


__all__ = [
    "COMMENT_SCOPE",
    "IDENTITY_SCOPES",
    "UPLOAD_SCOPES",
    "DramaSynthesisError",
    "DramaSynthesisStore",
    "ImmutableFilesystemPublisher",
    "RECIPE_CATEGORIES",
    "RECIPE_PROFILE",
    "SHORT_BASE_URL",
    "W2A_BASE_URL",
    "build_long_url",
    "freeze_random_recipe",
    "normalize_channel_scopes",
    "render_wrapper_html",
    "scope_capabilities",
]
