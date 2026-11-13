"""Fill missing legacy tenant identity using the existing backend user ledger."""
import contextlib
from pathlib import Path
import sqlite3


class OwnerResolver:
    def __init__(self, path):
        self.path = path

    def resolve(self, offers):
        keys = sorted({str(row.get("owner_user_id") or "") for row in offers if not row.get("owner_tenant_key") and row.get("owner_namespace") == "session_user"} - {""})
        if not keys or not self.path:
            return offers
        resolved = {}
        try:
            with contextlib.closing(sqlite3.connect(Path(self.path).resolve().as_uri() + "?mode=ro", uri=True, timeout=3)) as conn:
                conn.row_factory = sqlite3.Row
                for start in range(0, len(keys), 200):
                    page = keys[start:start + 200]
                    rows = conn.execute("SELECT user_id,tenant_key,name FROM drama_admin_user WHERE status='active' AND user_id IN (" + ",".join("?" for _ in page) + ")", page).fetchall()
                    for row in rows:
                        resolved.setdefault(str(row["user_id"]), []).append(dict(row))
        except sqlite3.Error:
            return offers
        for offer in offers:
            if offer.get("owner_tenant_key") or offer.get("owner_namespace") != "session_user":
                continue
            candidates = resolved.get(str(offer.get("owner_user_id") or ""), [])
            if len(candidates) == 1 and candidates[0].get("tenant_key"):
                offer["owner_tenant_key"] = str(candidates[0]["tenant_key"])
                offer["owner_name"] = offer.get("owner_name") or candidates[0].get("name") or ""
        return offers
