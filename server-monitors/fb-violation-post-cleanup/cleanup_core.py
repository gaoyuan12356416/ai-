"""Durable, fail-closed execution for the one-off violation Post cleanup.

This module never discovers credentials or retries a DELETE. The CLI owns an
exclusive process lock while using a ledger; adapters return only safe metadata.
"""

import concurrent.futures
import datetime as dt
import hashlib
import json
import re
import sqlite3
import threading
from pathlib import Path


ALLOWED_GROUPS = frozenset((3, 4, 24, 25))
POST_ID = re.compile(r"^([0-9]+)_([0-9]+)$")
SECRET_KEYS = re.compile(r"token|secret|password|authorization|appsecret", re.I)
STATUSES = ("pending", "deleting", "deleted", "failed", "unknown", "skipped")


def utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def parse_timestamp(value):
    """Reject naive timestamps instead of guessing the execution host's zone."""
    if isinstance(value, dt.datetime):
        parsed = value
    else:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include an explicit timezone")
    return parsed.astimezone(dt.timezone.utc)


def _safe(value):
    if isinstance(value, dict):
        return {str(key): ("[REDACTED]" if SECRET_KEYS.search(str(key)) else _safe(item))
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value]
    if isinstance(value, str):
        value = re.sub(r"(?i)(access_token|appsecret_proof|authorization|password)=([^&\s]+)",
                       r"\1=[REDACTED]", value)
        value = re.sub(r"(?i)Bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", value)
        return value
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def manifest_bytes(manifest):
    return (_json(manifest) + "\n").encode("utf-8")


def manifest_sha256(manifest):
    return hashlib.sha256(manifest_bytes(manifest)).hexdigest()


def load_manifest(path, expected_sha256):
    """Require the frozen canonical byte representation and its supplied hash."""
    raw = Path(path).read_bytes()
    if not re.fullmatch(r"[0-9a-fA-F]{64}", str(expected_sha256)):
        raise ValueError("an explicit SHA256 is required")
    if hashlib.sha256(raw).hexdigest().lower() != str(expected_sha256).lower():
        raise ValueError("manifest SHA256 mismatch")
    manifest = json.loads(raw.decode("utf-8"))
    if raw != manifest_bytes(manifest):
        raise ValueError("manifest must use the frozen canonical serialization")
    _validate_manifest(manifest)
    return manifest


def canonicalize_evidence(evidence, window_start, window_end):
    """Merge by real Page_Post ID, preserving every distinct source row.

    source_row_id, material_id, and post_id are deliberately separate namespaces.
    Bare video IDs remain unresolved until the scan adapter resolves a real Post.
    """
    start, end = parse_timestamp(window_start), parse_timestamp(window_end)
    if start > end:
        raise ValueError("window_start must not be after window_end")
    merged, unresolved = {}, []
    for original in evidence:
        row = _safe(dict(original))
        post_id = str(row.get("post_id") or row.get("real_post_id") or "")
        page_id = str(row.get("page_id") or "")
        match = POST_ID.fullmatch(post_id)
        if not match:
            unresolved.append({"reason": "real_post_id_unresolved", "evidence": row})
            continue
        target = merged.setdefault(post_id, {
            "post_id": post_id, "page_id": page_id, "material_ids": [],
            "groups": [], "evidence": [],
        })
        if row not in target["evidence"]:
            target["evidence"].append(row)
        if match.group(1) != page_id or page_id != target["page_id"]:
            target["skip_reason"] = "conflicting_page_identity"
        material_id = str(row.get("material_id") or "")
        if not material_id.isdigit():
            target["skip_reason"] = "material_identity_missing"
        elif material_id not in target["material_ids"]:
            target["material_ids"].append(material_id)
        try:
            groups = {int(group) for group in row.get("groups", row.get("group_ids", []))}
        except (TypeError, ValueError):
            groups = set()
        selected_groups = groups & ALLOWED_GROUPS
        if not selected_groups:
            target["skip_reason"] = "outside_authorized_groups"
        target["groups"] = sorted(set(target["groups"]) | selected_groups)
        if "fb_violation" in row and row["fb_violation"] != 1:
            target["skip_reason"] = "material_not_currently_violating"
    targets = []
    for post_id in sorted(merged):
        target = merged[post_id]
        target["material_ids"].sort()
        target["evidence"].sort(key=_json)
        if len(target["material_ids"]) > 1:
            target["skip_reason"] = "conflicting_material_identity"
        targets.append(target)
    return {
        "version": 1, "window_start": start.isoformat(), "window_end": end.isoformat(),
        "allowed_groups": sorted(ALLOWED_GROUPS), "targets": targets,
        "unresolved": sorted(unresolved, key=_json),
    }


