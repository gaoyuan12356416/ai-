"""Freeze strict Page matches for an explicitly selected hit drama.

Ordinary automatic publishing can fall back to its old order. This reverse
match follows X hit-drama publishing: language, positive audience income and
positive theme income under that audience are all required.
"""
from features.hit_publish import profile_score
from collections.abc import Mapping
from copy import deepcopy

from features.hit_drama_target import content_ids
from features.x_accounts.language import canonical_drama_language
from .profile_selection import (
    AUDIENCES, THEMES, Decision, HIERARCHY_VERSION, TAG_MAPPING_VERSION,
    TAXONOMY_VERSION, language, load_classifications, load_profiles,
)

MATCH_POLICY = "language_audience_theme_positive_v1"


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


def _audit(decision, metadata, content_id, lang):
    audience = metadata["audience"]
    weights = decision.node_weights[(audience,)]
    matched = sorted({key for key in metadata.get("themes", []) if weights.get(key, 0) > 0},
                     key=lambda key: (-weights[key], key))
    theme = matched[0]
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
        selection_mode="strict_profile_match", matched=True,
        page_id=decision.page_id, content_id=content_id, language=lang,
        tag_mapping_version=TAG_MAPPING_VERSION, taxonomy_version=TAXONOMY_VERSION,
        hierarchy_version=HIERARCHY_VERSION, profile_levels=["audience", "theme"],
        profile_version=decision.meta.get("version", ""),
        profile_generated_at=decision.meta.get("generated_at", ""),
        source_window={"start": decision.meta.get("start", ""), "end": decision.meta.get("end", "")},
        profile_source_end=decision.meta.get("end", ""),
        profile_stale=bool(decision.meta.get("stale") or decision.meta.get("refresh_failed")),
        profile_status=decision.profile.get("status", ""), classification=classification,
        match_score=profile_score(decision, metadata),
        matched_audience=dict(key=audience, name=AUDIENCES[audience], revenue_cents=decision.weights[audience]),
        matched_themes=[dict(key=key, name=THEMES[key], revenue_cents=weights[key]) for key in matched],
        selected_path=[audience, theme], selected_path_names=[AUDIENCES[audience], THEMES[theme]],
        fallback=False, fallback_level="", fallback_reason="",
    )


def freeze_match(pages, target, mysql, identity, app_id="1479"):
    """Return a new target with only matches from the caller's authorized Pages.

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
    if str(app_id) == "1479":
        snapshot = load_profiles(page_ids)
        try:
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
        elif catalog_error:
            reason = catalog_error
        decision = Decision(page_id, ["hit-drama", identity], snapshot)
        audits, candidate_reasons = {}, []
        if not reason:
            for content_id in ids:
                metadata = catalog.get((content_id, lang))
                rejected = match_reason(decision, metadata)
                if rejected:
                    candidate_reasons.append(rejected)
                else:
                    audits[content_id] = _audit(decision, metadata, content_id, lang)
            if not audits:
                reason = candidate_reasons[0] if candidate_reasons else "candidate_metadata_missing"
        if reason:
            frozen["skipped_pages"].append(dict(page_id=page_id, language=lang, reason=reason))
            continue
        frozen["page_ids"].append(page_id)
        frozen["page_content_ids"][page_id] = list(audits)
        frozen["page_languages"][page_id] = lang
        frozen["profiles"][page_id] = audits
    return frozen


def profileaudit(page_id, content_id, hit_drama):
    """Return an independent copy of the exact selected Page/drama evidence."""
    page_id, content_id = str(page_id), str(content_id)
    audit = hit_drama.get("profiles", {}).get(page_id, {}).get(content_id)
    if (isinstance(audit, Mapping) and audit.get("matched") is True
            and audit.get("page_id") == page_id and audit.get("content_id") == content_id
            and audit.get("match_policy") == MATCH_POLICY):
        return deepcopy(audit)
    return dict(policy_version=MATCH_POLICY, match_policy=MATCH_POLICY,
                selection_mode="strict_profile_match", matched=False,
                page_id=page_id, content_id=content_id,
                fallback=True, fallback_level="all", fallback_reason="profile_match_missing")


def filter_pages(pages, target):
    """Recheck frozen scope and language without adding a newly visible Page."""
    if target.get("match_policy") != MATCH_POLICY:
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
    return result
