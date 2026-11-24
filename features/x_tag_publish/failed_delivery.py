"""Keep the identity of the authenticated worker failure for later readback."""
import logging
import sqlite3

DDL = """CREATE TABLE IF NOT EXISTS x_tag_failed_delivery (
    receipt_id TEXT PRIMARY KEY, job_id INTEGER NOT NULL, source_key TEXT NOT NULL,
    generation INTEGER NOT NULL, node_id TEXT NOT NULL, error_code TEXT NOT NULL)"""


def record(bridge, job, receipt):
    if job.get("stage") != "publish" or not receipt.get("receipt_id"):
        return
    try:
        with bridge.connect() as db:
            db.execute(DDL)
            db.execute("INSERT OR IGNORE INTO x_tag_failed_delivery VALUES (?,?,?,?,?,?)",
                       (receipt["receipt_id"], job["id"], job["source_key"], job["generation"],
                        receipt["node_id"], str(receipt.get("error_code") or "")[:120]))
    except (sqlite3.Error, KeyError) as exc:
        # Losing auxiliary evidence keeps this queue in review, never replays it.
        logging.getLogger(__name__).warning("X failure receipt not recorded for %s (%s)",
                                            job.get("id"), type(exc).__name__)
