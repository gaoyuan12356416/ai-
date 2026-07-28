"""Read-only Dramawave episode selection for scheduled X Posts.

The selector treats ``ads_drama_resource`` as an external read-only source.
Each configured account receives exactly one pool drama selected by the X
sidecar's durable account affinity.  One pool drama is audited as a complete
snapshot before its next free episode can become a queue candidate.  A missing
episode, ambiguous URL or metadata drift blocks that drama instead of silently
advancing to the next one.
"""

from __future__ import annotations

import re
import unicodedata
import urllib.parse
from datetime import datetime, timezone


DEFAULT_SCHEMA = "kunlunads_dev"
DRAMAWAVE_APP_ID = 1479
DRAMA_RESOURCE_TYPE = 2
MAX_POOL_ITEMS = 1000
MAX_EPISODES_PER_SELECTION = 50

_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9_]+$")
_CONTENT_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_RESOURCE_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


class DramaSelectionError(RuntimeError):
    """The requested drama batch cannot be selected safely."""

    code = "x_post_drama_selection_failed"


class DramaQueryError(DramaSelectionError):
    """The read-only source query failed; the complete run must stop."""

    code = "x_post_drama_query_failed"


class DramaPoolRejection(DramaSelectionError):
    """The current FIFO drama is incomplete or ambiguous."""

    def __init__(self, code, message, pool_item_id=None, content_id=""):
        self.code = str(code or "x_post_drama_invalid")[:64]
        self.pool_item_id = pool_item_id
        self.content_id = str(content_id or "")
        super().__init__(str(message or "drama is not publishable")[:240])


def _text(
    value,
    label,
    *,
    required=True,
    limit=4096,
    normalize_whitespace=False,
):
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise DramaSelectionError("%s is not valid UTF-8" % label) from None
    if value is None:
        value = ""
    if not isinstance(value, str):
        value = str(value)
    value = (
        re.sub(r"\s+", " ", value).strip()
        if normalize_whitespace
        else value.strip()
    )
    if (
        (required and not value)
        or len(value) > int(limit)
        or any(ord(char) < 32 for char in value)
    ):
        raise DramaSelectionError("%s is incomplete" % label)
    return value


def _positive_int(value, label, *, maximum=9223372036854775807):
    if isinstance(value, bool):
        raise DramaSelectionError("%s is invalid" % label)
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        raise DramaSelectionError("%s is invalid" % label) from None
    if parsed <= 0 or parsed > int(maximum):
        raise DramaSelectionError("%s is invalid" % label)
    return parsed


def _nonnegative_int(value, label, *, maximum=9223372036854775807):
    if isinstance(value, bool):
        raise DramaSelectionError("%s is invalid" % label)
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        raise DramaSelectionError("%s is invalid" % label) from None
    if parsed < 0 or parsed > int(maximum):
        raise DramaSelectionError("%s is invalid" % label)
    return parsed


def _cursor_rows(connection, sql, params):
    if not re.match(r"(?i)^SELECT\b", str(sql or "").lstrip()):
        raise DramaSelectionError("selector attempted a non-read-only statement")
    cursor = connection.cursor()
    try:
        cursor.execute(sql, tuple(params))
        rows = cursor.fetchall()
        return [dict(row) for row in rows]
    except DramaSelectionError:
        raise
    except Exception as exc:
        raise DramaQueryError(
            "read-only drama query failed: %s" % type(exc).__name__
        ) from None
    finally:
        close = getattr(cursor, "close", None)
        if callable(close):
            close()


def _normalize_media_url(value):
    raw = _text(value, "episode URL", limit=4096)
    parsed = urllib.parse.urlsplit(raw)
    try:
        port = parsed.port
    except ValueError:
        raise DramaSelectionError("episode URL port is invalid") from None
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or port not in {None, 80, 443}
    ):
        raise DramaSelectionError("episode URL is not a safe absolute HTTP URL")
    scheme = "https"
    netloc = parsed.hostname.lower()
    if port not in {None, 80, 443}:
        netloc += ":%s" % port
    return urllib.parse.urlunsplit(
        (scheme, netloc, parsed.path, parsed.query, "")
    )


def _label_values(raw_labels):
    labels = _text(
        raw_labels,
        "drama labels",
        required=False,
        limit=512,
    )
    seen = set()
    values = []
    for raw in re.split(r"[,，;；|/]+", labels):
        value = re.sub(r"\s+", " ", raw).strip().lstrip("#")
        key = value.casefold()
        if not value or key in seen:
            continue
        seen.add(key)
        values.append(value)
    return values


def _hashtag(value):
    normalized = unicodedata.normalize("NFKC", str(value or "")).strip()
    normalized = re.sub(r"[\s-]+", "_", normalized)
    rendered = "".join(
        char for char in normalized if char == "_" or char.isalnum()
    ).strip("_")
    return rendered[:50]


