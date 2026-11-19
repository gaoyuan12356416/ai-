"""Identity and destination usage of transferable prepared media.

Callers hold the coordinator write transaction when reserving a candidate.
Native services retain ownership of publishing eligibility and of all writes
to the platform. The index stores neither a source URL nor upload credentials.
"""

import hashlib
import json
import math
import re
from urllib.parse import parse_qsl, urlsplit


SHA256 = re.compile("[a-f0-9]{64}")
PUBLIC_BUCKET = "advertising-1306474899.cos.ap-hongkong.myqcloud.com"
SOURCE_HOSTS = {PUBLIC_BUCKET: PUBLIC_BUCKET, "ads-cdn.yingliang.tech": PUBLIC_BUCKET}
SIGNATURE_KEYS = {
    "q-sign-algorithm",
    "q-ak",
    "q-sign-time",
    "q-key-time",
    "q-header-list",
    "q-url-param-list",
    "q-signature",
    "x-cos-security-token",
}


def _json(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _obj(value):
    if isinstance(value, dict):
        return value
    try:
        obj = json.loads(value or "{}")
    except (ValueError, TypeError):
        return {}
    return obj if isinstance(obj, dict) else {}


def _positive(value):
    try:
        n = float(value)
        return n if math.isfinite(n) and n > 0 else 0
    except (ValueError, TypeError, OverflowError):
        return 0


def _digest(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _platform(channel):
    return "x" if channel in ("x_tag", "x-tag") else channel


def build_context(
    source_url,
    *,
    mode,
    contract=None,
    source_sha256="",
    scope="",
    accepted_codecs=(),
    max_duration_seconds=None,
    max_bytes=None,
):
    """Only equivalent trusted CDN aliases coalesce. Retain transforms and versions."""
    original_sha = str(source_sha256 or "").lower()
    source_hash = original_sha if SHA256.fullmatch(original_sha) else ""
    try:
        u = urlsplit(str(source_url or ""))
        host = u.hostname or ""
        usable = (
            u.scheme in ("http", "https")
            and host
            and not u.username
            and not u.password
            and not u.fragment
        )
        if usable and host in SOURCE_HOSTS and not u.port:
            query = [
                (k, v)
                for k, v in parse_qsl(u.query, keep_blank_values=True)
                if k.lower() not in SIGNATURE_KEYS
            ]
            locator = _json([SOURCE_HOSTS[host], u.path, query])
        else:
            locator = str(source_url) if usable else ""
    except (TypeError, ValueError):
        locator = ""
    identity = _digest(_json([scope, locator])) if locator else ""
    return {
        "source_identity": identity,
        "source_sha256": source_hash,
        "scope": str(scope or ""),
        "mode": str(mode or ""),
        "contract": dict(contract or {}),
        "accepted_codecs": sorted(set(accepted_codecs)),
        "max_duration_seconds": _positive(max_duration_seconds),
        "max_bytes": _positive(max_bytes),
    }


def _mode(profile):
    p = str(profile or "").lower().replace("_", "-")
    if "random-overlay" in p:
        return "random_overlay"
    if "direct-outro" in p:
        return "direct_outro"
    if "direct-clean" in p:
        return "direct_clean"
    if "duration-policy" in p:
        return "repair"
    if p.startswith("tt-post-") and ("h264" in p or "hevc" in p):
        return "branded_preview"
    return ""


def context_from_row(channel, family, row):
    """Derive only provable semantics; an unfrozen TT route is filled by its native owner."""
    row = dict(row)
    channel = _platform(channel)
    snap = _obj(row.get("snapshot_json"))
    material = _obj(snap.get("material"))
    profile = row.get("preparation_profile") or row.get("prepared_profile")
    source = row.get("source_media_url") or material.get("media_url")
    mode = _mode(profile)
    contract = {}
    source_sha = ""
    if channel == "fb":
        spec = row.get("video_template") or row.get("processing_mode")
        if spec:
            mode = "random_overlay" if "random" in str(spec) else str(spec)
        if family == "auto" and not mode:
            mode = "random_overlay"
    elif channel == "x":
        source = row.get("original_material_url") or row.get("material_url")
        mime = str(row.get("media_type") or row.get("preflight_mime") or "").lower()
        if mime.startswith("image/") or urlsplit(
            str(source or "")
        ).path.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp")):
            mode = "image"
            return build_context(source, mode=mode)
        source_sha = (
            row.get("media_repair_source_sha256")
            if row.get("original_material_url")
            else row.get("preflight_sha256")
        )
        mode = "random_overlay" if row.get("random_template_enabled") else "repair"
        if row.get("media_repair_job_key"):
            contract["source_repair_profile"] = (
                row.get("media_repair_profile") or "unknown"
            )
    elif channel == "tt":
        material = _obj(_obj(row.get("selection_json")).get("material"))
        source = source or material.get("source_media_url") or material.get("media_url")
        if family == "auto" and not mode:
            profile = _obj(row.get("account_settings_json")).get("preparation_profile")
            mode = _mode(profile)
        trim = _positive(row.get("source_trim_tail_seconds"))
        if trim:
            contract["trim_tail_seconds"] = trim
    codecs = ("h264", "hevc") if channel == "tt" else ("h264",)
    return build_context(
        source,
        mode=mode,
        contract=contract,
        source_sha256=source_sha,
        accepted_codecs=codecs,
    )


def ensure_schema(c):
    # executescript() commits any enclosing SQLite transaction.
    statements = (
        """CREATE TABLE IF NOT EXISTS pipeline_media_context(
             source_key TEXT PRIMARY KEY, channel TEXT NOT NULL, context_json TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS pipeline_media_artifact(
             producer TEXT NOT NULL,sha TEXT NOT NULL,channel TEXT NOT NULL,identity TEXT NOT NULL,
             source_sha TEXT NOT NULL,scope TEXT NOT NULL,mode TEXT NOT NULL,contract TEXT NOT NULL,
             codec TEXT NOT NULL,bytes INTEGER NOT NULL,seconds REAL NOT NULL,
             receipt TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(producer,sha))""",
        "CREATE INDEX IF NOT EXISTS pipeline_media_identity ON pipeline_media_artifact(identity,created_at)",
        "CREATE INDEX IF NOT EXISTS pipeline_media_original_sha ON pipeline_media_artifact(scope,source_sha,created_at)",
        """CREATE TABLE IF NOT EXISTS pipeline_media_use(
             source_key TEXT PRIMARY KEY, channel TEXT NOT NULL,sha TEXT NOT NULL,producer TEXT NOT NULL,kind TEXT NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS pipeline_media_platform_sha ON pipeline_media_use(channel,sha)",
    )
    for statement in statements:
        c.execute(statement)


def register_context(c, source_key, channel, context):
    if not context or not (
        context.get("source_identity") or context.get("source_sha256")
    ):
        return
    prior = c.execute(
        "SELECT context_json FROM pipeline_media_context WHERE source_key=?",
        (source_key,),
    ).fetchone()
    if prior:
        old = _obj(prior[0])
        if old.get("source_identity") == context.get("source_identity"):
            if not context.get("mode") and old.get("mode"):
                context = dict(context, mode=old["mode"], contract=old["contract"])
            if not context.get("source_sha256") and old.get("source_sha256"):
                context = dict(context, source_sha256=old["source_sha256"])
    c.execute(
        """INSERT INTO pipeline_media_context VALUES(?,?,?) ON CONFLICT(source_key) DO UPDATE
                 SET channel=excluded.channel,context_json=excluded.context_json
                 WHERE pipeline_media_context.context_json<>excluded.context_json""",
        (source_key, _platform(channel), _json(context)),
    )


def _codec(artifact):
    p = str(artifact.get("profile") or "").lower()
    codec = str(
        _obj(artifact.get("probe")).get("codec") or artifact.get("codec") or ""
    ).lower()
    if codec in ("hevc", "h265") or "hevc" in p:
        return "hevc"
    if codec in ("h264", "avc1") or "h264" in p or "x264" in p:
        return "h264"
    return codec


def _receipt(artifact):
    """Keep file proof and media metadata, excluding task-bound credentials and sessions."""
    keys = (
        "url",
        "sha256",
        "size",
        "duration_seconds",
        "mime",
        "profile",
        "width",
        "height",
        "probe",
        "recipe",
        "random_overlay_recipe",
        "assets",
        "backend",
        "source_trim_tail_seconds",
        "source_sha256",
        "source_size",
        "identity_mode",
        "storage_key",
        "execution_profile",
    )
    a = {k: artifact[k] for k in keys if k in artifact}
    return a


def record_artifact(c, source_key, channel, artifact, now):
    if not isinstance(artifact, dict):
        return False
    sha = str(artifact.get("sha256") or "")
    mode = _mode(artifact.get("profile"))
    if (
        not mode
        or not SHA256.fullmatch(sha)
        or artifact.get("mime") != "video/mp4"
        or not _positive(artifact.get("size"))
        or not _positive(artifact.get("duration_seconds"))
    ):
        return False
    try:
        u = urlsplit(artifact.get("url") or "")
        if (
            u.scheme != "https"
            or not u.hostname
            or u.username
            or u.password
            or u.query
            or u.fragment
        ):
            return False
    except ValueError:
        return False
    channel = _platform(channel)
    previous = c.execute(
        "SELECT producer FROM pipeline_media_use WHERE source_key=?", (source_key,)
    ).fetchone()
    if previous and previous[0] != source_key:
        return False
    row = c.execute(
        "SELECT context_json FROM pipeline_media_context WHERE source_key=? AND channel=?",
        (source_key, channel),
    ).fetchone()
    if not row:
        return False
    ctx = _obj(row[0])
    source_sha = str(artifact.get("source_sha256") or ctx.get("source_sha256") or "")
    if source_sha and not SHA256.fullmatch(source_sha):
        return False
    if ctx.get("source_sha256") and ctx["source_sha256"] != source_sha:
        return False
    contract = dict(ctx.get("contract") or {})
    trim = _positive(artifact.get("source_trim_tail_seconds"))
    if trim:
        contract["trim_tail_seconds"] = trim
    codec = _codec(artifact)
    params = (
        source_key,
        sha,
        channel,
        ctx["source_identity"],
        source_sha,
        ctx.get("scope", ""),
        mode,
        _json(contract),
        codec,
        int(artifact["size"]),
        float(artifact["duration_seconds"]),
        _json(_receipt(artifact)),
        now,
    )
    c.execute(
        "INSERT INTO pipeline_media_artifact VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(producer,sha) DO NOTHING",
        params,
    )
    c.execute(
        "INSERT OR IGNORE INTO pipeline_media_use VALUES(?,?,?,?,?)",
        (source_key, channel, sha, source_key, "produced"),
    )
    return True


def select(c, job, *, at_cutoff, now, excluded=()):
    """Select and reserve under BEGIN IMMEDIATE; the target owner verifies the actual object."""
    key, channel = job["source_key"], _platform(job["channel"])
    row = c.execute(
        "SELECT context_json FROM pipeline_media_context WHERE source_key=? AND channel=?",
        (key, channel),
    ).fetchone()
    ctx = _obj(row[0]) if row else {}
    if ctx.get("mode") == "image":
        return None
    identity = ctx.get("source_identity", "")
    source_sha = ctx.get("source_sha256", "")
    fallback = (
        {
            "kind": "deadline_original",
            "producer_source_key": "",
            "artifact": {},
            "source_identity": identity,
        }
        if at_cutoff
        else None
    )
    if not (identity or source_sha):
        return fallback
    prior = c.execute(
        """SELECT u.kind,u.producer,a.receipt FROM pipeline_media_use u JOIN pipeline_media_artifact a
                       ON a.producer=u.producer AND a.sha=u.sha WHERE u.source_key=?""",
        (key,),
    ).fetchone()
    if prior:
        if prior[0] == "produced":
            return None
        return {
            "kind": prior[0],
            "producer_source_key": prior[1],
            "artifact": _obj(prior[2]),
            "source_identity": identity,
        }
    filters = [
        "a.scope=?",
        "((a.identity=? AND ?<>'') OR (a.source_sha=? AND ?<>''))",
        "(a.source_sha='' OR ?='' OR a.source_sha=?)",
    ]
    values = [
        ctx.get("scope", ""),
        identity,
        identity,
        source_sha,
        source_sha,
        source_sha,
        source_sha,
    ]
    used = (
        "EXISTS(SELECT 1 FROM pipeline_media_use u WHERE u.channel=? AND u.sha=a.sha)"
    )
    if at_cutoff:
        filters.append("(a.channel=? OR " + used + ")")
        values.extend([channel, channel])
    else:
        filters.extend(["a.mode=?", "a.contract=?", "NOT " + used])
        values.extend([ctx.get("mode", ""), _json(ctx.get("contract") or {}), channel])
    if ctx.get("accepted_codecs"):
        codecs = ctx["accepted_codecs"]
        filters.append("a.codec IN (" + ",".join("?" for _ in codecs) + ")")
        values.extend(codecs)
    for name, column in (("max_bytes", "bytes"), ("max_duration_seconds", "seconds")):
        if ctx.get(name):
            filters.append("a." + column + "<=?")
            values.append(ctx[name])
    denied = tuple(dict.fromkeys(excluded or ()))
    if denied:
        filters.append("a.sha NOT IN (" + ",".join("?" for _ in denied) + ")")
        values.extend(denied)
    row = c.execute(
        "SELECT a.producer,a.sha,a.receipt FROM pipeline_media_artifact a WHERE "
        + " AND ".join(filters)
        + " ORDER BY a.created_at DESC,a.producer DESC LIMIT 1",
        values,
    ).fetchone()
    if not row:
        return fallback
    kind = "deadline_reuse" if at_cutoff else "cross_platform_reuse"
    c.execute(
        "INSERT INTO pipeline_media_use VALUES(?,?,?,?,?)",
        (key, channel, row[1], row[0], kind),
    )
    return {
        "kind": kind,
        "producer_source_key": row[0],
        "artifact": _obj(row[2]),
        "source_identity": identity,
    }


def release(c, source_key):
    c.execute(
        "DELETE FROM pipeline_media_use WHERE source_key=? AND kind<>'produced'",
        (source_key,),
    )


def fb_video_appointment(row, batch):
    """Immediate rows also have planned_at_utc. Read the operator's actual timing choice."""
    if row.get("source_kind") in {"pool", "group_pool"}:
        return row.get("planned_at_utc")
    request = _obj(batch.get("request_json"))
    if request.get("schema_version") == 3:
        number = _obj(row.get("snapshot_json")).get("row_number")
        scheduled = any(
            p.get("row_number") == number and p.get("scheduled_at_utc")
            for p in request.get("pairs", [])
            if isinstance(p, dict)
        )
    elif request.get("schema_version") == 2:
        scheduled = _obj(request.get("material_schedules")).get(
            str(row.get("material_id"))
        )
    else:
        scheduled = request.get("schedule_mode") == "scheduled"
    return row.get("planned_at_utc") if scheduled else ""
