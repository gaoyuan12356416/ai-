#!/usr/bin/env python3
"""Explicit product-restriction recovery. This tool never sends a POST."""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from features.cover_material_source import Catalog, Ledger, ROOT, encoded, file_lock, reconcile


def recover(ledger, catalog, key, action, reference):
    if not re.fullmatch(r"[A-Za-z0-9_:./-]{3,160}", reference):
        raise ValueError("audit_reference_required")
    with file_lock(ledger.root / "locks" / (key + ".lock")):
        with file_lock(ledger.root / "outbound.lock"):
            if not (ledger.root / "HOLD").exists():
                raise ValueError("persistent_hold_required")
            row = ledger.get(key)
            if row is None:
                raise ValueError("upload_not_found")
            row = reconcile(ledger, catalog, row)
            if row["state"] == "confirmed":
                return row
            if row["state"] == "blocked" or row["api_id"] or row["final_id"]:
                raise ValueError("conflicting_or_accepted_receipt_requires_reconciliation")
            receipts = [json.loads(r[0]) for r in ledger.db.execute(
                "SELECT detail_json FROM events WHERE key=? AND state IN ('unknown','rejected') "
                "AND id > COALESCE((SELECT MAX(id) FROM events WHERE key=? AND state='outbound_started'),0) "
                "ORDER BY id", (key, key))]
            # Only the most recent HTTP receipt can establish a product rejection.
            receipts = [r for r in receipts if "http_status" in r]
            if not receipts or receipts[-1].get("code") != 403:
                raise ValueError("explicit_product_rejection_receipt_required")
            if action == "classify":
                if row["state"] not in ("unknown", "rejected"):
                    raise ValueError("rejection_classification_not_applicable")
                return ledger.change(key, "rejected", {"action": "classify_product_restriction",
                    "operator_reference": reference}, error="business_product_restricted")
            if action != "rearm" or row["state"] != "rejected":
                raise ValueError("only_explicit_rejection_can_be_rearmed")
            # The caller must first obtain upstream confirmation that the restriction
            # was removed. Freeze the same request/key and retain all past attempts.
            return ledger.change(key, "prepared", {"action": "operator_authorized_same_request_retry",
                "upstream_fix_reference": reference, "previous_attempts": row["attempts"]}, error="")


def main():
    import os
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("classify", "rearm"))
    parser.add_argument("--root", default=ROOT)
    parser.add_argument("--key", required=True)
    parser.add_argument("--reference", required=True,
                        help="User confirmation for classify; upstream fix evidence for rearm")
    args = parser.parse_args()
    ledger = Ledger(args.root)
    try:
        row = recover(ledger, Catalog(dict(os.environ)), args.key, args.action, args.reference)
        print(encoded({k: row[k] for k in ("key", "state", "attempts", "api_id", "final_id", "error")}))
    finally:
        ledger.close()


if __name__ == "__main__":
    main()
