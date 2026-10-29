"""Versioned editing contract. Transport credentials never define media identity."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from features.drama_synthesis.core import DramaSynthesisError

TASK_TYPE = "video_editing_v1"
LEGACY_PROFILE = "video-editing-720-v1"
PROFILE = "video-editing-720-v2"
FPS = 30
FADE_FRAMES = 9
# Numeric safety bound, not a social-publishing duration policy. Actual admission
# is limited by the available temporary-storage budget and upload size.
MAX_DURATION_MS = 2_147_483_647
JOB_RE = re.compile(r"ve_[0-9a-f]{32}\Z")
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
SHA_RE = re.compile(r"[0-9a-f]{64}\Z")
SIGNATURE_FIELDS = frozenset({"q-sign-algorithm", "q-ak", "q-sign-time", "q-key-time",
    "q-header-list", "q-url-param-list", "q-signature", "x-cos-security-token"})


def error(code="video_editing_recipe_invalid", message="剪辑参数无效，请检查片段范围和过渡", status=400):
    return DramaSynthesisError(code, message, status)


def integer(value, low=0, high=MAX_DURATION_MS):
    if type(value) is not int or not low <= value <= high:
        raise error()
    return value


def text(value, limit=4096):
    if not isinstance(value, str) or not value or len(value) > limit or any(ord(c) < 32 for c in value):
        raise error()
    return value


def canonical_json(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        raise error() from None


def source_locator(url):
    parsed = urlsplit(text(url, 16384))
    try:
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment or parsed.port not in (None, 443):
            raise error()
    except ValueError:
        raise error() from None
    query = urlencode([(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
                       if k.lower() not in SIGNATURE_FIELDS])
    return urlunsplit(("https", parsed.netloc.lower(), parsed.path or "/", query, ""))


def normalize_identity(value, *, kind):
    if not isinstance(value, dict):
        raise error()
    result = {"size_bytes": integer(value.get("size_bytes"), 1, 8 * 1024**3)}
    etag = value.get("etag", "")
    sha = value.get("sha256", "")
    if etag:
        etag = text(etag, 256).strip('"')
        if etag.startswith("W/") or '"' in etag:
            raise error()
        result["etag"] = etag
    if sha:
        if not isinstance(sha, str) or not SHA_RE.fullmatch(sha):
            raise error()
        result["sha256"] = sha
    if not etag and not sha:
        raise error("video_editing_source_identity_missing", "素材缺少可验证的版本信息")
    for field in ("bucket", "key", "version_id"):
        if value.get(field):
            result[field] = text(value[field], 4096 if field == "key" else 256)
    if kind == "asset" and not all(result.get(k) for k in ("bucket", "key")):
        raise error()
    if bool(result.get("bucket")) != bool(result.get("key")):
        raise error()
    return result


def ms_to_frame(value):
    """Nearest 30fps frame, ties upward; applied exactly once during compilation."""
    return (integer(value) * FPS + 500) // 1000


def validate_recipe(recipe, operation="render"):
    if operation not in {"probe", "render"} or not isinstance(recipe, dict):
        raise error()
    profile = recipe.get("profile")
    if recipe.get("version") != 1 or type(recipe.get("version")) is not int or profile not in (LEGACY_PROFILE, PROFILE):
        raise error()
    raw_sources, raw_clips = recipe.get("sources"), recipe.get("clips")
    if not isinstance(raw_sources, list) or not 1 <= len(raw_sources) <= 128 or not isinstance(raw_clips, list):
        raise error()
    sources, seen = [], set()
    for source in raw_sources:
        if not isinstance(source, dict) or not ID_RE.fullmatch(str(source.get("source_id", ""))):
            raise error()
        sid, kind = source["source_id"], source.get("kind")
        if sid in seen or kind not in {"asset", "episode"}:
            raise error()
        seen.add(sid)
        source_locator(source.get("url"))
        row = {"source_id": sid, "kind": kind, "url": source["url"],
               "identity": normalize_identity(source.get("identity"), kind=kind)}
        if operation == "render":
            row["duration_ms"] = integer(source.get("duration_ms"), 1)
            row["width"] = integer(source.get("width"), 1, 16384)
            row["height"] = integer(source.get("height"), 1, 16384)
            if type(source.get("has_audio")) is not bool:
                raise error()
            row["has_audio"] = source["has_audio"]
        sources.append(row)
    if operation == "probe":
        if len(sources) != 1 or raw_clips:
            raise error()
        return {"version": 1, "profile": profile, "sources": sources, "clips": []}
    if not 1 <= len(raw_clips) <= 512:
        raise error()
    lookup, clips, clip_ids = {s["source_id"]: s for s in sources}, [], set()
    for clip in raw_clips:
        if not isinstance(clip, dict) or not ID_RE.fullmatch(str(clip.get("id", ""))):
            raise error()
        sid, cid = clip.get("source_id"), clip["id"]
        if sid not in lookup or cid in clip_ids:
            raise error()
        clip_ids.add(cid)
        start = integer(clip.get("start_ms"))
        end = integer(clip.get("end_ms"), 1, lookup[sid]["duration_ms"])
        start_frame, end_frame = ms_to_frame(start), ms_to_frame(end)
        if end <= start or end_frame <= start_frame:
            raise error("video_editing_clip_too_short", "保留片段必须至少包含一帧")
        fit, transition, mute = clip.get("fit", "contain"), clip.get("transition", "cut"), clip.get("mute", False)
        if fit not in {"contain", "cover"} or transition not in {"cut", "fade"} or type(mute) is not bool:
            raise error()
        clips.append({"id": cid, "source_id": sid, "start_ms": start, "end_ms": end,
                      "start_frame": start_frame, "end_frame": end_frame,
                      "fit": fit, "mute": mute, "transition": transition})
    if clips[-1]["transition"] != "cut":
        raise error("video_editing_last_transition", "最后一个片段不能设置过渡")
    for index, clip in enumerate(clips):
        incoming = FADE_FRAMES if index and clips[index - 1]["transition"] == "fade" else 0
        outgoing = FADE_FRAMES if clip["transition"] == "fade" else 0
        if clip["end_frame"] - clip["start_frame"] < incoming + outgoing:
            raise error("video_editing_transition_too_long", "片段 %s 长度不足以容纳两侧0.3秒过渡" % clip["id"])
    recipe = {"version": 1, "profile": profile, "sources": sources, "clips": clips}
    if output_frames(recipe) > MAX_DURATION_MS * FPS // 1000:
        raise error("video_editing_duration_limit", "成片时长超出剪辑参数可表示范围")
    return recipe


def output_frames(recipe):
    return sum(c["end_frame"] - c["start_frame"] - (FADE_FRAMES if c["transition"] == "fade" else 0)
               for c in recipe["clips"])


def canonical_recipe(recipe, operation="render"):
    normalized = validate_recipe(recipe, operation)
    result = deepcopy(normalized)
    for source in result["sources"]:
        url = source.pop("url")
        if not source["identity"].get("bucket"):
            source["locator"] = source_locator(url)
    return result


def recipe_fingerprint(recipe, operation="render"):
    return hashlib.sha256(canonical_json({"operation": operation, "recipe": canonical_recipe(recipe, operation)}).encode()).hexdigest()


def validate_payload(payload):
    if not isinstance(payload, dict) or payload.get("task_type") != TASK_TYPE or not JOB_RE.fullmatch(str(payload.get("job_id", ""))):
        raise error()
    operation = payload.get("operation")
    return {"job_id": payload["job_id"], "task_type": TASK_TYPE, "operation": operation,
            "recipe": validate_recipe(payload.get("recipe"), operation)}


def payload_fingerprint(payload):
    value = validate_payload(payload)
    value["recipe"] = canonical_recipe(value["recipe"], value["operation"])
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()
