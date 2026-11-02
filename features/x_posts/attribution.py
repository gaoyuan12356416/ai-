"""Versioned X attribution, frozen independently of publishing outcomes."""
import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlencode

VERSION = "x-attribution-v2"
BASE_URL = "https://www.dramawavew2a.com/ads/101/2116/view"
TABLE = "x_post_link_attribution"
SCHEMA = """CREATE TABLE IF NOT EXISTS x_post_link_attribution(
    log_id INTEGER PRIMARY KEY, queue_id INTEGER NOT NULL UNIQUE,
    version TEXT NOT NULL, context_json TEXT NOT NULL, created_at TEXT NOT NULL)"""


class AttributionError(Exception):
    def __init__(self, code, status=409):
        super().__init__(code)
        self.code, self.status_code = code, status


def readonly(path):
    c = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA query_only=ON")
    return c


def epoch(value):
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None or dt.utcoffset() is None or dt.timestamp() <= 0:
            raise ValueError()
        return int(dt.timestamp())
    except (TypeError, ValueError, OverflowError, OSError):
        raise AttributionError("x_attribution_timestamp_invalid")


def valid_id(value):
    text = str(value or "")
    if not re.fullmatch(r"[1-9][0-9]*", text):
        raise AttributionError("x_attribution_identity_invalid")
    return text


def text(value, campaign=False):
    value = str(value or "").strip()
    if not value or len(value) > 512 or any(ord(c) < 32 or ord(c) == 127 for c in value) or (campaign and "*" in value):
        raise AttributionError("x_attribution_text_invalid")
    return value


def validate_context(context, log=None):
    if not isinstance(context, dict) or context.get("version") != VERSION:
        raise AttributionError("x_attribution_version_conflict")
    for key in ("channel_id", "sub_user_id", "af_c_id"):
        valid_id(context.get(key))
    for key in ("tenant", "owner", "channel_name", "material_id", "material_name", "content_id"):
        text(context.get(key))
    for key in ("language", "drama_name"):
        text(context.get(key), True)
    text(context.get("tag") or "none", True)
    epoch(context.get("frozen_at"))
    if log is not None and (context["af_c_id"] != str(log["queue_id"]) or context["frozen_at"] != log["created_at"]):
        raise AttributionError("x_attribution_identity_conflict")
    return context


def build_url(context, log):
    validate_context(context, log)
    campaign = "yingliang_post_CLV_VL_x_%s*%snone%s*%s*%s*%s" % (
        valid_id(context["channel_id"]), epoch(log["created_at"]),
        text(context["language"], True), text(context["drama_name"], True),
        text(context.get("tag") or "none", True), valid_id(log["id"]))
    return BASE_URL + "?" + urlencode([
        ("c", campaign), ("af_adset", text(context["channel_name"])),
        ("af_adset_id", valid_id(context["channel_id"])),
        ("af_ad", text(context["material_name"]) + "_contentid[" + text(context["content_id"]) + "]"),
        ("af_ad_id", text(context["material_id"])), ("af_channel", valid_id(context["sub_user_id"])),
        ("af_c_id", valid_id(context["af_c_id"])), ("af_dp", text(context["content_id"])),
    ], quote_via=quote, safe="*")


def load_frozen(db_path, log_id):
    valid_id(log_id)
    with closing(readonly(db_path)) as c:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone():
            return None
        row = c.execute("SELECT queue_id,version,context_json,created_at FROM x_post_link_attribution WHERE log_id=?", (log_id,)).fetchone()
    if row:
        if row["version"] != VERSION:
            raise AttributionError("x_attribution_version_conflict")
        try:
            context = validate_context(json.loads(row["context_json"]))
        except (ValueError, TypeError):
            raise AttributionError("x_attribution_frozen_invalid") from None
        if context["af_c_id"] != str(row["queue_id"]) or context["frozen_at"] != row["created_at"]:
            raise AttributionError("x_attribution_identity_conflict")
        return context
    return None


