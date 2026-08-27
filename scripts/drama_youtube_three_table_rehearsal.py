#!/usr/bin/env python3
"""RETIRED: the former kunlunads_dev mutation/snapshot workflow is forbidden.

The user superseded it with dedicated fresh ads_ai tables on 2026-08-27.
Historical source/evidence remains in Git history; it is not a bootstrap proof
for the new schema. This tombstone cannot connect, export, load old evidence,
rehearse, create, migrate, or verify a live database.
Use scripts/bootstrap_drama_youtube_ads_ai.py for the new fixed contract.
"""

from __future__ import annotations

import json
import sys

RETIRED = True
LEGACY_SCHEMA = "kunlunads_dev"
RETIRED_REASON = "legacy YouTube table workflow is retired; use the ads_ai CREATE-only bootstrap"


def _retired(*_args, **_kwargs):
    raise RuntimeError(RETIRED_REASON)


export_snapshot = _retired
_connect = _retired
_load_snapshot = _retired
_validate_table_snapshot_evidence = _retired
validate_table_snapshot_evidence = _retired
rehearse_loopback = _retired
main = _retired


if __name__ == "__main__":
    print(json.dumps({"ok": False, "error": "legacy_youtube_workflow_retired",
                      "reason": RETIRED_REASON}, sort_keys=True), file=sys.stderr)
    raise SystemExit(1)
