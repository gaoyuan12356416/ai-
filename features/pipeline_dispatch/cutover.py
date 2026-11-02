"""Explicit, frozen per-item admission changes. Never sends platform requests."""
import contextlib
import hashlib
import json
import secrets

from .adapters import TERMINAL
from .core import DispatchError, channel_key, dumps, stamp


class Cutover:
    def __init__(self, coordinator):
        self.coordinator = coordinator
        self.store, self.registry = coordinator.store, coordinator.registry
        with self.store.transaction() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS pipeline_cutover_plan(id TEXT PRIMARY KEY,channel TEXT NOT NULL,cutoff TEXT NOT NULL,sha256 TEXT NOT NULL,manifest_json TEXT NOT NULL,status TEXT NOT NULL,updated_at TEXT NOT NULL)")

    def snapshot(self, channel):
        """Bounded pages over nonterminal native rows, independent of UI cursors."""
        for adapter in self.registry.adapters.values():
            spec = adapter.spec
            if spec.channel != channel:
                continue
            with adapter.connect() as conn:
                if not adapter._exists(conn, spec.table):
                    raise DispatchError("cutover_source_unavailable")
                after = None
                while True:
                    rows = conn.execute("SELECT * FROM " + spec.table + " WHERE " + spec.state + " NOT IN (" + ",".join("?" for _ in TERMINAL) + ") AND (? IS NULL OR " + spec.identity + ">?) ORDER BY " + spec.identity + " LIMIT 300", (*sorted(TERMINAL), after, after)).fetchall()
                    if not rows:
                        break
                    for row in rows:
                        item = adapter.project(adapter.enrich(conn, row))
                        if item:
                            yield item
                    after = rows[-1][spec.identity]

    def plan(self, channel, cutoff=None, grandfathered=()):
        channel, cutoff = channel_key(channel), stamp(cutoff or self.store.now())
        if channel in self.store.config()["enabled_channels"]:
            raise DispatchError("cutover_channel_must_stop_claims")
        offers = list(self.snapshot(channel))
        if self.coordinator.owner_resolver:
            offers = self.coordinator.owner_resolver.resolve(offers)
        exempt = set(map(str, grandfathered))
        for item in offers:
            if item.get("legacy_inflight") and item.get("started_at") and stamp(item["started_at"]) <= cutoff:
                exempt.add(item["source_key"])
            self.store.offer(item)
        # Descendants must be exact frozen source links, never a batch-wide list.
        while True:
            found = {item["source_key"] for item in offers if exempt.intersection(item.get("ancestor_source_keys", []))} - exempt
            if not found:
                break
            exempt.update(found)
        paused = [item for item in offers if item["source_key"] not in exempt and item.get("pause_eligible") and not item.get("reconcile_only") and stamp(item.get("scheduled_at") or cutoff) <= cutoff and stamp(item.get("source_created_at") or cutoff) <= cutoff]
        unknown = [item["source_key"] for item in offers if item.get("reconcile_only")]
        manifest = {"channel": channel, "cutoff": cutoff, "paused": paused, "grandfathered_source_keys": sorted(exempt), "reconcile_source_keys": sorted(set(unknown))}
        encoded = dumps(manifest)
        identity, fingerprint = secrets.token_hex(12), hashlib.sha256(encoded.encode()).hexdigest()
        with self.store.transaction() as conn:
            conn.execute("INSERT INTO pipeline_cutover_plan VALUES(?,?,?,?,?,'planned',?)", (identity, channel, cutoff, fingerprint, encoded, self.store.now()))
        return {"plan_id": identity, "sha256": fingerprint, "channel": channel, "cutoff": cutoff, "paused_source_keys": [item["source_key"] for item in paused], "grandfathered_source_keys": sorted(exempt), "reconcile_source_keys": sorted(set(unknown))}

    def load(self, identity, expected_sha):
        with contextlib.closing(self.store.connect()) as conn:
            row = conn.execute("SELECT * FROM pipeline_cutover_plan WHERE id=?", (identity,)).fetchone()
        if not row or row["sha256"] != expected_sha:
            raise DispatchError("cutover_plan_identity_changed")
        return dict(row), json.loads(row["manifest_json"])

    def apply(self, identity, expected_sha):
        saved, manifest = self.load(identity, expected_sha)
        if manifest["channel"] in self.store.config()["enabled_channels"]:
            raise DispatchError("cutover_channel_must_stop_claims")
        self.store.cutover(manifest["channel"], manifest["cutoff"], manifest["grandfathered_source_keys"])
        errors = []
        for job in manifest["paused"]:
            try:
                driver = self.registry._adapter(job).driver
                if driver is None or not hasattr(driver, "pause"):
                    raise DispatchError("canonical_pause_unavailable")
                result = driver.pause(job, manifest["cutoff"])
                if not result.get("held"):
                    raise DispatchError("canonical_pause_not_confirmed")
            except Exception as exc:
                errors.append({"source_key": job["source_key"], "code": getattr(exc, "code", "canonical_pause_failed")})
        state = "partial" if errors else "held"
        with self.store.transaction() as conn:
            conn.execute("UPDATE pipeline_cutover_plan SET status=?,updated_at=? WHERE id=?", (state, self.store.now(), identity))
        return {"plan_id": identity, "status": state, "paused_count": len(manifest["paused"]) - len(errors), "errors": errors}

    def enable(self, identity, expected_sha):
        saved, manifest = self.load(identity, expected_sha)
        if saved["status"] != "held":
            raise DispatchError("canonical_pauses_incomplete")
        self.coordinator.scan()
        # Incremental UI collection may not have reached a late/native claim.
        # Re-read every nonterminal native row before opening admission. Source
        # failures propagate; a stale projection cannot authorize a cutover.
        offers = list(self.snapshot(manifest["channel"]))
        if self.coordinator.owner_resolver:
            offers = self.coordinator.owner_resolver.resolve(offers)
        for item in offers:
            self.store.offer(item)
        if any(item.get("legacy_inflight") for item in offers):
            raise DispatchError("legacy_items_still_running")
        exempt = set(manifest["grandfathered_source_keys"])
        while True:
            descendants = {item["source_key"] for item in offers if exempt.intersection(item.get("ancestor_source_keys", []))} - exempt
            if not descendants:
                break
            exempt.update(descendants)
        for item in offers:
            if (item["source_key"] not in exempt and item.get("pause_eligible")
                    and not item.get("reconcile_only")
                    and stamp(item.get("scheduled_at") or manifest["cutoff"]) <= manifest["cutoff"]
                    and stamp(item.get("source_created_at") or manifest["cutoff"]) <= manifest["cutoff"]
                    and item.get("hold_reason") != "cutover_overdue"):
                raise DispatchError("cutover_unplanned_backlog")
        with contextlib.closing(self.store.connect()) as conn:
            active = conn.execute("SELECT COUNT(*) FROM pipeline_job WHERE channel=? AND status='legacy_running'", (manifest["channel"],)).fetchone()[0]
        if active:
            raise DispatchError("legacy_items_still_running")
        self.store.channel_enabled(manifest["channel"], True)
        return {"accepted": True, "channel": manifest["channel"], "enabled": True}
