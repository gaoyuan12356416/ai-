"""Read existing Page portraits and apply X's audience/theme income preference.

The portrait is auxiliary: missing data keeps the ordinary candidate order. This
module never creates a database, calls a publisher or changes an existing task.
"""
from collections.abc import Mapping
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3

from features.x_tag_revenue.core import language
from features.x_tag_revenue.hierarchy import VERSION as HIERARCHY_VERSION
from features.x_tag_revenue.tags import VERSION as TAG_MAPPING_VERSION
from features.x_tag_revenue.taxonomy import (
    AUDIENCES, THEMES, UNKNOWN, VERSION as TAXONOMY_VERSION,
    classify, collect_classifications,
)

VERSION = "fb-revenue-audience-theme-weighted-v1"
DEFAULT_ROOT = "/mnt/data-disk/fb-page-portraits"
VERSION_RE = r"[0-9]{8}T[0-9]{12}Z"


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def load_profiles(page_ids, *, root=None, now=None):
    """Pin one immutable serving generation, using bounded read-only queries.

    Returns ``({'pages': {page_id: tree}, 'errors': {...}}, metadata)``. A
    damaged individual Page does not discard valid siblings. Global read errors
    return an unavailable marker, without exposing exception/SQL details.
    """
    data = {"pages": {}, "errors": {}}
    try:
        ids = list(dict.fromkeys(str(value) for value in page_ids if str(value)))
        if not ids:
            return data, {}
        directory = Path(root or os.environ.get("FB_AUTO_PROFILE_ROOT", DEFAULT_ROOT)).resolve()
        manifest = json.loads((directory / "current.json").read_text(encoding="utf-8"))
        version = manifest["version"]
        if not isinstance(version, str) or not re.fullmatch(VERSION_RE, version):
            raise ValueError("invalid_profile_version")
        filename = version + ".sqlite3"
        if manifest.get("database") != filename:
            raise ValueError("invalid_profile_database")
        path = (directory / filename).resolve()
        if path.parent != directory:
            raise ValueError("invalid_profile_path")
        # URI mode=ro also rejects a missing file; sqlite's default create mode
        # must never manufacture an empty portrait during an outage.
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=5)) as db:
            row = db.execute("SELECT payload FROM metadata").fetchone()
            meta = json.loads(row[0]) if row else None
            if not isinstance(meta, dict) or meta.get("version") != version:
                raise ValueError("profile_version_mismatch")
            generated = datetime.fromisoformat(meta["generated_at"].replace("Z", "+00:00"))
            if generated.tzinfo is None:
                raise ValueError("profile_generated_time_invalid")
            current = now or datetime.now(timezone.utc)
            if current.tzinfo is None:
                current = current.replace(tzinfo=timezone.utc)
            meta["stale"] = bool(meta.get("stale") or (current - generated).total_seconds() > 10800)
            for offset in range(0, len(ids), 500):
                batch = ids[offset:offset + 500]
                rows = db.execute(
                    "SELECT page_id,tree FROM profiles WHERE page_id IN (" + ",".join("?" for _ in batch) + ")", batch
                )
                for page_id, payload in rows:
                    page_id = str(page_id)
                    try:
                        profile = json.loads(payload)
                        if not isinstance(profile, dict) or str(profile.get("page_id")) != page_id:
                            raise ValueError("profile_page_mismatch")
                        data["pages"][page_id] = profile
                    except (TypeError, ValueError):
                        data["errors"][page_id] = "portrait_invalid"
        return data, meta
    except (OSError, ValueError, TypeError, KeyError, AttributeError, sqlite3.Error):
        return {"pages": {}, "errors": {}}, {"unavailable": True, "version": ""}


def load_classifications(mysql, content_ids):
    """Use the existing gated read-only source and the exact shared taxonomy.

    This taxonomy belongs to Dramawave (app 1479). The caller keeps other
    products on their existing selector and records that fallback explicitly.
    Query failures intentionally propagate to the caller's candidate fallback.
    """
    return collect_classifications(mysql.select, mysql.schema, content_ids)


def weighted_order(weights, seed):
    """Same exponential race as X: first node probability is its income share."""
    ranked = []
    for key, weight in weights.items():
        if weight <= 0:
            continue
        digest = hashlib.sha256((seed + "\0" + key).encode()).digest()
        u = (int.from_bytes(digest[:8], "big") + 1) / float(2 ** 64 + 1)
        ranked.append((-math.log(max(min(u, 1 - 2 ** -53), 2 ** -64)) / weight, key))
    return [key for _, key in sorted(ranked)]


def _cents(value):
    return isinstance(value, int) and not isinstance(value, bool)


