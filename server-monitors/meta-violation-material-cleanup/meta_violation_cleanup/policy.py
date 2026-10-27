"""Pure, fail-closed selection rules for the scheduled Meta material cleanup.

No function in this module reads credentials or performs platform/database I/O.
All identities must be supplied by the caller's frozen, read-only evidence.
"""
import json
import re
from datetime import datetime, timedelta, timezone


ROOT_PRODUCTS = {1479: "DramaWave", 3302: "HotDrama"}
POLICY_VERSION = "20261027-v3"
POLICY_CONTRACT = {
    "min_distinct_violating_ads": 5,
    "lookback_days": 3,
    "pause_scope": "any_level",
    "allow_active": False,
}
POLICY_NAMES = (
    "Adult Nudity and Sexual Activity",
    "Adult Sexual Solicitation and Sexually Explicit Language",
    "Adult Sexual Exploitation",
    "Human Exploitation",
    "Violence & Graphic Content",
    "Spam",
    "Drugs and Pharmaceuticals",
)
_POSITIVE_ID = re.compile(r"[1-9][0-9]*\Z")
_BEIJING = timezone(timedelta(hours=8))
_LIVE = {"ACTIVE", "PAUSED"}
_EFFECTIVE = _LIVE | {"ADSET_PAUSED", "CAMPAIGN_PAUSED"}
_EMPTY_FEEDBACK_CONTAINERS = {
    "global", "placement_specific", "by_adgroup", "by_asset", "placements"
}


def _id(value):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("invalid_positive_id")
    text = str(value).strip()
    if not _POSITIVE_ID.fullmatch(text):
        raise ValueError("invalid_positive_id")
    return text


def _sorted_ids(values):
    return sorted(set(values), key=int)


def parse_ids(value):
    """Parse complete positive IDs; a capacity-length VARCHAR is ambiguous.

    None/blank/empty arrays represent an absent optional field. A scalar zero is
    deliberately invalid here; lineage ingestion separately recognizes the
    business database's exact zero placeholder as absent evidence.
    """
    if value is None:
        return []
    if isinstance(value, str):
        if len(value) >= 768:
            raise ValueError("possibly_truncated_ids")
        text = value.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                items = json.loads(text)
            except (TypeError, ValueError):
                raise ValueError("malformed_ids") from None
            if not isinstance(items, list):
                raise ValueError("malformed_ids")
        else:
            items = text.split(",")
    elif isinstance(value, list):
        items = value
    elif isinstance(value, int) and not isinstance(value, bool):
        items = [value]
    else:
        raise ValueError("malformed_ids")
    return _sorted_ids(_id(item) for item in items)


def _optional_ids(value, capacity=768):
    if isinstance(value, str) and len(value) >= capacity:
        raise ValueError("possibly_truncated_ids")
    if value == 0 and not isinstance(value, bool) or isinstance(value, str) and value.strip() == "0":
        return []
    return parse_ids(value)


def product_scope(settings):
    """Map the two explicit roots and only their direct configured children."""
    roots = {str(key) for key in ROOT_PRODUCTS}
    result = {key: key for key in roots}
    seen, conflicts = {}, set()
    for row in settings:
        try:
            pid = _id(row.get("id"))
            parent = _id(row.get("ads_app_id"))
        except (AttributeError, ValueError):
            continue
        if pid in roots:
            continue
        if pid in seen and seen[pid] != parent:
            conflicts.add(pid)
        seen[pid] = parent
    for pid, parent in seen.items():
        if parent in roots and pid not in conflicts:
            result[pid] = parent
    return result


def _source_materials(source_ids, sources):
    material_ids = set()
    for sid in source_ids:
        row = sources.get(sid, sources.get(int(sid)))
        if not isinstance(row, dict):
            raise ValueError("source_missing")
        if str(row.get("source_type", "")) != "3":
            raise ValueError("source_type_unverified")
        if row.get("id") is not None and _id(row["id"]) != sid:
            raise ValueError("source_identity_conflict")
        mids = _optional_ids(row.get("source_id"), capacity=255)
        if not mids:
            raise ValueError("source_material_missing")
        material_ids.update(mids)
    return material_ids


