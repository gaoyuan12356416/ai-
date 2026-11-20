"""Freeze language-compatible Pages, preferring positive audience/theme evidence."""
from features.hit_publish import profile_score
from collections.abc import Mapping
from copy import deepcopy

from features.hit_drama_target import content_ids
from features.x_accounts.language import canonical_drama_language
from .profile_selection import (
    AUDIENCES, THEMES, Decision, HIERARCHY_VERSION, TAG_MAPPING_VERSION,
    TAXONOMY_VERSION, language, load_classifications, load_profiles,
)

LEGACY_MATCH_POLICY = "language_audience_theme_positive_v1"
MATCH_POLICY = "language_profile_priority_v2"
MATCH_POLICIES = {LEGACY_MATCH_POLICY, MATCH_POLICY}


def _field(page, name, default=None):
    return page.get(name, default) if isinstance(page, Mapping) else getattr(page, name, default)


def _language(value):
    return language(canonical_drama_language(value, default=""))


def _target_ids(target, lang):
    if "by_language" in target:
        return sorted({str(cid) for key, ids in target["by_language"].items()
                       if _language(key) == lang for cid in ids})
    if target.get("languages") and lang not in {_language(key) for key in target["languages"]}:
        return []
    return list(dict.fromkeys(str(cid) for cid in content_ids(target)))


def match_reason(decision, metadata):
    """Any positive matching branch qualifies; no random preference draw."""
    if decision.reason:
        return decision.reason
    if not isinstance(metadata, Mapping) or not metadata:
        return "candidate_metadata_missing"
    audience = metadata.get("audience")
    if audience not in decision.weights:
        return "audience_not_matched"
    weights = decision.node_weights.get((audience,), {})
    if not any(weights.get(theme, 0) > 0 for theme in metadata.get("themes", [])):
        return "theme_not_matched"
    return ""


def _audit(decision, metadata, content_id, lang, reason=""):
    reason = reason or match_reason(decision, metadata)
    metadata = metadata if isinstance(metadata, Mapping) else {}
    audience = metadata.get("audience", "__unknown__")
    weights = decision.node_weights.get((audience,), {}) if not reason else {}
    matched = sorted({key for key in metadata.get("themes", []) if weights.get(key, 0) > 0},
                     key=lambda key: (-weights[key], key))
    theme = matched[0] if matched else ""
    # Freeze only this drama's classification and matching branch. Random
    # preferred-path and unrelated branches would misdescribe strict matching
    # and inflate every Page/content audit copied into a run.
    classification = {key: deepcopy(metadata[key]) for key in (
        "audience", "audience_name", "audience_basis", "audience_rules",
        "themes", "raw_labels", "label_status", "taxonomy_version",
    ) if key in metadata}
    classification.update(content_id=content_id, language=lang)
    return dict(
        policy_version=MATCH_POLICY, match_policy=MATCH_POLICY,
        selection_mode="language_profile_priority", matched=True,
        profile_matched=not bool(reason),
        page_id=decision.page_id, content_id=content_id, language=lang,
        tag_mapping_version=TAG_MAPPING_VERSION, taxonomy_version=TAXONOMY_VERSION,
        hierarchy_version=HIERARCHY_VERSION, profile_levels=["audience", "theme"],
        profile_version=decision.meta.get("version", ""),
        profile_generated_at=decision.meta.get("generated_at", ""),
        source_window={"start": decision.meta.get("start", ""), "end": decision.meta.get("end", "")},
        profile_source_end=decision.meta.get("end", ""),
        profile_stale=bool(decision.meta.get("stale") or decision.meta.get("refresh_failed")),
        profile_status=decision.profile.get("status", ""), classification=classification,
        # The shared one-to-one allocator requires a positive eligible edge.
        # Keep evidence separate: 1 means eligible without profile evidence;
        # every positive profile score ranks above it, within the 1e9 bound.
        profile_score=profile_score(decision, metadata) if not reason else 0,
        match_score=min(1_000_000_000, profile_score(decision, metadata) + 1) if not reason else 1,
        matched_audience=(dict(key=audience, name=AUDIENCES[audience], revenue_cents=decision.weights[audience])
                          if not reason else {}),
        matched_themes=[dict(key=key, name=THEMES[key], revenue_cents=weights[key]) for key in matched],
        selected_path=[audience, theme] if not reason else [],
        selected_path_names=[AUDIENCES[audience], THEMES[theme]] if not reason else [],
        fallback=bool(reason), fallback_level="profile" if reason else "", fallback_reason=reason,
    )