def build_name_tag(drama_name, drama_labels):
    """Return a deterministic drama hashtag plus at most two label hashtags."""
    name = _text(drama_name, "drama name", limit=255)
    tags = []
    for label in [name] + _label_values(drama_labels):
        tag = _hashtag(label)
        if tag and tag.casefold() not in {item.casefold() for item in tags}:
            tags.append(tag)
        if len(tags) == 3:
            break
    if not tags:
        raise DramaSelectionError("drama name cannot form a hashtag")
    return " ".join("#" + tag for tag in tags)


def _created_order(value, pool_item_id):
    text = _text(value, "pool created_at", limit=64)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise DramaSelectionError("pool created_at is invalid") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).timestamp(), int(pool_item_id)


class DramawaveDramaSelector:
    """Audit and expand FIFO drama-pool rows into immutable episode candidates."""

    def __init__(
        self,
        connection,
        *,
        schema=DEFAULT_SCHEMA,
        app_id=DRAMAWAVE_APP_ID,
        resource_type=DRAMA_RESOURCE_TYPE,
    ):
        if not _SAFE_IDENTIFIER.fullmatch(str(schema or "")):
            raise DramaSelectionError("invalid MySQL schema")
        self.connection = connection
        self.schema = str(schema)
        self.app_id = _positive_int(app_id, "app_id")
        self.resource_type = _positive_int(resource_type, "resource type")

    def _rows(self, content_id):
        sql = """
            SELECT
                CAST(r.id AS CHAR) AS resource_id,
                CAST(r.app_id AS CHAR) AS app_id,
                r.app,
                r.content_id,
                r.name AS drama_name,
                r.desc AS drama_description,
                r.labels AS drama_labels,
                r.country,
                r.language,
                r.series_code,
                r.data_origin,
                r.unlocked_episodes_count,
                r.sub_number,
                r.sub_name,
                r.sub_url
              FROM `{schema}`.ads_drama_resource r FORCE INDEX (content_id)
             WHERE r.app_id = %s
               AND r.type = %s
               AND r.content_id = %s
             ORDER BY r.sub_number ASC, r.id ASC
        """.format(schema=self.schema)
        return _cursor_rows(
            self.connection,
            sql,
            (self.app_id, self.resource_type, content_id),
        )

    def audit(self, content_id):
        content_id = _text(content_id, "content_id", limit=128)
        if not _CONTENT_ID.fullmatch(content_id):
            raise DramaPoolRejection(
                "drama_id_invalid", "drama content_id is invalid", content_id=content_id
            )
        rows = self._rows(content_id)
        if not rows:
            raise DramaPoolRejection(
                "drama_not_found",
                "no Dramawave episode rows were found",
                content_id=content_id,
            )

        normalized_rows = []
        metadata_values = set()
        app_values = set()
        unlocked_values = []
        for row in rows:
            try:
                resource_id = _text(row.get("resource_id"), "resource id", limit=128)
                if not _RESOURCE_ID.fullmatch(resource_id):
                    raise DramaSelectionError("resource id is invalid")
                row_content_id = _text(row.get("content_id"), "content_id", limit=128)
                if row_content_id != content_id:
                    raise DramaSelectionError("content identity mismatch")
                row_app_id = _positive_int(row.get("app_id"), "app_id")
                if row_app_id != self.app_id:
                    raise DramaSelectionError("app identity mismatch")
                sub_number = _nonnegative_int(
                    row.get("sub_number"),
                    "sub_number",
                )
                # Some Dramawave titles include one platform-level metadata row
                # with sub_number=0 for each client app. These rows are not
                # publishable episodes and must not participate in episode or
                # drama-metadata validation.
                if sub_number == 0:
                    continue
                unlocked = _nonnegative_int(
                    row.get("unlocked_episodes_count"),
                    "unlocked_episodes_count",
                )
                drama_name = _text(row.get("drama_name"), "drama name", limit=255)
                description = _text(
                    row.get("drama_description"),
                    "drama description",
                    limit=10000,
                    normalize_whitespace=True,
                )
                labels = _text(
                    row.get("drama_labels"),
                    "drama labels",
                    required=False,
                    limit=512,
                )
                language = _text(row.get("language"), "language", limit=32)
                app = _text(row.get("app"), "app", limit=64)
                country = _text(row.get("country"), "country", limit=16)
                series_code = _text(
                    row.get("series_code"), "series_code", required=False, limit=64
                )
                data_origin = _nonnegative_int(row.get("data_origin"), "data_origin")
                sub_name = _text(
                    row.get("sub_name"),
                    "episode name",
                    required=False,
                    limit=255,
                )
                media_url = _normalize_media_url(row.get("sub_url"))
            except DramaSelectionError as exc:
                raise DramaPoolRejection(
                    "drama_resource_invalid",
                    str(exc),
                    content_id=content_id,
                ) from None
            metadata_values.add(
                (
                    drama_name,
                    description,
                    labels,
                    language,
                    country,
                    series_code,
                    data_origin,
                )
            )
            app_values.add(app)
            unlocked_values.append(unlocked)
            normalized_rows.append(
                {
                    "resource_id": resource_id,
                    "content_id": content_id,
                    "sub_number": sub_number,
                    "sub_name": sub_name,
                    "material_url": media_url,
                    "unlocked_episodes_count": unlocked,
                    "drama_name": drama_name,
                    "description": description,
                    "labels": labels,
                    "language": language,
                    "app": app,
                    "country": country,
                    "series_code": series_code,
                    "data_origin": data_origin,
                }
            )

        if not normalized_rows:
            raise DramaPoolRejection(
                "drama_resource_invalid",
                "no positive episode rows were found",
                content_id=content_id,
            )
        if len(metadata_values) != 1:
            raise DramaPoolRejection(
                "drama_metadata_ambiguous",
                "drama metadata is inconsistent across episode rows",
                content_id=content_id,
            )
        free_count = min(unlocked_values)
        if free_count < 1:
            raise DramaPoolRejection(
                "drama_no_free_episodes",
                "drama has no free episodes",
                content_id=content_id,
            )

        by_episode = {}
        for row in normalized_rows:
            if row["sub_number"] > free_count:
                continue
            by_episode.setdefault(row["sub_number"], []).append(row)
        expected_numbers = list(range(1, free_count + 1))
        if sorted(by_episode) != expected_numbers:
            raise DramaPoolRejection(
                "drama_episode_gap",
                "free episode numbers must be continuous from 1",
                content_id=content_id,
            )

        episodes = []
        for sub_number in expected_numbers:
            episode_rows = by_episode[sub_number]
            urls = {row["material_url"] for row in episode_rows}
            if len(urls) != 1:
                raise DramaPoolRejection(
                    "drama_episode_url_ambiguous",
                    "episode %s has multiple media URLs" % sub_number,
                    content_id=content_id,
                )
            # Identical duplicate source rows do not change the publish target.
            # Freeze the first stable resource identity from the ordered query.
            selected = episode_rows[0]
            episodes.append(
                {
                    "resource_id": selected["resource_id"],
                    "sub_number": sub_number,
                    "sub_name": selected["sub_name"],
                    "material_url": selected["material_url"],
                }
            )

        (
            drama_name,
            description,
            labels,
            language,
            country,
            series_code,
            data_origin,
        ) = next(iter(metadata_values))
        # iOS and Android rows may describe the same episode snapshot. The app
        # value is platform attribution, not drama identity; return one stable
        # representative while the per-episode URL checks above stay strict.
        app = sorted(app_values, key=lambda value: (value.casefold(), value))[0]
        return {
            "content_id": content_id,
            "app_id": self.app_id,
            "app": app,
            "country": country,
            "language": language,
            "series_code": series_code,
            "data_origin": data_origin,
            "drama_name": drama_name,
            "description": description,
            "labels": labels,
            "name_tag": build_name_tag(drama_name, labels),
            "free_episode_count": free_count,
            "episodes": episodes,
        }

    def select_pool(self, pool_items, *, account_ids):
        if not isinstance(pool_items, list) or not 1 <= len(pool_items) <= MAX_POOL_ITEMS:
            raise DramaSelectionError("drama pool response must contain 1..1000 items")
        if (
            not isinstance(account_ids, (list, tuple))
            or not 1 <= len(account_ids) <= MAX_EPISODES_PER_SELECTION
        ):
            raise DramaSelectionError("account_ids is invalid")
        normalized_accounts = []
        seen_accounts = set()
        for raw_account_id in account_ids:
            account_id = _positive_int(raw_account_id, "account_id")
            if account_id in seen_accounts:
                raise DramaSelectionError("account_ids is invalid")
            seen_accounts.add(account_id)
            normalized_accounts.append(account_id)
        if len(pool_items) > len(normalized_accounts):
            raise DramaSelectionError(
                "drama pool response exceeds the configured account scope"
            )
        normalized_pool = []
        seen_ids = set()
        seen_content_ids = set()
        for index, raw in enumerate(pool_items):
            if not isinstance(raw, dict):
                raise DramaSelectionError("drama pool item must be an object")
            pool_item_id = _positive_int(raw.get("id"), "drama pool item id")
            content_id = str(
                raw.get("content_id")
                or raw.get("drama_id")
                or ""
            ).strip()
            if (
                not _CONTENT_ID.fullmatch(content_id)
                or pool_item_id in seen_ids
                or content_id in seen_content_ids
            ):
                raise DramaSelectionError("drama pool identity is invalid")
            created_at = _text(raw.get("created_at"), "pool created_at", limit=64)
            _created_order(created_at, pool_item_id)
            next_sub_number = _positive_int(
                raw.get("next_sub_number", 1),
                "next_sub_number",
            )
            assigned_account_id = _nonnegative_int(
                raw.get("assigned_account_id"),
                "assigned_account_id",
            )
            candidate_account_id = _positive_int(
                raw.get("candidate_account_id"),
                "candidate_account_id",
            )
            if (
                candidate_account_id != normalized_accounts[index]
                or assigned_account_id not in (0, candidate_account_id)
            ):
                raise DramaSelectionError(
                    "drama pool account assignment is invalid"
                )
            assigned_at = _text(
                raw.get("assigned_at"),
                "assigned_at",
                required=False,
                limit=64,
            )
            raw_source_queue_id = raw.get("assigned_source_queue_id")
            assigned_source_queue_id = (
                _positive_int(
                    raw_source_queue_id,
                    "assigned_source_queue_id",
                )
                if raw_source_queue_id not in (None, "")
                else None
            )
            if assigned_account_id > 0:
                if not assigned_at or assigned_source_queue_id is None:
                    raise DramaSelectionError(
                        "drama pool assignment evidence is incomplete"
                    )
            elif assigned_at or assigned_source_queue_id is not None:
                raise DramaSelectionError(
                    "unassigned drama contains assignment evidence"
                )
            seen_ids.add(pool_item_id)
            seen_content_ids.add(content_id)
            normalized_pool.append(
                {
                    "id": pool_item_id,
                    "content_id": content_id,
                    "created_at": created_at,
                    "next_sub_number": next_sub_number,
                    "assigned_account_id": assigned_account_id,
                    "candidate_account_id": candidate_account_id,
                }
            )

        selected_candidates = []
        for pool in normalized_pool:
            try:
                audit = self.audit(pool["content_id"])
            except DramaPoolRejection as exc:
                raise DramaPoolRejection(
                    exc.code,
                    str(exc),
                    pool["id"],
                    pool["content_id"],
                ) from None
            labels = _label_values(audit["labels"])
            primary_tag = labels[0] if labels else audit["drama_name"]
            start = pool["next_sub_number"]
            if start > audit["free_episode_count"] + 1:
                raise DramaPoolRejection(
                    "drama_progress_invalid",
                    "next episode exceeds the frozen free episode range",
                    pool["id"],
                    pool["content_id"],
                )
            if start == audit["free_episode_count"] + 1:
                continue
            episode = audit["episodes"][start - 1]
            sub_number = episode["sub_number"]
            material_name = episode["sub_name"] or (
                "%s Episode %s" % (audit["drama_name"], sub_number)
            )
            selected_candidates.append(
                {
                    "source_type": "drama",
                    "drama_pool_item_id": pool["id"],
                    "drama_pool_created_at": pool["created_at"],
                    "pool_item_id": None,
                    "pool_created_at": "",
                    "episode_number": sub_number,
                    "sub_num": sub_number,
                    "episode_key": "%s:%s" % (audit["content_id"], sub_number),
                    "material_key": "",
                    "material_id": episode["resource_id"],
                    "content_id": audit["content_id"],
                    "material_url": episode["material_url"],
                    "material_name": material_name,
                    "material_language": audit["language"],
                    "drama_name": audit["drama_name"],
                    # Match the existing W2A attribution contract: use the
                    # first label, or the drama name when unlabelled.
                    "tag": primary_tag,
                    "name_tag": audit["name_tag"],
                    "description": audit["description"],
                    "free_episode_count": audit["free_episode_count"],
                    "assigned_account_id": pool["assigned_account_id"],
                    "candidate_account_id": pool["candidate_account_id"],
                    "spend": 0,
                    "facebook_violation_count": 0,
                    "tiktok_violation_count": 0,
                    "twitter_violation_count": 0,
                    "resource_audit_count": 0,
                    "dangerous_tag_count": 0,
                }
            )
        return selected_candidates


def audit_drama(
    connection,
    content_id,
    *,
    schema=DEFAULT_SCHEMA,
    app_id=DRAMAWAVE_APP_ID,
):
    return DramawaveDramaSelector(
        connection,
        schema=schema,
        app_id=app_id,
    ).audit(content_id)


def select_drama_pool_episodes(
    connection,
    pool_items,
    *,
    account_ids,
    schema=DEFAULT_SCHEMA,
    app_id=DRAMAWAVE_APP_ID,
):
    return DramawaveDramaSelector(
        connection,
        schema=schema,
        app_id=app_id,
    ).select_pool(pool_items, account_ids=account_ids)
