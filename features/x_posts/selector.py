"""Read-only Dramawave candidate selection for the daily X publisher.

The selector deliberately keeps the reporting database outside the publishing
transaction.  It only returns candidates whose source metadata, four violation
stores, material tags and drama mapping can all be checked unambiguously.
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime, timedelta, timezone


SHANGHAI_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
DEFAULT_SCHEMA = "kunlunads_dev"
DEFAULT_PRODUCT = "Dramawave"

_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9_]+$")
_MATERIAL_ID = re.compile(r"^[0-9]+$")
_ENGLISH_DANGER = re.compile(
    r"\b(?:"
    r"porn(?:ography|ographic|o)?s?|nudes?|nudity|naked(?:ness)?|nsfw|adults?|"
    r"erotic(?:a|ism)?|sex(?:ual(?:ly)?|y|es)?|explicit(?:ly)?|"
    r"rap(?:e[sd]?|ing)|rapists?|bdsm|hentai|xxx|fetish(?:es)?|"
    r"org(?:y|ies)|prostitut(?:e[sd]?|ing|ion)|brothels?|"
    r"strip(?:per(?:s)?|ping)|lingerie|blood(?:y|shed)?|gore|gory|"
    r"incest(?:uous)?|violen(?:ce|t(?:ly)?)|weapons?|weaponry|"
    r"guns?|gunfire|firearms?|pistols?|rifles?|shotguns?|"
    r"ammunition|ammo|bullets?|shoot(?:s|ing|er(?:s)?)?|"
    r"kni(?:fe|ves)|stabb(?:ed|ing)|tortur(?:e[sd]?|ing)|"
    r"beating|beaten|bleed(?:s|ing)?|brutal(?:ity|ly)?|dismember(?:ed|ing|ment)?|"
    r"murder(?:s|ed|ing|er(?:s)?|ous)?|suicid(?:e[sd]?|al)|"
    r"abus(?:e[sd]?|ing|ive)|assault(?:s|ed|ing)?|"
    r"kill(?:s|ed|ing|er(?:s)?)?|deaths?"
    r")\b",
    re.IGNORECASE,
)
_AGE_RATING_DANGER = re.compile(r"(?:\br[\s_-]*18\b|\b18\s*\+)", re.IGNORECASE)
_CJK_DANGER = (
    "色情",
    "暴力",
    "情色",
    "涉黄",
    "成人内容",
    "成人影片",
    "成人向",
    "18禁",
    "血腥",
    "流血",
    "裸",
    "强奸",
    "性侵",
    "乱伦",
    "虐待",
    "殴打",
    "折磨",
    "拷打",
    "自杀",
    "杀人",
    "凶杀",
    "武器",
    "枪支",
    "枪械",
    "枪击",
    "射击",
    "刀具",
)


class CandidateSelectionError(RuntimeError):
    """The source could not be checked safely enough to publish."""


class CandidateQueryError(CandidateSelectionError):
    """A read-only database query failed; the entire run must stop."""


class PoolCandidateRejection(CandidateSelectionError):
    """A single pool item is unsafe or incomplete and must be skipped."""

    def __init__(self, error_code, error_message):
        super().__init__(error_message)
        self.error_code = str(error_code)
        self.error_message = str(error_message)


def shanghai_now(now=None):
    """Return an aware Asia/Shanghai timestamp without requiring zoneinfo data."""
    if now is None:
        return datetime.now(timezone.utc).astimezone(SHANGHAI_TZ)
    if not isinstance(now, datetime):
        raise TypeError("now must be a datetime")
    if now.tzinfo is None:
        now = now.replace(tzinfo=SHANGHAI_TZ)
    return now.astimezone(SHANGHAI_TZ)


def previous_source_date(now=None):
    return (shanghai_now(now).date() - timedelta(days=1)).isoformat()


def normalize_date(value, label="source_date"):
    if isinstance(value, date):
        parsed = value
    else:
        try:
            parsed = datetime.strptime(str(value or ""), "%Y-%m-%d").date()
        except (TypeError, ValueError):
            raise CandidateSelectionError("%s must be YYYY-MM-DD" % label) from None
    return parsed.isoformat()


def material_key(material_id):
    value = str(material_id or "").strip()
    if not _MATERIAL_ID.fullmatch(value):
        raise CandidateSelectionError("invalid material id")
    # The ledger's global key is the canonical positive decimal material ID.
    # Dramawave is the only eligible product, so a product prefix would make
    # the runner disagree with the store migration and its unique index.
    parsed = int(value)
    if parsed <= 0 or parsed > 9223372036854775807:
        raise CandidateSelectionError("invalid material id")
    return str(parsed)


def normalize_material_url(value):
    """Upgrade an absolute HTTP material URL to HTTPS without changing its target."""
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    value = value.strip()
    scheme = value[:8].lower()
    if scheme.startswith("http://"):
        return "https://" + value[7:]
    if scheme == "https://":
        return "https://" + value[8:]
    return value


def contains_dangerous_tag(value):
    """Fail closed for undecodable tag values; otherwise apply the shared lexicon."""
    if value is None:
        raise CandidateSelectionError("tag value is null")
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise CandidateSelectionError("tag value is not valid UTF-8") from None
    if not isinstance(value, str):
        raise CandidateSelectionError("tag value is not text")
    normalized = value.strip()
    # Tags commonly use underscores (for example ``sexual_content``). Python
    # treats ``_`` as a word character, so searching the raw tag with ``\b``
    # would miss those explicit safety labels.
    english_tokens = re.sub(r"[\W_]+", " ", normalized, flags=re.UNICODE)
    return bool(
        _AGE_RATING_DANGER.search(normalized)
        or _ENGLISH_DANGER.search(english_tokens)
    ) or any(
        word in normalized for word in _CJK_DANGER
    )


def _text(value, label, required=True, limit=4096):
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise CandidateSelectionError("%s is not valid UTF-8" % label) from None
    if value is None:
        value = ""
    if not isinstance(value, str):
        value = str(value)
    value = value.strip()
    if (required and not value) or len(value) > limit or any(ord(char) < 32 for char in value):
        raise CandidateSelectionError("%s is incomplete" % label)
    return value


def _integer(value, label):
    if isinstance(value, bool):
        raise CandidateSelectionError("%s is invalid" % label)
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        raise CandidateSelectionError("%s is invalid" % label) from None


def _float(value, label):
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        raise CandidateSelectionError("%s is invalid" % label) from None
    if result < 0 or not math.isfinite(result):
        raise CandidateSelectionError("%s is invalid" % label)
    return result


def _cursor_rows(connection, sql, params):
    statement = str(sql or "").lstrip()
    if not re.match(r"(?i)^SELECT\b", statement):
        raise CandidateSelectionError("selector attempted a non-read-only statement")
    cursor = connection.cursor()
    try:
        cursor.execute(sql, tuple(params))
        rows = cursor.fetchall()
        return [dict(row) for row in rows]
    except CandidateSelectionError:
        raise
    except Exception as exc:
        raise CandidateQueryError(
            "read-only candidate query failed: %s" % type(exc).__name__
        ) from None
    finally:
        close = getattr(cursor, "close", None)
        if callable(close):
            close()


def _cursor_row(connection, sql, params):
    rows = _cursor_rows(connection, sql, params)
    if len(rows) != 1:
        raise CandidateSelectionError("candidate check returned an ambiguous result")
    return rows[0]


class DramawaveCandidateSelector:
    """Select spend-ranked, compliant and unambiguous video candidates."""

    def __init__(self, connection, schema=DEFAULT_SCHEMA):
        if not _SAFE_IDENTIFIER.fullmatch(str(schema or "")):
            raise CandidateSelectionError("invalid MySQL schema")
        self.connection = connection
        self.schema = str(schema)

    def _base_rows(self, source_date, scan_limit, product):
        schema = self.schema
        sql = """
            SELECT
                s.resource_id AS material_id,
                ROUND(SUM(COALESCE(s.spend, 0)), 6) AS spend,
                COUNT(DISTINCT NULLIF(TRIM(s.series_code), '')) AS series_count,
                MIN(NULLIF(TRIM(s.series_code), '')) AS series_code,
                COUNT(DISTINCT NULLIF(TRIM(s.drama_language), '')) AS drama_language_count,
                MIN(NULLIF(TRIM(s.drama_language), '')) AS drama_language,
                COUNT(DISTINCT NULLIF(TRIM(s.data_source_id), '')) AS insight_content_id_count,
                MIN(NULLIF(TRIM(s.data_source_id), '')) AS insight_content_id,
                cs.url AS material_url,
                cs.name AS material_name,
                cs.language AS material_language,
                cs.data_source_id AS content_id,
                cs.tag_name AS source_tag_name,
                cs.video_duration AS video_duration
            FROM `{schema}`.ads_custom_source_insight s FORCE INDEX (pss)
            JOIN `{schema}`.ads_custom_source cs
              ON cs.id = CAST(s.resource_id AS UNSIGNED)
            WHERE s.product = %s
              AND s.dt = %s
              AND s.resource_id REGEXP '^[1-9][0-9]*$'
              AND cs.type = %s
              AND cs.is_delete = %s
              AND cs.video_duration BETWEEN %s AND %s
            GROUP BY
                s.resource_id, cs.url, cs.name, cs.language, cs.data_source_id,
                cs.tag_name, cs.video_duration
            ORDER BY
                SUM(COALESCE(s.spend, 0)) DESC,
                CAST(s.resource_id AS UNSIGNED) ASC,
                s.resource_id ASC
            LIMIT %s
        """.format(schema=schema)
        return _cursor_rows(
            self.connection,
            sql,
            (product, source_date, 2, 0, 1, 140, int(scan_limit)),
        )

    def _violation_counts(self, material_id):
        schema = self.schema
        sql = """
            SELECT
              (SELECT COUNT(*)
                 FROM `{schema}`.ads_facebook_violations f
                WHERE f.source_id = %s) AS facebook_count,
              (SELECT COUNT(*)
                 FROM `{schema}`.ads_tiktok_violations t
                WHERE t.source_id = %s OR t.original_source_id = %s) AS tiktok_count,
              (SELECT COUNT(*)
                 FROM `{schema}`.ads_twitter_violations x
                WHERE x.source_id = %s OR x.original_source_id = %s) AS twitter_count,
              (SELECT COUNT(*)
                 FROM `{schema}`.ads_resource_audit a
                WHERE a.resource_id = %s) AS resource_audit_count
        """.format(schema=schema)
        return _cursor_row(
            self.connection,
            sql,
            (material_id, material_id, material_id, material_id, material_id, material_id),
        )

    def _material_tags(self, material_id):
        sql = """
            SELECT rt.tag_name
              FROM `{schema}`.resource_tags rt
             WHERE rt.source_id = %s
             ORDER BY rt.id ASC
        """.format(schema=self.schema)
        return [row.get("tag_name") for row in _cursor_rows(self.connection, sql, (material_id,))]

    def _drama_rows(self, content_id, series_code, language):
        sql = """
            SELECT
                r.content_id, r.series_code, r.language, r.name AS drama_name,
                r.labels AS drama_labels, r.desc AS drama_description
              FROM `{schema}`.ads_drama_resource r FORCE INDEX (content_id)
             WHERE r.content_id = %s
               AND r.series_code = %s
               AND r.language = %s
        """.format(schema=self.schema)
        return _cursor_rows(self.connection, sql, (content_id, series_code, language))

    def _pool_material_rows(self, material_id):
        """Load one eligible video directly from the custom material library."""
        sql = """
            SELECT
                CAST(cs.id AS CHAR) AS material_id,
                cs.product AS product,
                cs.url AS material_url,
                cs.name AS material_name,
                cs.language AS material_language,
                cs.data_source_id AS content_id,
                cs.tag_name AS source_tag_name,
                cs.video_duration AS video_duration
             FROM `{schema}`.ads_custom_source cs
             WHERE cs.id = %s
               AND cs.product = %s
               AND cs.type = %s
               AND cs.is_delete = %s
               AND cs.video_duration BETWEEN %s AND %s
             LIMIT 2
        """.format(schema=self.schema)
        return _cursor_rows(
            self.connection,
            sql,
            (material_id, DEFAULT_PRODUCT, 2, 0, 1, 140),
        )

    def _pool_drama_rows(self, content_id, language):
        """Resolve a manual material without depending on delivery insight rows."""
        sql = """
            SELECT
                r.content_id, r.series_code, r.language, r.name AS drama_name,
                r.labels AS drama_labels, r.desc AS drama_description
              FROM `{schema}`.ads_drama_resource r FORCE INDEX (content_id)
             WHERE r.content_id = %s
               AND LOWER(TRIM(r.language)) = LOWER(%s)
             ORDER BY r.id ASC
        """.format(schema=self.schema)
        return _cursor_rows(self.connection, sql, (content_id, language))

    def _pool_candidate(self, material_id, source_date):
        material_rows = self._pool_material_rows(material_id)
        if len(material_rows) != 1:
            raise PoolCandidateRejection(
                "material_not_found_or_ineligible",
                "material is missing or is not an eligible active video",
            )
        row = material_rows[0]
        try:
            candidate_id = _text(row.get("material_id"), "material_id", limit=64)
            key = material_key(candidate_id)
            if key != material_id:
                raise CandidateSelectionError("material identity mismatch")
            product = _text(row.get("product"), "product", limit=64)
            if product != DEFAULT_PRODUCT:
                raise PoolCandidateRejection(
                    "material_product_mismatch",
                    "material does not belong to Dramawave",
                )
            material_language = _text(
                row.get("material_language"), "material_language", limit=32
            )
            content_id = _text(row.get("content_id"), "content_id", limit=128)
            material_url = normalize_material_url(
                _text(row.get("material_url"), "material_url", limit=4096)
            )
            material_name = _text(
                row.get("material_name"), "material_name", limit=500
            )
        except PoolCandidateRejection:
            raise
        except CandidateSelectionError as exc:
            raise PoolCandidateRejection(
                "material_metadata_invalid",
                "material metadata is incomplete or invalid: %s" % exc,
            ) from None
        if not material_url.startswith("https://"):
            raise PoolCandidateRejection(
                "material_url_not_https",
                "material URL is not HTTPS",
            )

        violation_counts = self._violation_counts(candidate_id)
        normalized_counts = {}
        try:
            for field in (
                "facebook_count",
                "tiktok_count",
                "twitter_count",
                "resource_audit_count",
            ):
                normalized_counts[field] = _integer(violation_counts.get(field), field)
        except CandidateSelectionError as exc:
            raise PoolCandidateRejection(
                "violation_check_invalid",
                "violation check returned invalid data: %s" % exc,
            ) from None
        if any(value != 0 for value in normalized_counts.values()):
            raise PoolCandidateRejection(
                "material_has_violation",
                "material has a violation record",
            )

        source_tag = row.get("source_tag_name")
        if source_tag not in (None, ""):
            try:
                source_tag_is_unsafe = contains_dangerous_tag(source_tag)
            except CandidateSelectionError as exc:
                raise PoolCandidateRejection(
                    "material_source_tag_invalid",
                    "material source tag cannot be checked safely: %s" % exc,
                ) from None
            if source_tag_is_unsafe:
                raise PoolCandidateRejection(
                    "material_source_tag_unsafe",
                    "material source tag is unsafe",
                )

        material_tags = self._material_tags(candidate_id)
        for tag_value in material_tags:
            try:
                tag_is_unsafe = contains_dangerous_tag(tag_value)
            except CandidateSelectionError as exc:
                raise PoolCandidateRejection(
                    "material_tag_invalid",
                    "material tag cannot be checked safely: %s" % exc,
                ) from None
            if tag_is_unsafe:
                raise PoolCandidateRejection(
                    "material_tag_unsafe",
                    "material tag is unsafe",
                )

        drama_rows = self._pool_drama_rows(content_id, material_language)
        if not drama_rows:
            raise PoolCandidateRejection(
                "drama_mapping_missing",
                "drama mapping is missing",
            )

        canonical = {}
        for drama in drama_rows:
            try:
                mapped_content_id = _text(
                    drama.get("content_id"), "drama content_id", limit=128
                )
                series_code = _text(
                    drama.get("series_code"), "drama series_code", limit=128
                )
                mapped_language = _text(
                    drama.get("language"), "drama language", limit=32
                )
                drama_name = _text(drama.get("drama_name"), "drama name", limit=500)
                raw_labels = _text(
                    drama.get("drama_labels"), "drama labels", limit=4096
                )
                description = _text(
                    drama.get("drama_description"),
                    "drama description",
                    limit=4096,
                )
            except CandidateSelectionError as exc:
                raise PoolCandidateRejection(
                    "drama_mapping_invalid",
                    "drama mapping is incomplete or invalid: %s" % exc,
                ) from None
            if (
                mapped_content_id != content_id
                or mapped_language.casefold() != material_language.casefold()
            ):
                raise PoolCandidateRejection(
                    "drama_mapping_invalid",
                    "drama mapping identity does not match the material",
                )
            labels = [item.strip() for item in raw_labels.split(",") if item.strip()]
            if not labels:
                raise PoolCandidateRejection(
                    "drama_mapping_invalid",
                    "drama labels are incomplete",
                )
            for label in labels:
                try:
                    label_is_unsafe = contains_dangerous_tag(label)
                except CandidateSelectionError as exc:
                    raise PoolCandidateRejection(
                        "drama_label_invalid",
                        "drama label cannot be checked safely: %s" % exc,
                    ) from None
                if label_is_unsafe:
                    raise PoolCandidateRejection(
                        "drama_label_unsafe",
                        "drama label is unsafe",
                    )
            canonical_key = (
                mapped_content_id,
                series_code,
                mapped_language.casefold(),
                drama_name,
                tuple(label.casefold() for label in labels),
                description,
            )
            canonical.setdefault(
                canonical_key,
                {
                    "series_code": series_code,
                    "drama_name": drama_name,
                    "labels": labels,
                    "description": description,
                },
            )
        if len(canonical) != 1:
            raise PoolCandidateRejection(
                "drama_mapping_ambiguous",
                "drama mapping is ambiguous",
            )
        drama = next(iter(canonical.values()))

        return {
            "source_date": source_date,
            "material_key": key,
            "material_id": candidate_id,
            "content_id": content_id,
            "material_url": material_url,
            "material_name": material_name,
            "material_language": material_language,
            "drama_name": drama["drama_name"],
            "tag": drama["labels"][0],
            "description": drama["description"],
            "spend": 0.0,
            "facebook_violation_count": normalized_counts["facebook_count"],
            "tiktok_violation_count": normalized_counts["tiktok_count"],
            "twitter_violation_count": normalized_counts["twitter_count"],
            "resource_audit_count": normalized_counts["resource_audit_count"],
            "dangerous_tag_count": 0,
        }

    def _candidate(self, row, source_date):
        candidate_id = _text(row.get("material_id"), "material_id", limit=64)
        key = material_key(candidate_id)
        if _integer(row.get("series_count"), "series_count") != 1:
            raise CandidateSelectionError("series mapping is ambiguous")
        if _integer(row.get("drama_language_count"), "drama_language_count") != 1:
            raise CandidateSelectionError("drama language mapping is ambiguous")
        if _integer(row.get("insight_content_id_count"), "insight_content_id_count") != 1:
            raise CandidateSelectionError("insight content mapping is ambiguous")

        series_code = _text(row.get("series_code"), "series_code", limit=128)
        material_language = _text(row.get("material_language"), "material_language", limit=32)
        drama_language = _text(row.get("drama_language"), "drama_language", limit=32)
        if material_language.casefold() != drama_language.casefold():
            raise CandidateSelectionError("material and drama language do not match")
        content_id = _text(row.get("content_id"), "content_id", limit=128)
        insight_content_id = _text(
            row.get("insight_content_id"), "insight content_id", limit=128
        )
        if insight_content_id != content_id:
            raise CandidateSelectionError("insight and material content IDs do not match")
        material_url = normalize_material_url(
            _text(row.get("material_url"), "material_url", limit=4096)
        )
        if not material_url.startswith("https://"):
            raise CandidateSelectionError("material URL is not HTTPS")

        violation_counts = self._violation_counts(candidate_id)
        normalized_counts = {}
        for field in (
            "facebook_count",
            "tiktok_count",
            "twitter_count",
            "resource_audit_count",
        ):
            normalized_counts[field] = _integer(violation_counts.get(field), field)
            if normalized_counts[field] != 0:
                raise CandidateSelectionError("material has a violation record")

        source_tag = row.get("source_tag_name")
        if source_tag not in (None, "") and contains_dangerous_tag(source_tag):
            raise CandidateSelectionError("material source tag is unsafe")
        material_tags = self._material_tags(candidate_id)
        for tag_value in material_tags:
            if contains_dangerous_tag(tag_value):
                raise CandidateSelectionError("material tag is unsafe")

        drama_rows = self._drama_rows(content_id, series_code, material_language)
        if not drama_rows:
            raise CandidateSelectionError("drama mapping is missing")
        canonical = set()
        for drama in drama_rows:
            mapped_content_id = _text(drama.get("content_id"), "drama content_id", limit=128)
            mapped_series_code = _text(drama.get("series_code"), "drama series_code", limit=128)
            mapped_language = _text(drama.get("language"), "drama language", limit=32)
            drama_name = _text(drama.get("drama_name"), "drama name", limit=500)
            drama_labels = _text(drama.get("drama_labels"), "drama labels", limit=4096)
            description = _text(
                drama.get("drama_description"), "drama description", limit=4096
            )
            if (
                mapped_content_id != content_id
                or mapped_series_code != series_code
                or mapped_language.casefold() != material_language.casefold()
            ):
                raise CandidateSelectionError("drama mapping identity mismatch")
            canonical.add(
                (
                    mapped_content_id,
                    mapped_series_code,
                    mapped_language.casefold(),
                    drama_name,
                    drama_labels,
                    description,
                )
            )
        if len(canonical) != 1:
            raise CandidateSelectionError("drama mapping is ambiguous")
        (
            _mapped_content_id,
            _mapped_series_code,
            _mapped_language,
            drama_name,
            drama_labels,
            description,
        ) = next(iter(canonical))
        labels = [item.strip() for item in drama_labels.split(",") if item.strip()]
        if not labels:
            raise CandidateSelectionError("drama labels are incomplete")
        for label in labels:
            if contains_dangerous_tag(label):
                raise CandidateSelectionError("drama label is unsafe")

        return {
            "source_date": source_date,
            "material_key": key,
            "material_id": candidate_id,
            "content_id": content_id,
            "material_url": material_url,
            "material_name": _text(row.get("material_name"), "material_name", limit=500),
            "material_language": material_language,
            "drama_name": drama_name,
            "tag": labels[0],
            "description": description,
            "spend": _float(row.get("spend"), "spend"),
            "facebook_violation_count": normalized_counts["facebook_count"],
            "tiktok_violation_count": normalized_counts["tiktok_count"],
            "twitter_violation_count": normalized_counts["twitter_count"],
            "resource_audit_count": normalized_counts["resource_audit_count"],
            "dangerous_tag_count": 0,
        }

    def select(self, source_date, excluded_material_keys=(), limit=3, scan_limit=1000):
        source_date = normalize_date(source_date)
        try:
            limit = int(limit)
            scan_limit = int(scan_limit)
        except (TypeError, ValueError, OverflowError):
            raise CandidateSelectionError("candidate limits are invalid") from None
        if limit <= 0 or limit > 100 or scan_limit < limit or scan_limit > 5000:
            raise CandidateSelectionError("candidate limits are out of range")
        excluded = {str(value or "").strip() for value in excluded_material_keys}
        selected = []
        seen = set()
        for row in self._base_rows(source_date, scan_limit, DEFAULT_PRODUCT):
            raw_id = str(row.get("material_id", "") or "").strip()
            try:
                key = material_key(raw_id)
            except CandidateSelectionError:
                continue
            if key in excluded or key in seen:
                continue
            seen.add(key)
            try:
                selected.append(self._candidate(row, source_date))
            except CandidateQueryError:
                raise
            except CandidateSelectionError:
                continue
            if len(selected) >= limit:
                break
        return selected

    def select_pool(self, pool_items, source_date, limit=3):
        """Hydrate the oldest safe manual-pool items.

        Pool order is determined exclusively by ``created_at`` then ``id``.
        A data-quality or safety rejection is returned per item, while a
        database query failure aborts the whole operation.
        """
        source_date = normalize_date(source_date)
        try:
            limit = int(limit)
        except (TypeError, ValueError, OverflowError):
            raise CandidateSelectionError("candidate limit is invalid") from None
        if limit <= 0 or limit > 100:
            raise CandidateSelectionError("candidate limit is out of range")
        try:
            raw_items = list(pool_items)
        except TypeError:
            raise CandidateSelectionError("pool_items must be iterable") from None
        if len(raw_items) > 5000:
            raise CandidateSelectionError("pool_items exceeds the safety limit")

        prepared = []
        rejections = []
        for position, item in enumerate(raw_items):
            if not isinstance(item, dict):
                rejections.append(
                    {
                        "pool_item_id": "",
                        "material_id": "",
                        "error_code": "pool_item_invalid",
                        "error_message": "pool item is not an object",
                    }
                )
                continue
            raw_pool_item_id = item.get("id", item.get("pool_item_id"))
            raw_material_id = item.get("material_id")
            try:
                pool_item_id = _integer(raw_pool_item_id, "pool_item_id")
                if pool_item_id <= 0:
                    raise CandidateSelectionError("pool_item_id is invalid")
                created_at = _text(
                    item.get("created_at"), "pool created_at", limit=64
                )
                parsed_created_at = datetime.fromisoformat(
                    created_at.replace("Z", "+00:00")
                )
                if parsed_created_at.tzinfo is None:
                    parsed_created_at = parsed_created_at.replace(tzinfo=timezone.utc)
                created_timestamp = parsed_created_at.astimezone(timezone.utc).timestamp()
                key = material_key(raw_material_id)
            except (CandidateSelectionError, ValueError, OverflowError, OSError) as exc:
                rejections.append(
                    {
                        "pool_item_id": raw_pool_item_id,
                        "material_id": str(raw_material_id or "").strip(),
                        "error_code": "pool_item_invalid",
                        "error_message": "pool item is invalid: %s" % exc,
                    }
                )
                continue
            prepared.append(
                {
                    "pool_item_id": pool_item_id,
                    "material_id": key,
                    "created_at": created_at,
                    "sort_key": (created_timestamp, pool_item_id, position),
                }
            )
        prepared.sort(key=lambda item: item["sort_key"])

        selected = []
        seen_pool_ids = set()
        seen_material_keys = set()
        for item in prepared:
            pool_item_id = item["pool_item_id"]
            candidate_id = item["material_id"]
            if pool_item_id in seen_pool_ids:
                rejections.append(
                    {
                        "pool_item_id": pool_item_id,
                        "material_id": candidate_id,
                        "error_code": "duplicate_pool_item",
                        "error_message": "pool item is duplicated",
                    }
                )
                continue
            seen_pool_ids.add(pool_item_id)
            if candidate_id in seen_material_keys:
                rejections.append(
                    {
                        "pool_item_id": pool_item_id,
                        "material_id": candidate_id,
                        "error_code": "duplicate_material_id",
                        "error_message": "material ID is duplicated in the pool input",
                    }
                )
                continue
            seen_material_keys.add(candidate_id)
            try:
                candidate = self._pool_candidate(candidate_id, source_date)
            except CandidateQueryError:
                raise
            except PoolCandidateRejection as exc:
                rejections.append(
                    {
                        "pool_item_id": pool_item_id,
                        "material_id": candidate_id,
                        "error_code": exc.error_code,
                        "error_message": exc.error_message,
                    }
                )
                continue
            except CandidateSelectionError as exc:
                rejections.append(
                    {
                        "pool_item_id": pool_item_id,
                        "material_id": candidate_id,
                        "error_code": "material_safety_check_failed",
                        "error_message": str(exc),
                    }
                )
                continue
            candidate["pool_item_id"] = pool_item_id
            candidate["pool_created_at"] = item["created_at"]
            selected.append(candidate)
            if len(selected) >= limit:
                break
        return selected, rejections


def select_candidates(
    connection,
    source_date,
    excluded_material_keys=(),
    limit=3,
    scan_limit=1000,
    schema=DEFAULT_SCHEMA,
):
    return DramawaveCandidateSelector(connection, schema=schema).select(
        source_date,
        excluded_material_keys=excluded_material_keys,
        limit=limit,
        scan_limit=scan_limit,
    )


def select_pool_candidates(
    connection,
    pool_items,
    source_date,
    limit=3,
    schema=DEFAULT_SCHEMA,
):
    """Hydrate manual-pool materials in oldest-first order.

    Returns ``(candidates, rejections)``. Rejections are safe item-level
    outcomes; a :class:`CandidateQueryError` still aborts the whole call.
    """
    return DramawaveCandidateSelector(connection, schema=schema).select_pool(
        pool_items,
        source_date,
        limit=limit,
    )


def ranked_material_ids(
    connection,
    source_date,
    scan_limit=1000,
    schema=DEFAULT_SCHEMA,
    product=DEFAULT_PRODUCT,
):
    """Return the bounded candidate universe used for occupancy lookup.

    This intentionally applies the same product/date/video filters and stable
    spend order as full selection, but avoids violation/tag/drama joins.
    """
    source_date = normalize_date(source_date)
    if not _SAFE_IDENTIFIER.fullmatch(str(schema or "")):
        raise CandidateSelectionError("invalid MySQL schema")
    try:
        scan_limit = int(scan_limit)
    except (TypeError, ValueError, OverflowError):
        raise CandidateSelectionError("scan limit is invalid") from None
    if scan_limit < 3 or scan_limit > 1000:
        raise CandidateSelectionError("scan limit is out of range")
    sql = """
        SELECT s.resource_id AS material_id
          FROM `{schema}`.ads_custom_source_insight s FORCE INDEX (pss)
          JOIN `{schema}`.ads_custom_source cs
            ON cs.id = CAST(s.resource_id AS UNSIGNED)
         WHERE s.product = %s
           AND s.dt = %s
           AND s.resource_id REGEXP '^[1-9][0-9]*$'
           AND cs.type = %s
           AND cs.is_delete = %s
           AND cs.video_duration BETWEEN %s AND %s
         GROUP BY s.resource_id
         ORDER BY
             SUM(COALESCE(s.spend, 0)) DESC,
             CAST(s.resource_id AS UNSIGNED) ASC,
             s.resource_id ASC
         LIMIT %s
    """.format(schema=schema)
    rows = _cursor_rows(
        connection,
        sql,
        (product, source_date, 2, 0, 1, 140, scan_limit),
    )
    values = []
    seen = set()
    for row in rows:
        key = material_key(row.get("material_id"))
        if key not in seen:
            seen.add(key)
            values.append(key)
    if not values:
        raise CandidateSelectionError("no ranked Dramawave video materials were found")
    return values


def connect_read_only(
    *,
    host,
    port,
    user,
    password,
    database=DEFAULT_SCHEMA,
    connect_timeout=5,
    read_timeout=30,
):
    """Open and verify a PyMySQL connection to a read-only replica."""
    try:
        import pymysql
    except ImportError:
        raise CandidateSelectionError("PyMySQL is required for candidate selection") from None
    if not host or not user or password is None:
        raise CandidateSelectionError("read-only MySQL credentials are incomplete")
    if not _SAFE_IDENTIFIER.fullmatch(str(database or "")):
        raise CandidateSelectionError("invalid MySQL database")
    try:
        connection = pymysql.connect(
            host=str(host),
            port=int(port),
            user=str(user),
            password=str(password),
            database=str(database),
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=True,
            connect_timeout=int(connect_timeout),
            read_timeout=int(read_timeout),
            write_timeout=int(read_timeout),
        )
        cursor = connection.cursor()
        try:
            cursor.execute("SET SESSION TRANSACTION READ ONLY")
            cursor.execute("SELECT @@read_only AS read_only")
            row = cursor.fetchone()
        finally:
            cursor.close()
        if not row or _integer(row.get("read_only"), "read_only") != 1:
            connection.close()
            raise CandidateSelectionError("MySQL endpoint is not read-only")
        return connection
    except CandidateSelectionError:
        raise
    except Exception as exc:
        raise CandidateSelectionError(
            "read-only MySQL connection failed: %s" % type(exc).__name__
        ) from None