def material_identity(ad_rows, sources, scope, creative_id=None):
    """Resolve one ad's current material set without guessing between ledgers."""
    result = {"material_ids": [], "root_id": "", "product_ids": [],
              "ad_id": "", "creative_id": "", "reason": ""}
    try:
        current_creative = _id(creative_id) if creative_id is not None else None
        rows = list(ad_rows or [])
        if not all(isinstance(row, dict) for row in rows):
            raise ValueError("ad_lineage_malformed")
        if current_creative is not None:
            rows = [row for row in rows if str(row.get("creative_id") or "").strip() == current_creative]
        if not rows:
            raise ValueError("current_creative_lineage_missing" if current_creative else "ad_lineage_missing")
        ad_ids, product_ids, roots, materials, creatives = set(), set(), set(), [], set()
        for row in rows:
            ad_ids.add(_id(row.get("ad_id")))
            pid = _id(row.get("product", row.get("product_id")))
            root = str(scope.get(pid, ""))
            if root not in {str(key) for key in ROOT_PRODUCTS}:
                raise ValueError("product_out_of_scope")
            product_ids.add(pid)
            roots.add(root)
            source_ids = _optional_ids(row.get("source_id"))
            original = set(_optional_ids(row.get("original_source_id")))
            linked = _source_materials(source_ids, sources) if source_ids else set()
            if linked and original and linked != original:
                raise ValueError("source_original_conflict")
            mids = linked or original
            if not mids:
                raise ValueError("material_identity_missing")
            materials.append(mids)
            if row.get("creative_id") not in (None, "", 0, "0"):
                creatives.add(_id(row["creative_id"]))
        if len(ad_ids) != 1 or len(roots) != 1 or len(product_ids) != 1:
            raise ValueError("ad_product_identity_conflict")
        if any(mids != materials[0] for mids in materials[1:]):
            raise ValueError("ad_material_identity_conflict")
        if len(creatives) > 1:
            raise ValueError("ad_creative_identity_conflict")
        result.update(material_ids=_sorted_ids(materials[0]), root_id=next(iter(roots)),
                      product_ids=_sorted_ids(product_ids), ad_id=next(iter(ad_ids)),
                      creative_id=current_creative or next(iter(creatives), ""))
    except (TypeError, ValueError) as exc:
        result["reason"] = str(exc) if isinstance(exc, ValueError) else "ad_lineage_malformed"
    return result


def _json_value(raw):
    if isinstance(raw, str):
        return json.loads(raw) if raw.strip() else None
    return raw


