"""Frozen, best-effort material capacity observations; never sends a message."""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone

from features.x_accounts.language import canonical_drama_language

EVENT = "x_material_language_shortage_v1"


def snapshot(accounts, planned, *, scanned=0, scan_limit=1000, rejections=None):
    """Only verified usable accounts count as demand; locked accounts are absent."""
    try:
        selected = {int(item["account_id"]) for item in planned}
        required, matched = Counter(), Counter()
        for account in accounts:
            language = canonical_drama_language(account.get("drama_language"))
            required[language] += 1
            if int(account["id"]) in selected:
                matched[language] += 1
        rows = []
        for language, count in sorted(required.items()):
            if matched[language] < count:
                rows.append({"language": language, "required": count,
                             "matched": matched[language], "missing": count - matched[language],
                             "rejections": dict((rejections or {}).get(language, {}))})
        return {"languages": rows, "scanned": scanned, "scan_limit": scan_limit,
                "scan_limit_reached": scanned >= scan_limit}
    except Exception:
        # Observability must not change reservation/publishing behavior.
        return None


def observe_rejections(totals, candidates, rejections):
    try:
        languages = {str(c["material_id"]): canonical_drama_language(
            c.get("material_language") or c.get("language")) for c in candidates}
        for rejection in rejections:
            language = languages.get(str(rejection.get("material_id")))
            if language:
                counts = totals.setdefault(language, {})
                code = str(rejection.get("error_code") or "media_preflight_failed")
                counts[code] = counts.get(code, 0) + 1
    except Exception:
        pass


def emit(identity, observation):
    """Journald is the outbox source; the independent notifier owns delivery."""
    try:
        if identity.get("source_type") != "material" or not observation or not observation.get("languages"):
            return
        event = {"event": EVENT, "run_date": identity["run_date"],
                 "publish_time": identity["publish_time"], "config_version": identity["version"],
                 "observed_at": datetime.now(timezone.utc).isoformat(), **observation}
        print(json.dumps(event, ensure_ascii=False, sort_keys=True), flush=True)
    except Exception:
        # A broken output stream cannot stop an accepted batch.
        pass