class Decision:
    def __init__(self, page_id, identity, snapshot):
        self.page_id, self.identity = str(page_id), identity
        self.meta, self.profile = {}, {}
        self.orders, self.node_weights = {}, {}
        self.reason = ""
        try:
            data, self.meta = snapshot
            self.profile = data.get("pages", {}).get(self.page_id, {})
            profile, meta = self.profile, self.meta
            if meta.get("unavailable"):
                self.reason = "portrait_unavailable"
            elif data.get("errors", {}).get(self.page_id):
                self.reason = data["errors"][self.page_id]
            elif meta.get("missing_dates"):
                self.reason = "portrait_incomplete"
            elif not profile:
                self.reason = "no_revenue_history"
            elif meta.get("taxonomy_version") != TAXONOMY_VERSION:
                self.reason = "portrait_mapping_outdated"
            elif meta.get("hierarchy_version") != HIERARCHY_VERSION or profile.get("hierarchy_version") != HIERARCHY_VERSION:
                self.reason = "portrait_hierarchy_outdated"
            elif profile.get("status") == "signed_adjustment":
                self.reason = "signed_income_adjustment"
            elif not _cents(profile.get("revenue_cents")):
                self.reason = "portrait_invalid"
            elif profile["revenue_cents"] <= 0:
                self.reason = "no_positive_revenue"
            if not self.reason:
                seed = canonical([VERSION, identity, self.page_id, meta.get("version", ""), HIERARCHY_VERSION])
                self._visit(profile.get("audiences", []), (), seed)
                if not self.orders.get(()):
                    self.reason = "no_valid_audience_profile"
        except (TypeError, ValueError, KeyError, AttributeError):
            self.reason = "portrait_invalid"
            self.orders, self.node_weights = {}, {}
            if not isinstance(self.meta, dict):
                self.meta = {}
        self.weights = self.node_weights.get((), {})
        self.audience_order = self.orders.get((), [])

    def _visit(self, nodes, path, seed):
        names = AUDIENCES if not path else THEMES
        valid = isinstance(nodes, list) and bool(nodes) and all(
            isinstance(node, dict) and node.get("share_bp") is not None
            and _cents(node.get("revenue_cents")) and node["revenue_cents"] >= 0
            and node.get("key") in names for node in nodes
        )
        weights = {node["key"]: node["revenue_cents"] for node in nodes
                   if node.get("matchable") and node["key"] != UNKNOWN and node["revenue_cents"] > 0} if valid else {}
        self.node_weights[path] = weights
        self.orders[path] = weighted_order(weights, seed + "\0" + canonical(path))
        if valid and not path:
            for node in nodes:
                if node.get("children"):
                    self._visit(node["children"], (node["key"],), seed)

    @staticmethod
    def _classification(metadata):
        if isinstance(metadata, Mapping) and metadata:
            return metadata
        return classify(metadata or [], status="candidate_metadata_missing" if not metadata else "matched")

    def rank(self, metadata):
        if self.reason:
            return (0, 0)
        data = self._classification(metadata)
        audience = data.get("audience", UNKNOWN)
        position = self.audience_order.index(audience) if audience in self.audience_order else len(self.audience_order)
        themes = self.orders.get((audience,), []) if audience in self.audience_order else []
        if not themes:
            return (position, 0)
        return (position, min((themes.index(key) if key in themes else len(themes)
                              for key in data.get("themes", [UNKNOWN])), default=len(themes)))

    def preferred(self):
        path = []
        for _ in range(2):
            order = self.orders.get(tuple(path), [])
            if not order:
                break
            path.append(order[0])
        return path

    def audit(self, metadata, *, fallback=""):
        data = self._classification(metadata)
        audience = data.get("audience", UNKNOWN)
        if audience not in AUDIENCES:
            audience = UNKNOWN
        themes = [key for key in data.get("themes", [UNKNOWN]) if key in THEMES] or [UNKNOWN]
        order = self.orders.get((audience,), []) if audience in self.audience_order else []
        matched = next((key for key in order if key in themes), "")
        preferred = self.preferred()
        selected = [audience, matched or themes[0]]
        reason = self.reason or fallback
        level = "all" if reason else ""
        if metadata is not None and not reason:
            if not preferred or audience != preferred[0]:
                reason, level = "preferred_audience_no_eligible_material", "audience"
            elif not order:
                reason, level = "no_valid_theme_profile", "theme"
            elif not matched:
                reason, level = "no_matching_eligible_theme", "theme"
            elif matched != order[0]:
                reason, level = "preferred_theme_no_eligible_material", "theme"
        names = (AUDIENCES, THEMES)
        return dict(
            policy_version=VERSION, page_id=self.page_id,
            tag_mapping_version=TAG_MAPPING_VERSION, taxonomy_version=TAXONOMY_VERSION,
            hierarchy_version=HIERARCHY_VERSION, profile_levels=["audience", "theme"],
            classification=data, preferred_path=preferred,
            preferred_path_names=[names[i][key] for i, key in enumerate(preferred)],
            selected_path=selected if metadata is not None else [],
            selected_path_names=[names[i][key] for i, key in enumerate(selected)] if metadata is not None else [],
            fallback=bool(reason), fallback_level=level, fallback_reason=reason,
            profile_version=self.meta.get("version", ""),
            profile_generated_at=self.meta.get("generated_at", ""),
            source_window={"start": self.meta.get("start", ""), "end": self.meta.get("end", "")},
            profile_source_end=self.meta.get("end", ""),
            profile_stale=bool(self.meta.get("stale") or self.meta.get("refresh_failed")),
            profile_status=self.profile.get("status", "") if isinstance(self.profile, dict) else "",
            candidate_labels=data.get("raw_labels", []), candidate_tag_ids=themes,
            weights=[{"key": key, "name": AUDIENCES[key], "revenue_cents": value}
                     for key, value in sorted(self.weights.items())],
            conditional_weights=[{"path": list(path), "weights": weights}
                                 for path, weights in self.node_weights.items() if path in ((), (audience,))],
        )
