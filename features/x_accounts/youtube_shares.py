"""Durable, scoped YouTube link shares; X credentials stay in the sidecar.

This module deliberately has no OAuth-service import. Account access, final
credential locking and HTTP writes are injected so recovery can be tested
without reading token files or contacting X. Every actual send has a durable
attempt marker, and only untouched queued rows can be claimed.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

from features.x_accounts.youtube_share_text import canonical_url, contains_source_url, weighted_length


MAX_ACCOUNTS = 20
MAX_TEXT_BYTES = 8192
SOURCE_MAX_AGE_SECONDS = 600
HTTP_TIMEOUT_SECONDS = 25
MAX_RESPONSE_BYTES = 64 * 1024
CREATE_TWEET_URL = "https://api.x.com/2/tweets"
BLOCKING_STATES = ("queued", "publishing", "published", "unknown_outcome")
ERROR_MESSAGES = {
    "invalid_request": "分享请求参数无效",
    "x_admin_required": "仅管理员可使用全部X账号范围",
    "x_account_not_found": "X账号不存在或不属于当前用户",
    "x_account_owned_by_other": "X账号不属于当前用户",
    "x_account_disabled": "X账号已停用，不能分享",
    "x_account_not_publishable": "X账号授权当前不可用于发布",
    "x_account_publish_not_approved": "该X账号尚未勾选允许发布",
    "x_token_invalid": "X账号Token失效，请重新授权",
    "x_token_missing": "X账号Token缺失，请重新授权",
    "x_token_revoked": "X账号授权已失效，请重新授权",
    "x_disconnect_pending": "X账号退出授权尚未完成",
    "x_identity_mismatch": "X账号身份校验失败，请重新授权",
    "x_post_rate_limited": "X账号校验被限流，请稍后重新提交",
    "x_upstream_error": "X账号校验暂不可用，请稍后重新提交",
    "x_accounts_unavailable": "X账号服务暂不可用",
    "youtube_share_forbidden": "无权查看或操作该分享记录",
    "youtube_share_not_found": "分享记录不存在",
    "youtube_share_idempotency_conflict": "同一提交标识对应的分享内容已变更",
    "youtube_share_source_stale": "视频公开状态校验已过期，请重新提交",
    "youtube_share_text_too_long": "分享内容超过X的280字权重限制",
    "youtube_share_storage_unavailable": "分享记录存储暂不可用",
}


class YouTubeShareError(RuntimeError):
    def __init__(self, code="invalid_request", status=400):
        self.code = code if code in ERROR_MESSAGES else "x_accounts_unavailable"
        self.status = int(status) if status in {400, 403, 404, 409, 429, 502, 503} else 503
        super().__init__(ERROR_MESSAGES[self.code])


def _callback_error(exc):
    if isinstance(exc, YouTubeShareError):
        return exc
    code = getattr(exc, "code", "x_accounts_unavailable")
    if code not in ERROR_MESSAGES:
        code = "x_accounts_unavailable"
    return YouTubeShareError(code, getattr(exc, "status", 503))


def _call(callback, *args, **kwargs):
    try:
        return callback(*args, **kwargs)
    except Exception as exc:
        raise _callback_error(exc) from None


def _iso(epoch):
    return datetime.fromtimestamp(float(epoch), timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _identifier(value, *, maximum=255):
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise YouTubeShareError()
    result = str(value)
    if not result or result.strip() != result or len(result) > maximum or any(ord(ch) < 32 for ch in result):
        raise YouTubeShareError()
    return result


def _operation_id(value):
    if not isinstance(value, str):
        raise YouTubeShareError()
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        raise YouTubeShareError() from None
    if str(parsed) != value.lower():
        raise YouTubeShareError()
    return str(parsed)


def _video_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{11}", value):
        raise YouTubeShareError()
    return value


def ledger_path(storage_root, mount_root):
    """Refuse root-disk fallback or symlink escape before creating any ledger."""
    storage, mount = Path(storage_root), Path(mount_root)
    try:
        if not storage.is_absolute() or not mount.is_absolute():
            raise ValueError()
        if storage.is_symlink() or mount.is_symlink() or not os.path.ismount(str(mount)):
            raise ValueError()
        resolved_storage = storage.resolve(strict=True)
        resolved_mount = mount.resolve(strict=True)
        if not resolved_storage.is_dir() or not resolved_mount.is_dir():
            raise ValueError()
        if resolved_storage == resolved_mount or not resolved_storage.is_relative_to(resolved_mount):
            raise ValueError()
        if resolved_storage.stat().st_dev != resolved_mount.stat().st_dev:
            raise ValueError()
        directory = resolved_storage / "youtube-shares"
        if directory.is_symlink():
            raise ValueError()
        directory.mkdir(mode=0o700, exist_ok=True)
        directory.chmod(0o700)
        if directory.resolve(strict=True).parent != resolved_storage:
            raise ValueError()
        if directory.stat().st_dev != resolved_mount.stat().st_dev:
            raise ValueError()
        target = directory / "ledger.sqlite3"
        if target.is_symlink():
            raise ValueError()
        return target
    except (OSError, ValueError):
        raise YouTubeShareError("youtube_share_storage_unavailable", 503) from None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, _req, _fp, _code, _msg, _headers, _newurl):
        return None


_POST_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def send_text(access_token, text):
    """One bounded POST, with neither redirects, proxy inheritance nor retries."""
    request = urllib.request.Request(
        CREATE_TWEET_URL,
        data=json.dumps({"text": text}, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + access_token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with _POST_OPENER.open(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            status = response.getcode()
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        try:
            status = exc.code
            raw = exc.read(MAX_RESPONSE_BYTES + 1)
        finally:
            exc.close()
    if len(raw) > MAX_RESPONSE_BYTES:
        return status, None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        payload = None
    return status, payload


class YouTubeShares:
    def __init__(
        self,
        db_path,
        *,
        normalize_scope,
        get_account,
        verify_account,
        publish_credentials,
        sender=send_text,
        clock=time.time,
    ):
        self.db_path = Path(db_path)
        self.normalize_scope = normalize_scope
        self.get_account = get_account
        self.verify_account = verify_account
        self.publish_credentials = publish_credentials
        self.sender = sender
        self.clock = clock
        self._worker_lock = threading.Lock()
        self._worker = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._recovered = False
        self._terminal_lock = threading.Lock()
        self._terminal_pending = {}
        self._ensure_storage()

    @contextlib.contextmanager
    def _connect(self, *, write=False):
        conn = sqlite3.connect(str(self.db_path), timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _ensure_storage(self):
        try:
            self.db_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if self.db_path.is_symlink():
                raise OSError()
            with contextlib.closing(sqlite3.connect(str(self.db_path), timeout=10)) as conn:
                self.db_path.chmod(0o600)
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=FULL")
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS youtube_share_run (
                        id TEXT PRIMARY KEY,
                        actor_tenant TEXT NOT NULL, actor_user TEXT NOT NULL,
                        actor_role TEXT NOT NULL, scope TEXT NOT NULL,
                        task_id TEXT NOT NULL, video_id TEXT NOT NULL,
                        youtube_url TEXT NOT NULL, title TEXT NOT NULL,
                        text TEXT NOT NULL, description_template TEXT NOT NULL,
                        operation_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                        created_at REAL NOT NULL, source_verified_at REAL NOT NULL,
                        UNIQUE(actor_tenant, actor_user, scope, operation_id)
                    );
                    CREATE INDEX IF NOT EXISTS youtube_share_run_task
                        ON youtube_share_run(task_id,video_id,created_at);
                    CREATE TABLE IF NOT EXISTS youtube_share_item (
                        id INTEGER PRIMARY KEY,
                        run_id TEXT NOT NULL REFERENCES youtube_share_run(id),
                        ordinal INTEGER NOT NULL, account_id INTEGER NOT NULL,
                        video_id TEXT NOT NULL, username TEXT NOT NULL,
                        status TEXT NOT NULL CHECK(status IN
                            ('queued','publishing','published','failed','unknown_outcome','duplicate')),
                        duplicate_of INTEGER REFERENCES youtube_share_item(id),
                        post_id TEXT NOT NULL DEFAULT '', message TEXT NOT NULL DEFAULT '',
                        claim_token TEXT NOT NULL DEFAULT '', claimed_at REAL,
                        attempted_at REAL, finished_at REAL,
                        UNIQUE(run_id,account_id), UNIQUE(run_id,ordinal),
                        CHECK((status='duplicate')=(duplicate_of IS NOT NULL))
                    );
                    CREATE UNIQUE INDEX IF NOT EXISTS youtube_share_item_fence
                        ON youtube_share_item(video_id,account_id)
                        WHERE duplicate_of IS NULL AND status IN
                            ('queued','publishing','published','unknown_outcome');
                    CREATE INDEX IF NOT EXISTS youtube_share_item_claim
                        ON youtube_share_item(status,id);
                    CREATE TABLE IF NOT EXISTS youtube_share_attempt (
                        item_id INTEGER PRIMARY KEY REFERENCES youtube_share_item(id),
                        started_at REAL NOT NULL, finished_at REAL,
                        status TEXT NOT NULL, http_status INTEGER, post_id TEXT NOT NULL DEFAULT ''
                    );
                    """
                )
                conn.commit()
        except (OSError, sqlite3.Error):
            raise YouTubeShareError("youtube_share_storage_unavailable", 503) from None

    def _actor(self, payload):
        if not isinstance(payload, dict):
            raise YouTubeShareError()
        actor, scope = _call(self.normalize_scope, payload.get("actor", {}), payload.get("scope", "mine"))
        return actor, scope

    def _account(self, account_id, actor, scope, *, require_publishable=False):
        item = _call(self.get_account, account_id, actor, scope)
        if not isinstance(item, dict) or int(item.get("id") or 0) != account_id:
            raise YouTubeShareError("x_account_not_found", 404)
        if require_publishable:
            if item.get("status") == "disabled":
                raise YouTubeShareError("x_account_disabled", 409)
            if item.get("status") != "active":
                raise YouTubeShareError("x_account_not_publishable", 409)
            if item.get("publish_approved") is not True:
                raise YouTubeShareError("x_account_publish_not_approved", 409)
        return item

    def _fresh(self, created_at, source_verified_at):
        now = self.clock()
        if (
            not math.isfinite(source_verified_at)
            or source_verified_at > now + 30
            or now - source_verified_at > SOURCE_MAX_AGE_SECONDS
            or now - created_at > SOURCE_MAX_AGE_SECONDS
        ):
            raise YouTubeShareError("youtube_share_source_stale", 409)

    def _validate_create(self, payload, actor, scope):
        task_id = _identifier(payload.get("task_id"))
        video_id = _video_id(payload.get("video_id"))
        url = canonical_url(video_id)
        if payload.get("youtube_url") != url:
            raise YouTubeShareError()
        text = payload.get("text")
        template = payload.get("description_template")
        title = payload.get("title")
        if not all(isinstance(value, str) for value in (text, template, title)):
            raise YouTubeShareError()
        if not contains_source_url(text, video_id) or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ufeff\ufffe\uffff\ud800-\udfff]", text):
            raise YouTubeShareError()
        try:
            byte_sizes = [len(value.encode("utf-8")) for value in (text, template, title)]
        except UnicodeEncodeError:
            raise YouTubeShareError() from None
        if not text.strip() or byte_sizes[0] > MAX_TEXT_BYTES or byte_sizes[1] > MAX_TEXT_BYTES or byte_sizes[2] > 2048:
            raise YouTubeShareError()
        if weighted_length(text) > 280:
            raise YouTubeShareError("youtube_share_text_too_long", 400)
        account_ids = payload.get("account_ids")
        if not isinstance(account_ids, list) or not 1 <= len(account_ids) <= MAX_ACCOUNTS:
            raise YouTubeShareError()
        if any(type(value) is not int or not 0 < value <= 9223372036854775807 for value in account_ids):
            raise YouTubeShareError()
        if len(set(account_ids)) != len(account_ids):
            raise YouTubeShareError()
        operation_id = _operation_id(payload.get("operation_id"))
        checked_at = payload.get("source_verified_at")
        if isinstance(checked_at, bool) or not isinstance(checked_at, (int, float)):
            raise YouTubeShareError()
        fingerprint = hashlib.sha256(json.dumps({
            "actor_tenant": actor["tenant_key"], "actor_user": actor["user_id"],
            "scope": scope, "task_id": task_id, "video_id": video_id,
            "youtube_url": url, "title": title, "text": text,
            "description_template": template, "account_ids": account_ids,
        }, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
        return {
            "task_id": task_id, "video_id": video_id, "youtube_url": url,
            "title": title, "text": text, "description_template": template,
            "account_ids": account_ids, "operation_id": operation_id,
            "source_verified_at": float(checked_at), "fingerprint": fingerprint,
        }

    def _operation(self, conn, actor, scope, operation_id):
        return conn.execute(
            "SELECT * FROM youtube_share_run WHERE actor_tenant=? AND actor_user=? AND scope=? AND operation_id=?",
            (actor["tenant_key"], actor["user_id"], scope, operation_id),
        ).fetchone()

    def _authorize(self, conn, run, actor, scope, task_id):
        if str(run["task_id"]) != task_id or not (
            (run["actor_tenant"] == actor["tenant_key"] and run["actor_user"] == actor["user_id"])
            or (scope == "all" and actor.get("role") == "admin")
        ):
            raise YouTubeShareError("youtube_share_forbidden", 403)
        for item in conn.execute("SELECT account_id FROM youtube_share_item WHERE run_id=?", (run["id"],)):
            self._account(item["account_id"], actor, scope)

    def _dto(self, conn, run):
        items = []
        for row in conn.execute("SELECT * FROM youtube_share_item WHERE run_id=? ORDER BY ordinal", (run["id"],)):
            effective = row
            if row["duplicate_of"] is not None:
                effective = conn.execute("SELECT * FROM youtube_share_item WHERE id=?", (row["duplicate_of"],)).fetchone()
            items.append({
                "account_id": row["account_id"], "username": row["username"],
                "status": effective["status"],
                "post_url": "https://x.com/i/status/" + effective["post_id"] if effective["post_id"] else "",
                "message": effective["message"], "duplicate": row["duplicate_of"] is not None,
            })
        statuses = {item["status"] for item in items}
        if "publishing" in statuses or ("queued" in statuses and len(statuses) > 1):
            status = "running"
        else:
            status = "queued" if "queued" in statuses else "completed"
        return {
            "id": run["id"], "status": status, "created_at": _iso(run["created_at"]),
            "text": run["text"], "title": run["title"], "youtube_url": run["youtube_url"],
            "task_id": run["task_id"], "video_id": run["video_id"],
            "description_template": run["description_template"],
            "operation_id": run["operation_id"], "source_verified_at": run["source_verified_at"],
            "account_ids": [item["account_id"] for item in items], "items": items,
        }

    def create(self, payload):
        actor, scope = self._actor(payload)
        data = self._validate_create(payload, actor, scope)
        # Replays are read-only even if the original source check is now old.
        with self._connect() as conn:
            existing = self._operation(conn, actor, scope, data["operation_id"])
            if existing:
                if existing["fingerprint"] != data["fingerprint"]:
                    raise YouTubeShareError("youtube_share_idempotency_conflict", 409)
                self._authorize(conn, existing, actor, scope, data["task_id"])
                return {"run": self._dto(conn, existing)}
        created_at = self.clock()
        self._fresh(created_at, data["source_verified_at"])
        accounts = [self._account(account_id, actor, scope, require_publishable=True) for account_id in data["account_ids"]]
        run_id = uuid.uuid4().hex
        with self._connect(write=True) as conn:
            existing = self._operation(conn, actor, scope, data["operation_id"])
            if existing:
                if existing["fingerprint"] != data["fingerprint"]:
                    raise YouTubeShareError("youtube_share_idempotency_conflict", 409)
                self._authorize(conn, existing, actor, scope, data["task_id"])
                return {"run": self._dto(conn, existing)}
            conn.execute(
                "INSERT INTO youtube_share_run VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, actor["tenant_key"], actor["user_id"], actor.get("role", "user"), scope,
                 data["task_id"], data["video_id"], data["youtube_url"], data["title"], data["text"],
                 data["description_template"], data["operation_id"], data["fingerprint"], created_at,
                 data["source_verified_at"]),
            )
            for ordinal, account in enumerate(accounts):
                original = conn.execute(
                    "SELECT id FROM youtube_share_item WHERE video_id=? AND account_id=? "
                    "AND duplicate_of IS NULL AND status IN ('queued','publishing','published','unknown_outcome')",
                    (data["video_id"], account["id"]),
                ).fetchone()
                username = str(account.get("username") or "")
                if not re.fullmatch(r"[A-Za-z0-9_]{1,50}", username):
                    username = ""
                conn.execute(
                    "INSERT INTO youtube_share_item(run_id,ordinal,account_id,video_id,username,status,duplicate_of) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (run_id, ordinal, account["id"], data["video_id"], username,
                     "duplicate" if original else "queued", original["id"] if original else None),
                )
            run = conn.execute("SELECT * FROM youtube_share_run WHERE id=?", (run_id,)).fetchone()
            result = {"run": self._dto(conn, run)}
        self._wake.set()
        return result

    def query(self, payload):
        actor, scope = self._actor(payload)
        task_id = _identifier(payload.get("task_id"))
        with self._connect() as conn:
            if payload.get("operation_id") is not None:
                run = self._operation(conn, actor, scope, _operation_id(payload["operation_id"]))
                if not run:
                    return {"run": None}
                self._authorize(conn, run, actor, scope, task_id)
                return {"run": self._dto(conn, run)}
            video_id = _video_id(payload.get("video_id"))
            query = "SELECT * FROM youtube_share_run WHERE task_id=? AND video_id=?"
            values = [task_id, video_id]
            if scope == "mine":
                query += " AND actor_tenant=? AND actor_user=?"
                values.extend((actor["tenant_key"], actor["user_id"]))
            query += " ORDER BY created_at DESC,id DESC LIMIT 100"
            history = []
            for run in conn.execute(query, values):
                self._authorize(conn, run, actor, scope, task_id)
                history.append(self._dto(conn, run))
            return {"history": history}

    def run(self, payload):
        actor, scope = self._actor(payload)
        task_id = _identifier(payload.get("task_id"))
        run_id = payload.get("run_id")
        if not isinstance(run_id, str) or not re.fullmatch(r"[0-9a-f]{32}", run_id):
            raise YouTubeShareError()
        with self._connect() as conn:
            run = conn.execute("SELECT * FROM youtube_share_run WHERE id=?", (run_id,)).fetchone()
            if not run:
                raise YouTubeShareError("youtube_share_not_found", 404)
            self._authorize(conn, run, actor, scope, task_id)
            return {"run": self._dto(conn, run)}

    def recover_interrupted(self):
        with self._connect(write=True) as conn:
            now = self.clock()
            message = "发布过程已中断，结果待核实，系统不会自动重发"
            conn.execute(
                "UPDATE youtube_share_attempt SET status='unknown_outcome',finished_at=? "
                "WHERE item_id IN (SELECT id FROM youtube_share_item WHERE status='publishing')",
                (now,),
            )
            changed = conn.execute(
                "UPDATE youtube_share_item SET status='unknown_outcome',message=?,finished_at=?,claim_token='' "
                "WHERE status='publishing'", (message, now),
            ).rowcount
            return changed

    def claim_next(self):
        with self._connect(write=True) as conn:
            row = conn.execute(
                "SELECT i.id FROM youtube_share_item i JOIN youtube_share_run r ON r.id=i.run_id "
                "WHERE i.status='queued' AND i.duplicate_of IS NULL AND i.attempted_at IS NULL "
                "ORDER BY r.created_at,i.id LIMIT 1"
            ).fetchone()
            if not row:
                return None
            claim = uuid.uuid4().hex
            conn.execute(
                "UPDATE youtube_share_item SET status='publishing',claim_token=?,claimed_at=? WHERE id=? AND status='queued'",
                (claim, self.clock(), row["id"]),
            )
            item = dict(conn.execute("SELECT * FROM youtube_share_item WHERE id=?", (row["id"],)).fetchone())
            item["run"] = dict(conn.execute("SELECT * FROM youtube_share_run WHERE id=?", (item["run_id"],)).fetchone())
            return item

    def _begin_attempt(self, item):
        with self._connect(write=True) as conn:
            now = self.clock()
            changed = conn.execute(
                "UPDATE youtube_share_item SET attempted_at=? WHERE id=? AND claim_token=? "
                "AND status='publishing' AND attempted_at IS NULL", (now, item["id"], item["claim_token"]),
            ).rowcount
            if changed != 1:
                return False
            conn.execute(
                "INSERT INTO youtube_share_attempt(item_id,started_at,status) VALUES(?,?,'unknown_outcome')",
                (item["id"], now),
            )
        return True

    def _finish(self, item, status, message, *, http_status=None, post_id=""):
        with self._connect(write=True) as conn:
            now = self.clock()
            changed = conn.execute(
                "UPDATE youtube_share_item SET status=?,message=?,post_id=?,finished_at=?,claim_token='' "
                "WHERE id=? AND claim_token=? AND status='publishing'",
                (status, message, post_id, now, item["id"], item["claim_token"]),
            ).rowcount
            if changed:
                conn.execute(
                    "UPDATE youtube_share_attempt SET status=?,http_status=?,post_id=?,finished_at=? WHERE item_id=?",
                    (status, http_status, post_id, now, item["id"]),
                )

    def _stage_terminal(self, item, status, message, *, http_status=None, post_id=""):
        # Preserve the first known outcome even if the ledger is temporarily
        # unavailable. Never keep the credential or upstream payload in this
        # queue, and never re-enter the sender to recover a storage error.
        with self._terminal_lock:
            self._terminal_pending.setdefault(item["id"], {
                "item": {"id": item["id"], "claim_token": item["claim_token"]},
                "status": status, "message": message,
                "http_status": http_status, "post_id": post_id,
            })
        return self.flush_terminal_results()

    def flush_terminal_results(self):
        with self._terminal_lock:
            for item_id, outcome in list(self._terminal_pending.items()):
                try:
                    self._finish(**outcome)
                except Exception:
                    # The durable attempt is still fenced. The worker waits
                    # here and retries only this local write; a process crash
                    # falls back to conservative unknown-outcome recovery.
                    return False
                del self._terminal_pending[item_id]
            return True

    def process_claim(self, item):
        run = item["run"]
        actor = {"tenant_key": run["actor_tenant"], "user_id": run["actor_user"], "role": run["actor_role"]}
        attempted = False
        try:
            self._fresh(run["created_at"], run["source_verified_at"])
            verified = _call(
                self.verify_account, item["account_id"], actor, run["scope"],
                preserve_transient_status=True, require_publish_approved=True,
            )
            if not isinstance(verified, dict) or verified.get("status") != "active":
                raise YouTubeShareError("x_account_not_publishable", 409)
            if verified.get("publish_approved") is not True:
                raise YouTubeShareError("x_account_publish_not_approved", 409)
            with self.publish_credentials(item["account_id"], actor, run["scope"]) as (account, access_token):
                if account.get("status") != "active":
                    raise YouTubeShareError("x_account_not_publishable", 409)
                if account.get("publish_approved") is not True:
                    raise YouTubeShareError("x_account_publish_not_approved", 409)
                self._fresh(run["created_at"], run["source_verified_at"])
                if not self._begin_attempt(item):
                    return
                attempted = True
                http_status, response = self.sender(access_token, run["text"])
                data = response.get("data") if isinstance(response, dict) else None
                post_id = data.get("id") if isinstance(data, dict) else None
                if http_status in {200, 201} and isinstance(post_id, str) and re.fullmatch(r"[0-9]{1,30}", post_id):
                    self._stage_terminal(item, "published", "分享成功", http_status=http_status, post_id=post_id)
                elif isinstance(http_status, int) and 400 <= http_status < 500 and http_status != 408:
                    self._stage_terminal(item, "failed", "X拒绝了分享请求（HTTP %s），可确认后重新提交" % http_status, http_status=http_status)
                else:
                    self._stage_terminal(item, "unknown_outcome", "发布结果待核实，系统不会自动重发", http_status=http_status if type(http_status) is int else None)
        except Exception as exc:
            if attempted:
                self._stage_terminal(item, "unknown_outcome", "发布结果待核实，系统不会自动重发")
            else:
                self._stage_terminal(item, "failed", str(_callback_error(exc)))

    def process_next(self):
        if not self.flush_terminal_results():
            return False
        item = self.claim_next()
        if not item:
            return False
        self.process_claim(item)
        return True

    def _loop(self):
        while not self._stop.is_set():
            try:
                if self.process_next():
                    continue
            except Exception:
                # Never log callback/HTTP exception text: it may contain credentials.
                pass
            self._wake.wait(1)
            self._wake.clear()

    def start_worker(self):
        with self._worker_lock:
            if self._worker is not None and self._worker.is_alive():
                self._wake.set()
                return
            if not self._recovered:
                self.recover_interrupted()
                self._recovered = True
            self._stop.clear()
            self._worker = threading.Thread(target=self._loop, name="youtube-share-x", daemon=True)
            self._worker.start()
            self._wake.set()

    def stop_worker(self):
        self._stop.set()
        self._wake.set()
        thread = self._worker
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)
