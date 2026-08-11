"""Deterministic two-stage X automatic material selection.

The public operation is :meth:`TwoStageSelector.select_and_reserve`.  It never
returns an unreserved winner: permanent exclusion, a final blacklist refresh,
published-history recheck, template cooldown and the global material uniqueness
constraint are all evaluated before the selected identity leaves this layer.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Tuple,
)

from .repositories import (
    BEIJING_TZ,
    DEFAULT_APP_ID,
    DEFAULT_MATERIAL_DATA_SOURCE,
    DEFAULT_PLATFORM,
    DEFAULT_PRODUCT,
    BlacklistSnapshot,
    DramaSourceRow,
    MaterialSourceRow,
    MetricTotals,
    MetricWindowRepository,
    MetricWindowSnapshot,
    SourceDataError,
    canonical_language,
    canonical_material_id,
    complete_beijing_dates,
    utc_iso,
)


UTC = timezone.utc
MAX_DRAMA_WINDOW_DAYS = 3650
MAX_COOLDOWN_DAYS = 3650
MAX_SOURCE_DURATION_SECONDS = Decimal("600")
_ACCOUNT_ID = re.compile(r"^[A-Za-z0-9._:@-]{1,128}$")
_SORT_FIELDS = frozenset({"spend", "d0_roas"})
_SORT_DIRECTIONS = frozenset({"asc", "desc"})
_VALIDATED_PUBLIC_TEXT_LIMITS = {
    "description": 4096,
    "drama_name": 500,
    "material_tag": 255,
}


class SelectionError(RuntimeError):
    def __init__(self, code: str, message: str, status: int = 409):
        self.code = str(code)
        self.status = int(status)
        super().__init__(str(message))


class CandidateRejected(SelectionError):
    """One candidate is unusable; deterministic scanning may continue."""


class NoEligibleMaterial(SelectionError):
    def __init__(self, rejection_counts: Mapping[str, int]):
        self.rejection_counts = dict(rejection_counts)
        super().__init__(
            "x_auto_no_eligible_material",
            "no eligible X material matched the template",
            409,
        )


def _decimal_optional(value: Any, label: str) -> Optional[Decimal]:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise ValueError("%s is invalid" % label)
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("%s is invalid" % label) from None
    if not result.is_finite() or result < 0:
        raise ValueError("%s is invalid" % label)
    return result


def _nonnegative_int(value: Any, label: str, maximum: int) -> int:
    if isinstance(value, bool):
        raise ValueError("%s is invalid" % label)
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("%s is invalid" % label) from None
    if result < 0 or result > int(maximum):
        raise ValueError("%s is invalid" % label)
    return result


def _positive_int(value: Any, label: str) -> int:
    result = _nonnegative_int(value, label, 2**63 - 1)
    if result == 0:
        raise ValueError("%s is invalid" % label)
    return result


def _current_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("now must be a datetime")
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@dataclass(frozen=True)
class DecimalRange:
    minimum: Optional[Decimal] = None
    maximum: Optional[Decimal] = None

    def __post_init__(self) -> None:
        for value, label in ((self.minimum, "minimum"), (self.maximum, "maximum")):
            if value is not None and (
                not isinstance(value, Decimal)
                or not value.is_finite()
                or value < 0
            ):
                raise ValueError("%s is invalid" % label)
        if (
            self.minimum is not None
            and self.maximum is not None
            and self.minimum > self.maximum
        ):
            raise ValueError("minimum cannot exceed maximum")

    @classmethod
    def from_mapping(cls, raw: Any, label: str) -> "DecimalRange":
        if raw in (None, ""):
            return cls()
        if not isinstance(raw, Mapping):
            raise ValueError("%s range must be an object" % label)
        unknown = set(raw).difference({"min", "max", "minimum", "maximum"})
        if unknown:
            raise ValueError("%s range has unknown fields" % label)
        return cls(
            minimum=_decimal_optional(raw.get("min", raw.get("minimum")), "%s minimum" % label),
            maximum=_decimal_optional(raw.get("max", raw.get("maximum")), "%s maximum" % label),
        )

    @property
    def constrained(self) -> bool:
        return self.minimum is not None or self.maximum is not None

    def contains(self, value: Optional[Decimal]) -> bool:
        if value is None:
            return not self.constrained
        if self.minimum is not None and value < self.minimum:
            return False
        if self.maximum is not None and value > self.maximum:
            return False
        return True

    def as_dict(self) -> Dict[str, Optional[str]]:
        return {
            "min": None if self.minimum is None else format(self.minimum, "f"),
            "max": None if self.maximum is None else format(self.maximum, "f"),
        }


@dataclass(frozen=True)
class SortRule:
    field: str = "spend"
    direction: str = "desc"

    def __post_init__(self) -> None:
        if self.field not in _SORT_FIELDS or self.direction not in _SORT_DIRECTIONS:
            raise ValueError("sort rule is invalid")

    @classmethod
    def from_mapping(cls, raw: Any) -> "SortRule":
        if raw in (None, ""):
            return cls()
        if not isinstance(raw, Mapping) or set(raw).difference({"field", "direction"}):
            raise ValueError("sort rule is invalid")
        return cls(
            field=str(raw.get("field") or "spend").strip(),
            direction=str(raw.get("direction") or "desc").strip(),
        )


@dataclass(frozen=True)
class DramaFilterRule:
    spend: DecimalRange = field(default_factory=DecimalRange)
    d0_roas: DecimalRange = field(default_factory=DecimalRange)
    sort: SortRule = field(default_factory=SortRule)
    resource_types: Tuple[str, ...] = ()
    launch_window_days: int = 0
    cooldown_days: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "resource_types",
            tuple(dict.fromkeys(str(value).strip() for value in self.resource_types if str(value).strip())),
        )
        _nonnegative_int(
            self.launch_window_days,
            "launch_window_days",
            MAX_DRAMA_WINDOW_DAYS,
        )
        _nonnegative_int(
            self.cooldown_days,
            "cooldown_days",
            MAX_COOLDOWN_DAYS,
        )

    @classmethod
    def from_mapping(cls, raw: Any) -> "DramaFilterRule":
        raw = {} if raw in (None, "") else raw
        if not isinstance(raw, Mapping):
            raise ValueError("drama rule must be an object")
        allowed = {
            "spend",
            "d0_roas",
            "sort",
            "resource_types",
            "launch_window_days",
            "cooldown_days",
        }
        if set(raw).difference(allowed):
            raise ValueError("drama rule has unknown fields")
        resource_types = raw.get("resource_types") or []
        if isinstance(resource_types, (str, bytes)) or not isinstance(resource_types, Sequence):
            raise ValueError("resource_types must be an array")
        return cls(
            spend=DecimalRange.from_mapping(raw.get("spend"), "drama spend"),
            d0_roas=DecimalRange.from_mapping(raw.get("d0_roas"), "drama D0 ROAS"),
            sort=SortRule.from_mapping(raw.get("sort")),
            resource_types=tuple(str(value).strip() for value in resource_types),
            launch_window_days=_nonnegative_int(
                raw.get("launch_window_days", 0),
                "launch_window_days",
                MAX_DRAMA_WINDOW_DAYS,
            ),
            cooldown_days=_nonnegative_int(
                raw.get("cooldown_days", 0),
                "cooldown_days",
                MAX_COOLDOWN_DAYS,
            ),
        )


@dataclass(frozen=True)
class MaterialFilterRule:
    spend: DecimalRange = field(default_factory=DecimalRange)
    d0_roas: DecimalRange = field(default_factory=DecimalRange)
    duration_seconds: DecimalRange = field(default_factory=DecimalRange)
    sort: SortRule = field(default_factory=SortRule)

    @classmethod
    def from_mapping(cls, raw: Any) -> "MaterialFilterRule":
        raw = {} if raw in (None, "") else raw
        if not isinstance(raw, Mapping):
            raise ValueError("material rule must be an object")
        allowed = {"spend", "d0_roas", "duration_seconds", "sort"}
        if set(raw).difference(allowed):
            raise ValueError("material rule has unknown fields")
        return cls(
            spend=DecimalRange.from_mapping(raw.get("spend"), "material spend"),
            d0_roas=DecimalRange.from_mapping(raw.get("d0_roas"), "material D0 ROAS"),
            duration_seconds=DecimalRange.from_mapping(
                raw.get("duration_seconds"),
                "material duration",
            ),
            sort=SortRule.from_mapping(raw.get("sort")),
        )


@dataclass(frozen=True)
class SelectionRules:
    metric_window_days: int = 7
    platform: int = DEFAULT_PLATFORM
    drama: DramaFilterRule = field(default_factory=DramaFilterRule)
    material: MaterialFilterRule = field(default_factory=MaterialFilterRule)

    def __post_init__(self) -> None:
        if isinstance(self.metric_window_days, bool):
            raise ValueError("metric_window_days is invalid")
        if int(self.metric_window_days) < 1 or int(self.metric_window_days) > 30:
            raise ValueError("metric_window_days must be between 1 and 30")
        if isinstance(self.platform, bool) or int(self.platform) != DEFAULT_PLATFORM:
            raise ValueError("platform must be 0")

    @classmethod
    def from_mapping(cls, raw: Any) -> "SelectionRules":
        if not isinstance(raw, Mapping):
            raise ValueError("selection rules must be an object")
        allowed = {"metric_window_days", "platform", "drama", "material"}
        if set(raw).difference(allowed):
            raise ValueError("selection rules have unknown fields")
        raw_days = raw.get("metric_window_days")
        days = (
            7
            if raw_days in (None, "")
            else _nonnegative_int(raw_days, "metric_window_days", 30)
        )
        if days == 0:
            raise ValueError("metric_window_days must be between 1 and 30")
        raw_platform = raw.get("platform")
        platform = (
            DEFAULT_PLATFORM
            if raw_platform in (None, "")
            else _nonnegative_int(raw_platform, "platform", 255)
        )
        return cls(
            metric_window_days=days,
            platform=platform,
            drama=DramaFilterRule.from_mapping(raw.get("drama")),
            material=MaterialFilterRule.from_mapping(raw.get("material")),
        )

    def as_dict(self) -> Dict[str, Any]:
        return {
            "metric_window_days": int(self.metric_window_days),
            "platform": int(self.platform),
            "drama": {
                "spend": self.drama.spend.as_dict(),
                "d0_roas": self.drama.d0_roas.as_dict(),
                "sort": {
                    "field": self.drama.sort.field,
                    "direction": self.drama.sort.direction,
                },
                "resource_types": list(self.drama.resource_types),
                "launch_window_days": int(self.drama.launch_window_days),
                "cooldown_days": int(self.drama.cooldown_days),
            },
            "material": {
                "spend": self.material.spend.as_dict(),
                "d0_roas": self.material.d0_roas.as_dict(),
                "duration_seconds": self.material.duration_seconds.as_dict(),
                "sort": {
                    "field": self.material.sort.field,
                    "direction": self.material.sort.direction,
                },
            },
        }


@dataclass(frozen=True)
class SelectionRequest:
    run_id: int
    task_id: int
    template_id: int
    template_version: int
    account_id: str
    language: str
    rules: SelectionRules
    now: datetime
    claim_token: str = ""

    def __post_init__(self) -> None:
        for value, label in (
            (self.run_id, "run_id"),
            (self.task_id, "task_id"),
            (self.template_id, "template_id"),
            (self.template_version, "template_version"),
        ):
            _positive_int(value, label)
        if not _ACCOUNT_ID.fullmatch(str(self.account_id or "").strip()):
            raise ValueError("account_id is invalid")
        canonical_language(self.language)
        _current_utc(self.now)
        if (
            not isinstance(self.claim_token, str)
            or len(self.claim_token) > 512
            or "\x00" in self.claim_token
        ):
            raise ValueError("claim_token is invalid")


@dataclass(frozen=True)
class DramaCandidate:
    content_id: str
    series_code: str
    language: str
    resource_type_v2: str
    deploy_time: int
    name: str
    metrics: MetricTotals


@dataclass(frozen=True)
class MaterialCandidate:
    source: MaterialSourceRow
    metrics: MetricTotals
    validated: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReservedSelection:
    reservation: Mapping[str, Any]
    drama: Optional[DramaCandidate]
    material: Optional[MaterialCandidate]
    metric_dates: Tuple[str, ...]
    initial_blacklist_sha256: str
    final_blacklist_sha256: str
    idempotent: bool = False

    def as_dict(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "reservation": dict(self.reservation),
            "metric_dates": list(self.metric_dates),
            "initial_blacklist_sha256": self.initial_blacklist_sha256,
            "final_blacklist_sha256": self.final_blacklist_sha256,
            "idempotent": bool(self.idempotent),
        }
        if self.drama is not None:
            result["drama"] = _drama_snapshot(self.drama)
        if self.material is not None:
            result["material"] = _material_snapshot(self.material)
        return result


class CandidateSource(Protocol):
    def blacklist_snapshot(self) -> BlacklistSnapshot:
        ...

    def list_drama_rows(
        self,
        *,
        language: str,
        now_epoch: int,
        deploy_since_epoch: Optional[int],
        resource_types: Sequence[str],
    ) -> List[DramaSourceRow]:
        ...

    def list_material_rows(
        self,
        *,
        content_id: str,
        language: str,
    ) -> List[MaterialSourceRow]:
        ...


class PublishedMaterialHistory(Protocol):
    def seen_material_ids(self, material_ids: Sequence[str]) -> Iterable[str]:
        ...


class SelectionStore(Protocol):
    def get_task_reservation(self, task_id: int) -> Optional[Mapping[str, Any]]:
        ...

    def reserved_material_ids(self, material_ids: Sequence[str]) -> Iterable[str]:
        ...

    def cooldown_content_ids(
        self,
        *,
        template_id: int,
        content_ids: Sequence[str],
        since_utc: str,
    ) -> Iterable[str]:
        ...

    def reserve_material(
        self,
        *,
        run_id: int,
        task_id: int,
        template_id: int,
        template_version: int,
        account_id: str,
        material_id: str,
        content_id: str,
        series_code: str,
        reserved_at_utc: str,
        cooldown_since_utc: Optional[str],
        claim_token: Optional[str],
        selection_snapshot: Mapping[str, Any],
    ) -> Any:
        """Return reservation/idempotent row; return ``None`` on a race."""
        ...


class StrictMaterialValidator(Protocol):
    def validate(self, material_id: str) -> Mapping[str, Any]:
        ...


class ResolverMaterialValidator:
    """Adapt the hardened strict material resolver without importing its service class."""

    def __init__(self, resolver: Any):
        if not callable(getattr(resolver, "resolve", None)):
            raise ValueError("resolver must expose resolve(material_id)")
        self.resolver = resolver

    def validate(self, material_id: str) -> Mapping[str, Any]:
        try:
            result = self.resolver.resolve(material_id)
        except Exception as exc:
            status = int(getattr(exc, "status", 500) or 500)
            code = str(getattr(exc, "code", "x_auto_material_validation_failed"))
            if status >= 500:
                raise SelectionError(
                    "x_auto_material_validator_unavailable",
                    "strict material validation is unavailable",
                    503,
                ) from None
            raise CandidateRejected(code, "strict material validation rejected the candidate", status) from None
        if not isinstance(result, Mapping):
            raise SelectionError(
                "x_auto_material_validator_invalid",
                "strict material validator returned invalid data",
                503,
            )
        return dict(result)


def _metric_value(item: Any, field_name: str) -> Optional[Decimal]:
    metrics = item.metrics
    return metrics.spend if field_name == "spend" else metrics.d0_roas


def _stable_metric_sort(
    items: Sequence[Any],
    rule: SortRule,
    tie_key: Callable[[Any], Any],
) -> List[Any]:
    defined = [item for item in items if _metric_value(item, rule.field) is not None]
    missing = [item for item in items if _metric_value(item, rule.field) is None]
    # Stable sorts preserve the explicit ascending identity tie-breaker even
    # when the primary metric is descending.
    defined.sort(key=tie_key)
    defined.sort(
        key=lambda item: _metric_value(item, rule.field),
        reverse=rule.direction == "desc",
    )
    missing.sort(key=tie_key)
    return defined + missing


def _https_url(value: Any) -> bool:
    text = str(value or "").strip()
    if not text or len(text) > 4096 or "\\" in text or any(char.isspace() for char in text):
        return False
    try:
        parsed = urllib.parse.urlsplit(text)
        port = parsed.port
    except ValueError:
        return False
    return bool(
        parsed.scheme.lower() == "https"
        and parsed.hostname
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
        and port in (None, 443)
    )


def _validated_public_snapshot(
    value: Mapping[str, Any],
    *,
    required: bool,
) -> Dict[str, str]:
    """Return the strict validator's recovery-safe public field whitelist."""

    if not value:
        if required:
            raise SelectionError(
                "x_auto_material_validator_invalid",
                "strict material validator omitted recovery metadata",
                503,
            )
        return {}
    result: Dict[str, str] = {}
    for field_name, limit in _VALIDATED_PUBLIC_TEXT_LIMITS.items():
        raw = value.get(field_name)
        if raw in (None, ""):
            continue
        if not isinstance(raw, str):
            text = ""
        else:
            text = raw.strip()
        if not text or len(text) > limit or "\x00" in text:
            raise SelectionError(
                "x_auto_material_validator_invalid",
                "strict material validator returned invalid recovery metadata",
                503,
            )
        result[field_name] = text
    return result