def freeze_match(pages, target, mysql, identity, app_id="1479"):
    """Rank language-compatible targets from the caller's authorized Pages.

    Reads one profile generation and one target classification catalog. Does not
    query materials, write a database or acquire additional Page permissions.
    An empty match is returned for the caller to report before creating a run.
    """
    pages = list(pages)
    frozen = deepcopy(target)
    frozen.update(page_ids=[], page_content_ids={}, page_languages={},
                  skipped_pages=[], profiles={}, match_policy=MATCH_POLICY)
    if not pages:
        return frozen
    page_ids = [str(_field(page, "page_id", "")) for page in pages]
    catalog, catalog_error = {}, ""
    if str(app_id) == "1479" or target.get('catalog') == 'moboreels':
        snapshot = load_profiles(page_ids)
        try:
            if target.get('catalog') == 'moboreels':
                from features.moboreels_hit import classifications
                catalog = classifications(mysql.select, content_ids(target))
            else:
                catalog = load_classifications(mysql, content_ids(target))
        except Exception:
            catalog_error = "candidate_metadata_unavailable"
    else:
        snapshot = ({"pages": {}}, {})
        catalog_error = "unsupported_profile_product"
    for page in pages:
        page_id = str(_field(page, "page_id", ""))
        try:
            lang = _language(_field(page, "language"))
            ids = _target_ids(target, lang)
        except (TypeError, ValueError):
            lang, ids = "", []
        reason = ""
        if not _field(page, "eligible_token_count", 0):
            reason = "page_not_publishable"
        elif not ids:
            reason = "language_not_matched"
        decision = Decision(page_id, ["hit-drama", identity], snapshot)
        audits = {}
        if not reason:
            for content_id in ids:
                metadata = catalog.get((content_id, lang))
                audits[content_id] = _audit(decision, metadata, content_id, lang, catalog_error)
        if reason:
            frozen["skipped_pages"].append(dict(page_id=page_id, language=lang, reason=reason))
            continue
        frozen["page_ids"].append(page_id)
        frozen["page_content_ids"][page_id] = sorted(audits, key=lambda cid: -audits[cid]["match_score"])
        frozen["page_languages"][page_id] = lang
        frozen["profiles"][page_id] = audits
    frozen["page_ids"].sort(key=lambda pid: -max(a["match_score"] for a in frozen["profiles"][pid].values()))
    return frozen


def profileaudit(page_id, content_id, hit_drama):
    """Return an independent copy of the exact selected Page/drama evidence."""
    page_id, content_id = str(page_id), str(content_id)
    audit = hit_drama.get("profiles", {}).get(page_id, {}).get(content_id)
    if (isinstance(audit, Mapping) and audit.get("matched") is True
            and audit.get("page_id") == page_id and audit.get("content_id") == content_id
            and hit_drama.get("match_policy") in MATCH_POLICIES
            and audit.get("match_policy") == hit_drama["match_policy"]):
        return deepcopy(audit)
    return dict(policy_version=MATCH_POLICY, match_policy=MATCH_POLICY,
                selection_mode="language_profile_priority", matched=False,
                page_id=page_id, content_id=content_id,
                fallback=True, fallback_level="all", fallback_reason="profile_match_missing")


def filter_pages(pages, target):
    """Recheck frozen scope and language without adding a newly visible Page."""
    if target.get("match_policy") not in MATCH_POLICIES:
        return []
    allowed = set(target.get("page_ids", []))
    result = []
    for page in pages:
        page_id = str(_field(page, "page_id", ""))
        if page_id not in allowed:
            continue
        try:
            lang = _language(_field(page, "language"))
            current_ids = set(_target_ids(target, lang))
        except (TypeError, ValueError):
            continue
        ids = target.get("page_content_ids", {}).get(page_id, [])
        if lang != target.get("page_languages", {}).get(page_id) or not ids or not set(ids).issubset(current_ids):
            continue
        audits = [profileaudit(page_id, cid, target) for cid in ids]
        if all(audit.get("matched") and audit.get("language") == lang
               and audit.get("classification", {}).get("language") == lang for audit in audits):
            result.append(page)
    if target.get("match_policy") == MATCH_POLICY:
        order = {pid: index for index, pid in enumerate(target["page_ids"])}
        result.sort(key=lambda page: order[str(_field(page, "page_id", ""))])
    return result