def _validate_manifest(manifest):
    if manifest.get("version") != 1:
        raise ValueError("unsupported manifest version")
    start = parse_timestamp(manifest["window_start"])
    end = parse_timestamp(manifest["window_end"])
    if start > end or end - start > dt.timedelta(days=90):
        raise ValueError("manifest window must be ordered and no longer than 90 days")
    if set(manifest.get("allowed_groups", [])) != ALLOWED_GROUPS:
        raise ValueError("manifest does not match the authorized Page groups")
    seen = set()
    for target in manifest["targets"]:
        post_id = str(target["post_id"])
        if post_id in seen:
            raise ValueError("duplicate post_id in manifest")
        seen.add(post_id)
        if target.get("skip_reason"):
            continue
        match = POST_ID.fullmatch(post_id)
        if not match or match.group(1) != str(target["page_id"]):
            raise ValueError("manifest contains an invalid Page_Post identity")
        groups = set(target.get("groups", []))
        if not groups or not groups.issubset(ALLOWED_GROUPS):
            raise ValueError("manifest target is outside the authorized Page groups")
        materials = target.get("material_ids", [])
        if not materials or any(not str(item).isdigit() for item in materials):
            raise ValueError("manifest target requires material IDs")


class Ledger:
    """SQLite FULL-synchronous intent and result journal, safe across worker threads."""

    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute("PRAGMA busy_timeout=30000")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS targets (
                post_id TEXT PRIMARY KEY, page_id TEXT NOT NULL,
                target_json TEXT NOT NULL, status TEXT NOT NULL,
                credential_id TEXT, is_canary INTEGER NOT NULL DEFAULT 0,
                verification TEXT, result_json TEXT, readback_json TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, post_id TEXT NOT NULL,
                event TEXT NOT NULL, data_json TEXT NOT NULL, created_at TEXT NOT NULL
            );
        """)
        self._db.commit()

    def close(self):
        with self._lock:
            self._db.close()

    def _event(self, post_id, event, data):
        self._db.execute(
            "INSERT INTO events(post_id,event,data_json,created_at) VALUES(?,?,?,?)",
            (post_id, event, _json(_safe(data)), utc_now()),
        )

    def import_manifest(self, manifest):
        _validate_manifest(manifest)
        digest = manifest_sha256(manifest)
        with self._lock, self._db:
            row = self._db.execute("SELECT value FROM metadata WHERE key='manifest_sha256'").fetchone()
            if row and row["value"] != digest:
                raise ValueError("ledger is already bound to a different frozen manifest")
            self._db.execute("INSERT OR IGNORE INTO metadata(key,value) VALUES('manifest_sha256',?)", (digest,))
            for target in manifest["targets"]:
                post_id = str(target["post_id"])
                state = "skipped" if target.get("skip_reason") else "pending"
                cursor = self._db.execute(
                    "INSERT OR IGNORE INTO targets(post_id,page_id,target_json,status,result_json,updated_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (post_id, str(target["page_id"]), _json(_safe(target)), state,
                     _json({"reason": target["skip_reason"]}) if state == "skipped" else None, utc_now()),
                )
                if cursor.rowcount:
                    self._event(post_id, "manifest_imported", {"status": state, "manifest_sha256": digest})
        return digest

    def recover_inflight(self):
        """Call only while holding the CLI's exclusive ledger process lock."""
        with self._lock, self._db:
            rows = self._db.execute("SELECT post_id FROM targets WHERE status='deleting'").fetchall()
            for row in rows:
                self._db.execute(
                    "UPDATE targets SET status='unknown', result_json=?, updated_at=? WHERE post_id=?",
                    (_json({"reason": "interrupted_after_durable_delete_intent"}), utc_now(), row["post_id"]),
                )
                self._event(row["post_id"], "interrupted_delete_unknown", {})
            return len(rows)

    def get(self, post_id):
        with self._lock:
            row = self._db.execute("SELECT * FROM targets WHERE post_id=?", (post_id,)).fetchone()
        return self._decode(row) if row else None

    @staticmethod
    def _decode(row):
        decoded = dict(row)
        for column in ("target_json", "result_json", "readback_json"):
            value = decoded.pop(column)
            decoded[column[:-5]] = json.loads(value) if value is not None else None
        decoded["is_canary"] = bool(decoded["is_canary"])
        return decoded

    def rows(self):
        with self._lock:
            rows = self._db.execute("SELECT * FROM targets ORDER BY post_id").fetchall()
        return [self._decode(row) for row in rows]

    def events(self, post_id=None):
        with self._lock:
            if post_id is None:
                rows = self._db.execute("SELECT * FROM events ORDER BY id").fetchall()
            else:
                rows = self._db.execute("SELECT * FROM events WHERE post_id=? ORDER BY id", (post_id,)).fetchall()
        return [dict(row) for row in rows]

    def skip(self, post_id, reason, details=None):
        payload = {"reason": reason, "details": _safe(details)}
        with self._lock, self._db:
            cursor = self._db.execute(
                "UPDATE targets SET status='skipped',result_json=?,updated_at=? WHERE post_id=? AND status='pending'",
                (_json(payload), utc_now(), post_id),
            )
            if cursor.rowcount:
                self._event(post_id, "skipped_before_delete", payload)
        return bool(cursor.rowcount)

    def claim(self, post_id, credential_id, precheck, inspection, is_canary=False):
        """Commit the intention before returning permission to call DELETE."""
        with self._lock, self._db:
            cursor = self._db.execute(
                "UPDATE targets SET status='deleting',credential_id=?,is_canary=?,updated_at=? "
                "WHERE post_id=? AND status='pending'",
                (str(credential_id), int(is_canary), utc_now(), post_id),
            )
            if cursor.rowcount:
                self._event(post_id, "delete_intent", {
                    "credential_id": credential_id, "precheck": precheck,
                    "inspection": inspection, "is_canary": is_canary,
                })
        return bool(cursor.rowcount)

    def finish(self, post_id, status, result):
        if status not in ("deleted", "failed", "unknown", "skipped"):
            raise ValueError("invalid DELETE outcome")
        with self._lock, self._db:
            cursor = self._db.execute(
                "UPDATE targets SET status=?,result_json=?,updated_at=?, "
                "is_canary=CASE WHEN ?='skipped' THEN 0 ELSE is_canary END "
                "WHERE post_id=? AND status='deleting'",
                (status, _json(_safe(result)), utc_now(), status, post_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("DELETE result has no matching durable intention")
            self._event(post_id, "delete_not_sent" if status == "skipped" else "delete_" + status, result)

    def record_readback(self, post_id, readback):
        with self._lock, self._db:
            row = self._db.execute("SELECT status FROM targets WHERE post_id=?", (post_id,)).fetchone()
            if not row or row["status"] not in ("deleted", "unknown"):
                raise ValueError("readback is limited to acknowledged or unknown DELETEs")
            if row["status"] == "deleted" and readback.get("absent") is True:
                verification = "verified_absent_after_ack"
            elif readback.get("ok") is True:
                verification = "still_visible"
            else:
                verification = "unconfirmed_after_ack" if row["status"] == "deleted" else "unknown_unchanged"
            self._db.execute(
                "UPDATE targets SET readback_json=?,verification=?,updated_at=? WHERE post_id=?",
                (_json(_safe(readback)), verification, utc_now(), post_id),
            )
            self._event(post_id, "readback", {"verification": verification, "readback": readback})
        return verification

    def summary(self):
        rows = self.rows()
        counts = {status: 0 for status in STATUSES}
        per_group = {str(group): {status: 0 for status in STATUSES} for group in sorted(ALLOWED_GROUPS)}
        for row in rows:
            counts[row["status"]] += 1
            for group in row["target"].get("groups", []):
                if str(group) in per_group:
                    per_group[str(group)][row["status"]] += 1
        with self._lock:
            intentions = self._db.execute("SELECT COUNT(*) FROM events WHERE event='delete_intent'").fetchone()[0]
            not_sent = self._db.execute("SELECT COUNT(*) FROM events WHERE event='delete_not_sent'").fetchone()[0]
        return {
            "total": len(rows), "actual_requests": intentions - not_sent, "intent_count": intentions,
            "not_sent_after_intent": not_sent, "counts": counts,
            "verified_absent_after_ack": sum(row["verification"] == "verified_absent_after_ack" for row in rows),
            "canary_attempted": sum(row["is_canary"] for row in rows),
            "canary_verified": sum(row["is_canary"] and row["status"] == "deleted" and
                                   row["verification"] == "verified_absent_after_ack" for row in rows),
            "per_group": per_group,
        }


def _inspection_problem(target, inspection, manifest):
    if inspection.get("ok") is not True:
        return "post_not_readable_before_delete"
    if str(inspection.get("id")) != str(target["post_id"]):
        return "post_identity_mismatch"
    if str(inspection.get("page_id")) != str(target["page_id"]):
        return "post_page_identity_mismatch"
    try:
        published = parse_timestamp(inspection.get("created_at"))
    except (TypeError, ValueError):
        return "actual_publish_time_unconfirmed"
    if not (parse_timestamp(manifest["window_start"]) <= published <= parse_timestamp(manifest["window_end"])):
        return "actual_publish_time_outside_frozen_window"
    return None


def _process_target(target, manifest, ledger, adapter, is_canary=False, stop_event=None,
                    reserve_attempt=None, finish_reservation=None):
    """Own the bound client's lifetime, including all preflight early returns."""
    client = None
    try:
        try:
            client = adapter.for_target(target)
        except Exception as exc:
            ledger.skip(target["post_id"], "credential_adapter_exception", {"type": type(exc).__name__})
            return ledger.get(target["post_id"])
        return _process_bound_target(target, manifest, ledger, client, is_canary, stop_event,
                                     reserve_attempt, finish_reservation)
    finally:
        if client is not None and hasattr(client, "close"):
            client.close()


def _process_bound_target(target, manifest, ledger, client, is_canary, stop_event,
                          reserve_attempt, finish_reservation):
    post_id = target["post_id"]
    row = ledger.get(post_id)
    if row["status"] != "pending" or (stop_event and stop_event.is_set()):
        return row
    try:
        precheck = client.precheck()
        if precheck.get("ok") is not True:
            ledger.skip(post_id, precheck.get("reason", "current_source_precheck_failed"), precheck)
            return ledger.get(post_id)
        if not precheck.get("credential_id"):
            ledger.skip(post_id, "credential_identity_missing", precheck)
            return ledger.get(post_id)
        inspection = client.inspect(post_id)
    except (sqlite3.Error, KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        ledger.skip(post_id, "predelete_adapter_exception", {"type": type(exc).__name__, "message": _safe(str(exc))})
        return ledger.get(post_id)
    reason = _inspection_problem(target, inspection, manifest)
    if reason:
        ledger.skip(post_id, reason, inspection)
        return ledger.get(post_id)
    if stop_event and stop_event.is_set():
        return ledger.get(post_id)
    if reserve_attempt is not None and not reserve_attempt():
        return ledger.get(post_id)
    if not ledger.claim(post_id, precheck["credential_id"], precheck, inspection, is_canary=is_canary):
        if finish_reservation is not None:
            finish_reservation(False)
        return ledger.get(post_id)
    try:
        result = client.delete(post_id)
    except (KeyboardInterrupt, SystemExit):
        raise  # Durable deleting intention becomes unknown during recovery.
    except Exception as exc:
        result = {"kind": "unknown", "reason": "delete_adapter_exception",
                  "error": {"type": type(exc).__name__, "message": _safe(str(exc))}}
    if not isinstance(result, dict) or result.get("kind") not in ("success", "failure", "unknown"):
        result = {"kind": "unknown", "reason": "unrecognized_delete_response", "response": _safe(result)}
    status = {"success": "deleted", "failure": "failed", "unknown": "unknown"}[result["kind"]]
    if result["kind"] == "failure" and result.get("response", {}).get("sent") is False:
        status = "skipped"
    ledger.finish(post_id, status, result)
    if finish_reservation is not None:
        finish_reservation(status != "skipped")
    if status == "deleted":
        try:
            readback = client.inspect(post_id)
        except Exception as exc:
            readback = {"ok": False, "error": {"type": type(exc).__name__, "message": _safe(str(exc))}}
        ledger.record_readback(post_id, readback)
    return ledger.get(post_id)


def execute(manifest, ledger, adapter, stage="all", workers=2, canary_size=5, limit=None):
    """Execute pending targets only; canary is serial, remainder serial per Page.

    `limit` caps new DELETE attempts, not preflight reads. Callers hold an exclusive
    process lock. A resumed unknown/failed/skipped target is never retried here.
    """
    if stage not in ("all", "canary", "remaining"):
        raise ValueError("stage must be all, canary, or remaining")
    if workers not in (1, 2) or canary_size < 1 or (limit is not None and limit < 1):
        raise ValueError("workers must be 1 or 2; canary_size and limit must be positive")
    ledger.import_manifest(manifest)
    ledger.recover_inflight()
    target_map = {str(target["post_id"]): target for target in manifest["targets"]}
    initial_attempts = ledger.summary()["actual_requests"]
    if stage in ("all", "canary"):
        for row in ledger.rows():
            summary = ledger.summary()
            canaries = [item for item in ledger.rows() if item["is_canary"]]
            if any(item["status"] != "deleted" or item["verification"] != "verified_absent_after_ack"
                   for item in canaries):
                return dict(summary, halted="canary_failed_unknown_or_unverified")
            if summary["canary_verified"] >= canary_size:
                break
            if limit is not None and summary["actual_requests"] - initial_attempts >= limit:
                return dict(summary, halted="request_limit_reached")
            if row["status"] != "pending":
                continue
            result = _process_target(target_map[row["post_id"]], manifest, ledger, adapter, is_canary=True)
            if result["is_canary"] and (result["status"] != "deleted" or
                                        result["verification"] != "verified_absent_after_ack"):
                return dict(ledger.summary(), halted="canary_failed_unknown_or_unverified")
        if stage == "canary":
            summary = ledger.summary()
            return dict(summary, halted=None if summary["canary_verified"] >= canary_size else "insufficient_verified_canaries")
    summary = ledger.summary()
    if summary["canary_verified"] < canary_size or summary["canary_attempted"] != summary["canary_verified"]:
        return dict(summary, halted="canary_gate_not_satisfied")
    remaining_budget = None if limit is None else limit - (summary["actual_requests"] - initial_attempts)
    if remaining_budget is not None and remaining_budget <= 0:
        return dict(summary, halted="request_limit_reached")
    by_page = {}
    for row in ledger.rows():
        if row["status"] == "pending":
            by_page.setdefault(row["page_id"], []).append(target_map[row["post_id"]])
    stop_event, budget_condition = threading.Event(), threading.Condition()
    budget = [remaining_budget]
    reservations = [0]

    def reserve_attempt():
        with budget_condition:
            if stop_event.is_set():
                return False
            if budget[0] is None:
                return True
            while budget[0] <= 0 and reservations[0] and not stop_event.is_set():
                budget_condition.wait(timeout=0.2)
            if budget[0] <= 0 or stop_event.is_set():
                return False
            budget[0] -= 1
            reservations[0] += 1
            return True

    def finish_reservation(sent):
        with budget_condition:
            if budget[0] is not None:
                reservations[0] -= 1
                if not sent:
                    budget[0] += 1
                budget_condition.notify_all()

    def page_worker(targets):
        for target in targets:
            if stop_event.is_set():
                break
            try:
                result = _process_target(target, manifest, ledger, adapter, stop_event=stop_event,
                                         reserve_attempt=reserve_attempt, finish_reservation=finish_reservation)
                if result["status"] == "pending":
                    break  # The real DELETE-attempt budget has been exhausted.
            except BaseException:
                stop_event.set()
                with budget_condition:
                    budget_condition.notify_all()
                raise

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(page_worker, targets) for targets in by_page.values()]
        for future in concurrent.futures.as_completed(futures):
            future.result()
    summary = ledger.summary()
    halted = "request_limit_reached" if limit is not None and summary["counts"]["pending"] else None
    return dict(summary, halted=halted)


def reconcile(manifest, ledger, adapter):
    """GET-only reconciliation. An ambiguous DELETE never becomes a success."""
    ledger.import_manifest(manifest)
    ledger.recover_inflight()
    for row in ledger.rows():
        if row["status"] not in ("deleted", "unknown"):
            continue
        client = None
        try:
            client = adapter.for_target(row["target"])
            precheck = getattr(client, "reconcile_precheck", client.precheck)()
            if precheck.get("ok") is not True:
                readback = {"ok": False, "error": "reconcile_precheck_failed", "context": precheck}
            else:
                readback = client.inspect(row["post_id"])
        except Exception as exc:
            readback = {"ok": False, "error": {"type": type(exc).__name__, "message": _safe(str(exc))}}
        finally:
            if client is not None and hasattr(client, "close"):
                client.close()
        ledger.record_readback(row["post_id"], readback)
    return ledger.summary()