def _object_mapping(value: Any) -> Optional[Dict[str, Any]]:
    if value is None:
        return None
    if isinstance(value, Mapping):
        return dict(value)
    as_dict = getattr(value, "as_dict", None)
    if callable(as_dict):
        converted = as_dict()
        if isinstance(converted, Mapping):
            return dict(converted)
    return None


def _drama_snapshot(item: DramaCandidate) -> Dict[str, Any]:
    return {
        "content_id": item.content_id,
        "series_code": item.series_code,
        "language": item.language,
        "resource_type_v2": item.resource_type_v2,
        "deploy_time": item.deploy_time,
        "name": item.name,
        "metrics": {
            "spend": format(item.metrics.spend, "f"),
            "af_revenue0": format(item.metrics.af_revenue0, "f"),
            "d0_roas": None
            if item.metrics.d0_roas is None
            else format(item.metrics.d0_roas, "f"),
        },
    }


def _material_snapshot(item: MaterialCandidate) -> Dict[str, Any]:
    result = {
        "material_id": item.source.material_id,
        "content_id": item.source.content_id,
        "language": item.source.language,
        "video_duration": format(item.source.video_duration, "f"),
        "material_name": item.source.material_name,
        "metrics": {
            "spend": format(item.metrics.spend, "f"),
            "af_revenue0": format(item.metrics.af_revenue0, "f"),
            "d0_roas": None
            if item.metrics.d0_roas is None
            else format(item.metrics.d0_roas, "f"),
        },
    }
    result.update(_validated_public_snapshot(item.validated, required=False))
    return result