def qualifying_policies(raw):
    try:
        value = _json_value(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(value, dict) or not isinstance(value.get("global"), dict):
        return []
    return [name for name in POLICY_NAMES if name in value["global"]]


def _instant(value):
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    else:
        raise ValueError("invalid_timestamp")
    return parsed.replace(tzinfo=_BEIJING) if parsed.tzinfo is None else parsed


def build_evidence(violations, lineage_by_ad, sources, scope, start, end):
    """Count distinct violating ads, never policy hits or ambiguous materials."""
    start_at, end_at = _instant(start), _instant(end)
    if start_at >= end_at:
        raise ValueError("invalid_window")
    materials, excluded = {}, []
    for violation in violations:
        if not isinstance(violation, dict):
            excluded.append({"violation_id": "", "ad_id": "", "reason": "violation_malformed"})
            continue
        ad_id = str(violation.get("ad_id") or "")
        try:
            ad_id = _id(ad_id)
            at = _instant(violation.get("created_at"))
            if not start_at <= at < end_at:
                raise ValueError("outside_window")
            policies = qualifying_policies(violation.get("issues_info"))
            if not policies:
                raise ValueError("no_qualifying_policy")
            rows = lineage_by_ad.get(ad_id, lineage_by_ad.get(int(ad_id), []))
            identity = material_identity(rows, sources, scope)
            if identity["reason"]:
                raise ValueError(identity["reason"])
            if identity["ad_id"] != ad_id:
                raise ValueError("violation_ad_identity_conflict")
            mapped = set(identity["material_ids"])
            explicit_sources = _optional_ids(violation.get("source_id"))
            explicit = _source_materials(explicit_sources, sources) if explicit_sources else set()
            if explicit and not explicit.issubset(mapped):
                raise ValueError("violation_material_identity_conflict")
            if explicit:
                if len(explicit) != 1:
                    raise ValueError("ambiguous_violation_material")
                mid = next(iter(explicit))
            elif len(mapped) == 1:
                mid = next(iter(mapped))
            else:
                raise ValueError("ambiguous_violation_material")
            entry = materials.setdefault(mid, {"violating_ad_ids": set(), "policies": set(), "root_ids": set()})
            entry["violating_ad_ids"].add(ad_id)
            entry["policies"].update(policies)
            entry["root_ids"].add(identity["root_id"])
        except (TypeError, ValueError, OverflowError) as exc:
            excluded.append({"violation_id": str(violation.get("id") or ""), "ad_id": ad_id,
                             "reason": str(exc) if isinstance(exc, ValueError) else "violation_malformed"})
    output = {}
    for mid in _sorted_ids(materials):
        entry = materials[mid]
        output[mid] = {"violating_ad_ids": _sorted_ids(entry["violating_ad_ids"]),
                       "policies": [p for p in POLICY_NAMES if p in entry["policies"]],
                       "root_ids": _sorted_ids(entry["root_ids"]), "count": len(entry["violating_ad_ids"])}
    return {"materials": output, "excluded": excluded}


def _feedback_empty(value):
    value = _json_value(value)
    if value is None:
        return True
    if isinstance(value, list):
        return not value
    if isinstance(value, dict):
        return all(key in _EMPTY_FEEDBACK_CONTAINERS and _feedback_empty(item)
                   for key, item in value.items())
    raise ValueError("review_feedback_unreadable")


def qualified_material_ids(evidence):
    """Validate frozen counts before using the five-distinct-Ad lookup gate."""
    materials = evidence.get("materials", {})
    if not isinstance(materials, dict):
        raise ValueError("evidence_count_conflict")
    qualified = set()
    for mid, entry in materials.items():
        mid = _id(mid)
        if not isinstance(entry, dict):
            raise ValueError("evidence_count_conflict")
        ids = parse_ids(entry.get("violating_ad_ids"))
        count = entry.get("count")
        if type(count) is not int or count != len(ids):
            raise ValueError("evidence_count_conflict")
        if count >= POLICY_CONTRACT["min_distinct_violating_ads"]:
            qualified.add(mid)
    return qualified


def evaluate_ad(ad, identity, evidence):
    """Five distinct violations AND an explicit Ad/AdSet/Campaign pause."""
    result = {"eligible": False, "reason": "", "rules": [], "material_ids": [],
              "root_id": "", "creative_id": "", "ad_id": ""}
    try:
        if not isinstance(ad, dict) or not isinstance(identity, dict):
            raise ValueError("current_ad_malformed")
        result["ad_id"] = _id(ad.get("id"))
        result["creative_id"] = _id((ad.get("creative") or {}).get("id"))
        if identity.get("reason"):
            raise ValueError(identity["reason"])
        if identity.get("ad_id") != result["ad_id"]:
            raise ValueError("current_ad_identity_conflict")
        if identity.get("creative_id") and identity["creative_id"] != result["creative_id"]:
            raise ValueError("current_creative_identity_conflict")
        root = str(identity.get("root_id") or "")
        if root not in {str(key) for key in ROOT_PRODUCTS}:
            raise ValueError("product_out_of_scope")
        mids = parse_ids(identity.get("material_ids"))
        if not mids:
            raise ValueError("material_identity_missing")
        result.update(root_id=root, material_ids=mids)
        status = str(ad.get("status") or "").upper()
        effective = str(ad.get("effective_status") or "").upper()
        campaign = str((ad.get("campaign") or {}).get("status") or "").upper()
        adset = str((ad.get("adset") or {}).get("status") or "").upper()
        if status not in _LIVE or effective not in _EFFECTIVE or campaign not in _LIVE or adset not in _LIVE:
            raise ValueError("current_status_not_eligible")
        closed = "PAUSED" in (status, campaign, adset)
        if ((effective == "ACTIVE" and closed) or
                (effective == "PAUSED" and status != "PAUSED") or
                (effective == "ADSET_PAUSED" and adset != "PAUSED") or
                (effective == "CAMPAIGN_PAUSED" and campaign != "PAUSED") or
                (effective != "ACTIVE" and not closed)):
            raise ValueError("current_status_conflict")
        try:
            review_clear = all(_feedback_empty(ad.get(key)) for key in ("issues_info", "ad_review_feedback"))
        except (TypeError, ValueError):
            raise ValueError("review_feedback_unreadable") from None
        if not review_clear:
            raise ValueError("current_review_issue")
        counts = []
        entries = evidence.get("materials", {})
        for mid in mids:
            entry = entries.get(mid)
            if entry is None:
                continue
            ids = parse_ids(entry.get("violating_ad_ids"))
            count = entry.get("count")
            if isinstance(count, bool) or not isinstance(count, int) or count != len(ids):
                raise ValueError("evidence_count_conflict")
            counts.append(count)
        rules = []
        if closed and any(count >= POLICY_CONTRACT["min_distinct_violating_ads"] for count in counts):
            rules.append("paused_material_5")
        reason = "" if rules else "active_ad_protected" if not closed else "violation_threshold_not_met"
        result.update(eligible=bool(rules), rules=rules, reason=reason)
    except (AttributeError, TypeError, ValueError) as exc:
        result["reason"] = str(exc) if isinstance(exc, ValueError) else "current_ad_malformed"
    return result