def freeze(db_path, log, context):
    """Compare-and-insert; never replace ownership after a first resolution."""
    validate_context(context, log)
    encoded = json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    with closing(sqlite3.connect(str(db_path), timeout=10)) as c:
        with c:
            c.execute(SCHEMA)
            c.execute("BEGIN IMMEDIATE")
            publication = c.execute("SELECT queue_id,created_at FROM x_post_publish_log WHERE id=?", (log["id"],)).fetchone()
            if publication != (int(log["queue_id"]), log["created_at"]):
                raise AttributionError("x_attribution_publication_conflict")
            existing = c.execute("SELECT queue_id,version,context_json FROM x_post_link_attribution WHERE log_id=?", (log["id"],)).fetchone()
            if existing:
                if existing != (int(log["queue_id"]), VERSION, encoded):
                    raise AttributionError("x_attribution_frozen_conflict")
            else:
                try:
                    c.execute("INSERT INTO x_post_link_attribution VALUES(?,?,?,?,?)",
                              (log["id"], log["queue_id"], VERSION, encoded, context["frozen_at"]))
                except sqlite3.IntegrityError:
                    raise AttributionError("x_attribution_frozen_conflict") from None
    return context


class Resolver:
    """Only reads trusted stores; SQL lookup callbacks never expose credentials."""
    def __init__(self, x_db, auto_db, users_db, ids_for_email, emails_for_id):
        self.x_db, self.auto_db, self.users_db = x_db, auto_db, users_db
        self.ids_for_email, self.emails_for_id = ids_for_email, emails_for_id
        self._id_cache, self._email_cache = {}, {}

    def users(self):
        with closing(readonly(self.users_db)) as c:
            return [dict(r) for r in c.execute("SELECT user_id,tenant_key,email,name FROM drama_admin_user")]

    def _mapped(self, email):
        key = email.strip().lower()
        if key not in self._id_cache:
            try:
                raw = self.ids_for_email(key)
            except AttributionError:
                raise
            except Exception:
                raise AttributionError("x_attribution_mapping_unavailable", 503) from None
            if not isinstance(raw, (list, tuple, set)):
                raise AttributionError("x_attribution_mapping_unavailable", 503)
            ids = {valid_id(v) for v in raw}
            if len(ids) > 1:
                raise AttributionError("x_attribution_user_ambiguous")
            self._id_cache[key] = next(iter(ids)) if ids else None
        return self._id_cache[key]

    def _default_owner(self, users, tenant=""):
        if "248" not in self._email_cache:
            try:
                emails = self.emails_for_id("248")
            except AttributionError:
                raise
            except Exception:
                raise AttributionError("x_attribution_mapping_unavailable", 503) from None
            if not isinstance(emails, (list, tuple, set)):
                raise AttributionError("x_attribution_mapping_unavailable", 503)
            self._email_cache["248"] = {str(v).strip().lower() for v in emails if v}
        candidates = [u for u in users if u["tenant_key"] and u["user_id"]
                      and str(u["email"] or "").strip().lower() in self._email_cache["248"]
                      and (not tenant or u["tenant_key"] == tenant)]
        identities = {(u["tenant_key"], u["user_id"]) for u in candidates}
        if len(identities) != 1:
            raise AttributionError("x_attribution_default_owner_unavailable", 503)
        return candidates[0]

    def resolve(self, queue_id, log_id):
        valid_id(queue_id); valid_id(log_id)
        with closing(readonly(self.x_db)) as c:
            queue = c.execute("SELECT * FROM x_post_queue WHERE id=?", (queue_id,)).fetchone()
            log = c.execute("SELECT * FROM x_post_publish_log WHERE id=? AND queue_id=?", (log_id, queue_id)).fetchone()
            if queue is None or log is None:
                raise AttributionError("x_attribution_publication_missing", 404)
            q, l = dict(queue), dict(log)
            manual = c.execute("SELECT * FROM x_post_manual_run WHERE id=?", (q.get("manual_run_id"),)).fetchone()
            m = dict(manual) if manual else {}
        frozen = load_frozen(self.x_db, log_id)
        if frozen:
            return validate_context(frozen, l)
        source = m.get("trigger_source") or "system"
        owner_id, tenant, template_id = "", "", None
        if source == "auto_template":
            match = re.fullmatch(r"x-auto-template-([1-9][0-9]*)", m.get("template_ref", ""))
            if not match:
                raise AttributionError("x_attribution_template_reference_invalid")
            template_id = int(match.group(1))
            with closing(readonly(self.auto_db)) as c:
                tpl = c.execute("SELECT * FROM x_auto_template WHERE id=?", (template_id,)).fetchone()
                task_key = re.fullmatch(r"x-auto-task-([1-9][0-9]*)", m.get("external_task_key", ""))
                task = c.execute("SELECT template_id,execution_queue_id FROM x_auto_task WHERE id=?",
                                 (int(task_key.group(1)) if task_key else -1,)).fetchone()
                if not task or task["template_id"] != template_id or task["execution_queue_id"] != int(queue_id):
                    raise AttributionError("x_attribution_template_task_conflict")
                if tpl:
                    tpl = dict(tpl)
                    owner_id, tenant = tpl["created_by_user_id"], tpl.get("created_by_tenant_key", "")
        elif source == "manual":
            owner_id, tenant = m.get("actor_user_id", ""), m.get("actor_tenant_key", "")
        elif source != "system":
            raise AttributionError("x_attribution_source_invalid")
        users = self.users()
        pool_owner = None
        if source == "system":
            from .pool_ownership import identity
            with closing(readonly(self.x_db)) as c:
                pool_owner = identity(c, q, users)
            if pool_owner:
                owner_id, tenant = pool_owner['owner'], pool_owner['tenant']
        candidates = [u for u in users if u["user_id"] == owner_id and (not tenant or u["tenant_key"] == tenant)] if owner_id else []
        if len({(u["tenant_key"], u["user_id"]) for u in candidates}) > 1:
            raise AttributionError("x_attribution_tenant_ambiguous")
        actor = candidates[0] if candidates else None
        if actor and not actor["tenant_key"]:
            raise AttributionError("x_attribution_tenant_unavailable")
        fallback = ""
        if not owner_id:
            fallback = "no_publisher_relation"
        elif actor is None:
            fallback = "publisher_not_found"
        elif not str(actor["email"] or "").strip():
            fallback = "publisher_email_missing"
        sub_id = None if fallback else self._mapped(actor["email"])
        if not fallback and sub_id is None:
            fallback = "publisher_mapping_missing"
        if fallback and pool_owner:
            # Missing URL mapping must not transfer the reporting owner to admin.
            effective, sub_id = actor, "248"
            fallback = "pool_url_" + fallback
        elif fallback:
            effective = self._default_owner(users, tenant or (actor or {}).get("tenant_key", ""))
            sub_id = "248"
        else:
            effective = actor
        context = dict(
            version=VERSION, tenant=effective["tenant_key"], owner=effective["user_id"],
            owner_name=effective["name"], sub_user_id=sub_id,
            source_owner=owner_id, source_tenant=tenant or (actor or {}).get("tenant_key", ""),
            fallback_reason=fallback, template_id=template_id, link_type=source,
            channel_id=text(q["page_id"]), channel_name=text(q["page_name"]),
            material_id=text(q["material_id"]), material_name=text(q["material_name"]),
            content_id=text(q["content_id"]), drama_name=text(q["drama_name"], True),
            language=text(q["material_language"], True), tag=text(q.get("tag") or "none", True),
            af_c_id=str(queue_id), excluded=False, frozen_at=l["created_at"])
        if pool_owner:
            context.update({k: v for k, v in pool_owner.items()
                            if k not in {'fallback_reason'}})
        return validate_context(context, l)


def resolve_via_loopback(queue_id, log_id):
    import os
    import urllib.request
    token = (os.environ.get("X_INTERNAL_TOKEN") or os.environ.get("X_POST_AUTOMATION_INTERNAL_TOKEN") or "").strip()
    if len(token) < 32:
        raise AttributionError("x_attribution_internal_auth_missing", 503)
    payload = json.dumps(dict(queue_id=queue_id, log_id=log_id)).encode()
    req = urllib.request.Request("http://127.0.0.1:8787/internal/x-posts/attribution/resolve", data=payload,
                                 headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            result = json.loads(response.read(65536))
        context = validate_context(result["context"])
        if context["af_c_id"] != str(queue_id):
            raise ValueError()
        return context
    except Exception:
        raise AttributionError("x_attribution_resolution_unavailable", 503) from None