class TwoStageSelector:
    def __init__(
        self,
        source: CandidateSource,
        metrics: MetricWindowRepository,
        history: PublishedMaterialHistory,
        store: SelectionStore,
        *,
        material_validator: StrictMaterialValidator,
        product: str = DEFAULT_PRODUCT,
        app_id: int = DEFAULT_APP_ID,
        material_data_source: int = DEFAULT_MATERIAL_DATA_SOURCE,
    ):
        self.source = source
        self.metrics = metrics
        self.history = history
        self.store = store
        if material_validator is None:
            raise ValueError("strict X material validator is required")
        self.material_validator = material_validator
        self.product = str(product)
        self.app_id = int(app_id)
        self.material_data_source = int(material_data_source)
        if (
            self.product != DEFAULT_PRODUCT
            or self.app_id != DEFAULT_APP_ID
            or self.material_data_source != DEFAULT_MATERIAL_DATA_SOURCE
        ):
            raise ValueError("X automatic selection source scope is invalid")

    @staticmethod
    def _reject(counts: Dict[str, int], reason: str) -> None:
        counts[reason] = counts.get(reason, 0) + 1

    def _eligible_dramas(
        self,
        request: SelectionRequest,
        rows: Sequence[DramaSourceRow],
        blacklist: BlacklistSnapshot,
        metrics: MetricWindowSnapshot,
        rejection_counts: Dict[str, int],
    ) -> List[DramaCandidate]:
        language = canonical_language(request.language)
        now_utc = _current_utc(request.now)
        now_epoch = int(now_utc.timestamp())
        launch_days = int(request.rules.drama.launch_window_days)
        earliest = now_epoch - launch_days * 86400 if launch_days else None
        allowed_types = set(request.rules.drama.resource_types)
        grouped: Dict[str, List[DramaSourceRow]] = {}
        for row in rows:
            if (
                row.app_id != self.app_id
                or row.release_status != 1
                or row.language != language
                or row.deploy_time <= 0
                or row.deploy_time > now_epoch
                or (earliest is not None and row.deploy_time < earliest)
                or (allowed_types and row.resource_type_v2 not in allowed_types)
            ):
                self._reject(rejection_counts, "drama_hard_gate")
                continue
            if row.series_code in blacklist.drama_series_codes:
                self._reject(rejection_counts, "drama_blacklist")
                continue
            grouped.setdefault(row.content_id, []).append(row)

        candidates: List[DramaCandidate] = []
        for content_id, group in grouped.items():
            identities = {
                (
                    item.series_code,
                    item.language,
                    item.resource_type_v2,
                    item.name,
                )
                for item in group
            }
            if len(identities) != 1:
                self._reject(rejection_counts, "drama_identity_ambiguous")
                continue
            series_code, row_language, resource_type, name = next(iter(identities))
            totals = metrics.drama(content_id)
            if not request.rules.drama.spend.contains(totals.spend):
                self._reject(rejection_counts, "drama_spend")
                continue
            if not request.rules.drama.d0_roas.contains(totals.d0_roas):
                self._reject(rejection_counts, "drama_d0_roas")
                continue
            candidates.append(
                DramaCandidate(
                    content_id=content_id,
                    series_code=series_code,
                    language=row_language,
                    resource_type_v2=resource_type,
                    deploy_time=max(item.deploy_time for item in group),
                    name=name,
                    metrics=totals,
                )
            )

        cooldown_days = int(request.rules.drama.cooldown_days)
        if cooldown_days and candidates:
            since = utc_iso(now_utc - timedelta(days=cooldown_days))
            blocked = {
                str(value)
                for value in self.store.cooldown_content_ids(
                    template_id=int(request.template_id),
                    content_ids=[item.content_id for item in candidates],
                    since_utc=since,
                )
            }
            kept = []
            for candidate in candidates:
                if candidate.content_id in blocked:
                    self._reject(rejection_counts, "drama_cooldown")
                else:
                    kept.append(candidate)
            candidates = kept
        return _stable_metric_sort(
            candidates,
            request.rules.drama.sort,
            lambda item: item.content_id,
        )

    def _eligible_materials(
        self,
        request: SelectionRequest,
        drama: DramaCandidate,
        rows: Sequence[MaterialSourceRow],
        blacklist: BlacklistSnapshot,
        metrics: MetricWindowSnapshot,
        rejection_counts: Dict[str, int],
    ) -> List[MaterialCandidate]:
        if drama.content_id in blacklist.material_data_source_ids:
            self._reject(rejection_counts, "material_blacklist")
            return []
        language = canonical_language(request.language)
        rule = request.rules.material
        prepared: List[MaterialCandidate] = []
        normalized_rows: List[Tuple[str, MaterialSourceRow]] = []
        identity_counts: Dict[str, int] = {}
        for row in rows:
            try:
                material_id = canonical_material_id(row.material_id)
            except SourceDataError:
                self._reject(rejection_counts, "material_identity_invalid")
                continue
            normalized_rows.append((material_id, row))
            identity_counts[material_id] = identity_counts.get(material_id, 0) + 1
        for material_id, row in normalized_rows:
            if identity_counts[material_id] != 1:
                self._reject(rejection_counts, "material_identity_ambiguous")
                continue
            if (
                row.content_id != drama.content_id
                or row.language != language
                or row.product != self.product
                or row.material_type != 2
                or row.is_delete != 0
                or row.data_source != self.material_data_source
                or not _https_url(row.media_url)
                or row.video_duration <= 0
                or row.video_duration > MAX_SOURCE_DURATION_SECONDS
                or not rule.duration_seconds.contains(row.video_duration)
            ):
                self._reject(rejection_counts, "material_hard_gate")
                continue
            totals = metrics.material(drama.content_id, material_id)
            if not rule.spend.contains(totals.spend):
                self._reject(rejection_counts, "material_spend")
                continue
            if not rule.d0_roas.contains(totals.d0_roas):
                self._reject(rejection_counts, "material_d0_roas")
                continue
            prepared.append(MaterialCandidate(row, totals))

        if not prepared:
            return []
        ids = [item.source.material_id for item in prepared]
        history_seen = {str(value) for value in self.history.seen_material_ids(ids)}
        auto_seen = {str(value) for value in self.store.reserved_material_ids(ids)}
        eligible: List[MaterialCandidate] = []
        for item in prepared:
            material_id = item.source.material_id
            if material_id in history_seen:
                self._reject(rejection_counts, "material_history_seen")
            elif material_id in auto_seen:
                self._reject(rejection_counts, "material_auto_seen")
            else:
                eligible.append(item)
        return _stable_metric_sort(
            eligible,
            rule.sort,
            lambda item: int(item.source.material_id),
        )

    def _validate_material(
        self,
        request: SelectionRequest,
        drama: DramaCandidate,
        material: MaterialCandidate,
    ) -> MaterialCandidate:
        validated = self.material_validator.validate(material.source.material_id)
        returned_material_id = canonical_material_id(
            validated.get("material_id", material.source.material_id)
        )
        returned_content_id = str(validated.get("content_id") or "").strip()
        returned_language = canonical_language(
            validated.get("material_language", request.language)
        )
        if (
            returned_material_id != material.source.material_id
            or returned_content_id != drama.content_id
            or returned_language != canonical_language(request.language)
        ):
            raise SelectionError(
                "x_auto_material_validation_identity_mismatch",
                "strict material identity does not match the selected candidate",
                409,
            )
        public_fields = _validated_public_snapshot(validated, required=True)
        return MaterialCandidate(material.source, material.metrics, public_fields)

    def _select(
        self,
        request: SelectionRequest,
        *,
        reserve: bool,
    ) -> ReservedSelection:
        existing = (
            self.store.get_task_reservation(int(request.task_id))
            if reserve
            else None
        )
        if existing is not None:
            existing_mapping = _object_mapping(existing)
            if existing_mapping is None:
                raise SelectionError(
                    "x_auto_reservation_invalid",
                    "stored task reservation is invalid",
                    500,
                )
            return ReservedSelection(
                reservation=existing_mapping,
                drama=None,
                material=None,
                metric_dates=(),
                initial_blacklist_sha256="",
                final_blacklist_sha256="",
                idempotent=True,
            )

        language = canonical_language(request.language)
        now_utc = _current_utc(request.now)
        now_epoch = int(now_utc.timestamp())
        metric_dates = complete_beijing_dates(
            now_utc,
            int(request.rules.metric_window_days),
        )
        launch_days = int(request.rules.drama.launch_window_days)
        deploy_since = now_epoch - launch_days * 86400 if launch_days else None
        initial_blacklist = self.source.blacklist_snapshot()
        drama_rows = self.source.list_drama_rows(
            language=language,
            now_epoch=now_epoch,
            deploy_since_epoch=deploy_since,
            resource_types=request.rules.drama.resource_types,
        )
        content_ids = tuple(dict.fromkeys(row.content_id for row in drama_rows))
        # Readiness is checked even when the hard-gate source currently has no
        # candidates.  A missing metric day must never be misrepresented as an
        # ordinary empty-selection outcome.
        metric_snapshot = self.metrics.load(
            platform=int(request.rules.platform),
            metric_dates=metric_dates,
            content_ids=content_ids,
        )
        rejection_counts: Dict[str, int] = {}
        dramas = self._eligible_dramas(
            request,
            drama_rows,
            initial_blacklist,
            metric_snapshot,
            rejection_counts,
        )
        cooldown_days = int(request.rules.drama.cooldown_days)
        cooldown_since = (
            utc_iso(now_utc - timedelta(days=cooldown_days))
            if cooldown_days
            else None
        )

        for drama in dramas:
            material_rows = self.source.list_material_rows(
                content_id=drama.content_id,
                language=language,
            )
            materials = self._eligible_materials(
                request,
                drama,
                material_rows,
                initial_blacklist,
                metric_snapshot,
                rejection_counts,
            )
            for material in materials:
                try:
                    validated = self._validate_material(request, drama, material)
                except CandidateRejected:
                    self._reject(rejection_counts, "material_strict_rejected")
                    continue

                # Blacklist and published history can change after initial
                # ranking.  Re-read both immediately before the atomic local
                # reservation.  Cross-system mutual exclusion is deliberately
                # not introduced; this is a fresh exclusion check only.
                final_blacklist = self.source.blacklist_snapshot()
                if drama.series_code in final_blacklist.drama_series_codes:
                    self._reject(rejection_counts, "drama_blacklist_final")
                    break
                if drama.content_id in final_blacklist.material_data_source_ids:
                    self._reject(rejection_counts, "material_blacklist_final")
                    break
                if material.source.material_id in {
                    str(value)
                    for value in self.history.seen_material_ids(
                        [material.source.material_id]
                    )
                }:
                    self._reject(rejection_counts, "material_history_seen_final")
                    continue

                snapshot = {
                    "rules": request.rules.as_dict(),
                    "metric_dates": list(metric_dates),
                    "drama": _drama_snapshot(drama),
                    "material": _material_snapshot(validated),
                    "blacklist": {
                        "initial_sha256": initial_blacklist.sha256,
                        "initial_loaded_at_utc": initial_blacklist.loaded_at_utc,
                        "final_sha256": final_blacklist.sha256,
                        "final_loaded_at_utc": final_blacklist.loaded_at_utc,
                    },
                }
                if not reserve:
                    return ReservedSelection(
                        reservation={},
                        drama=drama,
                        material=validated,
                        metric_dates=metric_dates,
                        initial_blacklist_sha256=initial_blacklist.sha256,
                        final_blacklist_sha256=final_blacklist.sha256,
                    )
                try:
                    reservation = self.store.reserve_material(
                        run_id=int(request.run_id),
                        task_id=int(request.task_id),
                        template_id=int(request.template_id),
                        template_version=int(request.template_version),
                        account_id=str(request.account_id),
                        material_id=material.source.material_id,
                        content_id=drama.content_id,
                        series_code=drama.series_code,
                        reserved_at_utc=utc_iso(now_utc),
                        cooldown_since_utc=cooldown_since,
                        claim_token=request.claim_token or None,
                        selection_snapshot=snapshot,
                    )
                except Exception as exc:
                    conflict_code = str(getattr(exc, "code", ""))
                    if conflict_code == "x_auto_material_already_reserved":
                        self._reject(rejection_counts, "material_reservation_race")
                        continue
                    if conflict_code == "x_auto_drama_in_cooldown":
                        self._reject(rejection_counts, "drama_cooldown_race")
                        break
                    raise
                if reservation is None:
                    self._reject(rejection_counts, "material_reservation_race")
                    continue
                reservation_mapping = _object_mapping(reservation)
                if reservation_mapping is None:
                    raise SelectionError(
                        "x_auto_reservation_invalid",
                        "material reservation result is invalid",
                        500,
                    )
                return ReservedSelection(
                    reservation=reservation_mapping,
                    drama=drama,
                    material=validated,
                    metric_dates=metric_dates,
                    initial_blacklist_sha256=initial_blacklist.sha256,
                    final_blacklist_sha256=final_blacklist.sha256,
                )
        raise NoEligibleMaterial(rejection_counts)

    def preview(self, request: SelectionRequest) -> ReservedSelection:
        """Return the deterministic current winner without reserving anything."""

        return self._select(request, reserve=False)

    def select_and_reserve(self, request: SelectionRequest) -> ReservedSelection:
        return self._select(request, reserve=True)


__all__ = [
    "CandidateRejected",
    "CandidateSource",
    "DecimalRange",
    "DramaCandidate",
    "DramaFilterRule",
    "MaterialCandidate",
    "MaterialFilterRule",
    "MAX_SOURCE_DURATION_SECONDS",
    "NoEligibleMaterial",
    "PublishedMaterialHistory",
    "ReservedSelection",
    "ResolverMaterialValidator",
    "SelectionError",
    "SelectionRequest",
    "SelectionRules",
    "SelectionStore",
    "SortRule",
    "StrictMaterialValidator",
    "TwoStageSelector",
]
