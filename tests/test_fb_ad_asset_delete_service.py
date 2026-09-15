"""End-to-end orchestration tests with a real temporary SQLite ledger.

Both business-source and Graph adapters are in-memory fakes. The task worker is
queued explicitly, so no threads, remote calls or actual deletions are started.
"""
from copy import deepcopy
from itertools import combinations
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from features.fb_ad_asset_delete.core import AssetError, PHASES, actor_key
from features.fb_ad_asset_delete.graph import GraphError
from features.fb_ad_asset_delete.service import Service
from features.fb_ad_asset_delete.store import Store, StoreError


SESSION = dict(tenant_key="tenant", user_id="operator", role="user", name="Test Operator")
PRODUCT = dict(id="1", name="Catalog", parent_id="1", parent_name="Catalog", kind="App", default_user="803")
DRAMA = dict(product_id="1", product_name="Catalog", parent_id="1", content_id="100", series_code="SERIES-A", language="en", name="Example")


def ad(index=1, creative=None, videos=None):
    return dict(ad_id=str(100 + index), creative_id=str(200 + index) if creative is None else creative,
                video_ids=[str(300 + index)] if videos is None else videos,
                product_id="1", account_id="444", user_id="803", content_ids=["100"],
                dramas=[deepcopy(DRAMA)], reason="")


class FakeSource:
    def __init__(self):
        self.allowed = True
        self.catalog = [deepcopy(PRODUCT)]
        self.dramas = [deepcopy(DRAMA)]
        self.ads = [ad()]
        self.references = []
        self.reference_error = None
        self.resolve_error = None
        self.snapshot_error = None
        self.fresh_checks = []

    def list_products(self, session):
        return deepcopy(self.catalog) if self.allowed else []

    def selected_products(self, session, ids):
        allowed = {p["id"]: p for p in self.list_products(session)}
        if any(pid not in allowed for pid in ids):
            raise AssetError("product_permission_denied", "Product access was revoked", 403)
        return [allowed[pid] for pid in ids]

    def resolve_dramas(self, input_type, ids, products):
        return deepcopy(self.dramas), []

    def resolve_ads(self, dramas, products):
        if self.resolve_error:
            raise self.resolve_error
        return deepcopy(self.ads), []

    def shared_references(self, objects, progress=None, fresh=False):
        self.fresh_checks.append(fresh)
        if self.reference_error:
            raise self.reference_error
        keys = {o["key"] for o in objects}
        return deepcopy([row for row in self.references if row["key"] in keys])

    def validate_reference_snapshot(self, refs):
        if self.snapshot_error:
            raise self.snapshot_error


class GraphState:
    def __init__(self):
        self.events = []
        self.ad_creatives = {"101": "201", "102": "202"}
        self.creative_videos = {"201": ["301"], "202": ["302"]}
        self.inspect_errors = {}
        self.delete_outcomes = {}
        self.reconcile_outcomes = {}
        self.after_delete = lambda obj: None

    def deleted_keys(self):
        return [key for method, key in self.events if method == "DELETE"]


class FakeGraph:
    def __init__(self, state):
        self.state = state

    def inspect(self, obj, allowed_ads=None, deleted_creatives=None, check_references=True):
        self.state.events.append(("GET", obj["key"]))
        if obj["key"] in self.state.inspect_errors:
            raise self.state.inspect_errors[obj["key"]]
        proof = dict(id=obj["object_id"], account_ids=obj["account_ids"], checked_at="2026-09-15T00:00:00Z")
        if obj["kind"] == "ad":
            proof["creative_id"] = self.state.ad_creatives.get(obj["object_id"], "")
        elif obj["kind"] == "creative":
            proof["video_ids"] = self.state.creative_videos.get(obj["object_id"], [])
        return "pending", proof

    def delete(self, obj):
        self.state.events.append(("DELETE", obj["key"]))
        self.state.after_delete(obj)
        result = self.state.delete_outcomes.get(obj["key"], ("deleted", {"success": True}))
        if isinstance(result, BaseException):
            raise result
        return deepcopy(result)

    def reconcile(self, obj):
        self.state.events.append(("GET_RECONCILE", obj["key"]))
        return deepcopy(self.state.reconcile_outcomes.get(obj["key"], ("unknown", {"code": "read_unavailable", "checked_at": "2026-09-15T12:00:00Z"})))


class QueuedWorkers:
    def __init__(self):
        self.pending = []

    def __call__(self, function, *args):
        self.pending.append((function, args))

    def run_next(self):
        function, args = self.pending.pop(0)
        function(*args)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.new_environment()

    def new_environment(self):
        self.source = FakeSource()
        self.graph = GraphState()
        self.workers = QueuedWorkers()
        self.store = Store(Path(self.temp.name) / (uuid.uuid4().hex + ".sqlite3"))
        self.module_allowed = True

        def authorize(session):
            if not self.module_allowed:
                raise AssetError("module_forbidden", "Module access was revoked", 403)
            return deepcopy(session)

        self.service = Service(self.store, self.source, lambda: FakeGraph(self.graph),
                               spawn=self.workers, authorize=authorize)

    def preview(self):
        response = self.service.preview(SESSION, {"input_type": "content_id", "ids": ["100"], "product_ids": ["1"]})
        self.assertEqual(response["status"], "previewing")
        self.workers.run_next()
        job = self.store.get_job(response["job_id"])
        self.graph.events.clear()
        return job

    def execute(self, job, phases=PHASES, request_id=None, run=True, extra=None):
        payload = dict(preview_id=job["preview_id"], phases=list(phases), request_id=request_id or uuid.uuid4().hex)
        payload.update(extra or {})
        result = self.service.execute(SESSION, job["job_id"], payload)
        if run and not result["duplicate"]:
            self.workers.run_next()
        return result

    def statuses(self, job):
        return {o["key"]: o["status"] for o in self.store.get_job(job["job_id"])["objects"]}

    def test_all_seven_phase_combinations_execute_in_fixed_order(self):
        for size in (1, 2, 3):
            for phases in combinations(PHASES, size):
                with self.subTest(phases=phases):
                    self.new_environment()
                    job = self.preview()
                    self.assertEqual(job["status"], "ready")
                    self.execute(job, tuple(reversed(phases)))
                    keys = {"creative": "creative:201", "ad": "ad:101", "video": "video:301"}
                    self.assertEqual(self.graph.deleted_keys(), [keys[p] for p in PHASES if p in phases])
                    for phase, key in keys.items():
                        self.assertEqual(self.statuses(job)[key], "deleted" if phase in phases else "pending")
                    self.assertEqual(self.store.get_job(job["job_id"])["runs"][0]["phases"], list(phases))

    def test_single_failure_continues_later_phases_and_retry_preserves_success(self):
        job = self.preview()
        self.graph.delete_outcomes["creative:201"] = ("failed", {"code": "100", "message": "Explicit rejection"})
        self.execute(job)
        self.assertEqual(self.graph.deleted_keys(), ["creative:201", "ad:101", "video:301"])
        self.assertEqual(self.statuses(job), {"creative:201": "failed", "ad:101": "deleted", "video:301": "deleted"})
        self.assertEqual(self.store.get_job(job["job_id"])["status"], "partial")
        self.graph.delete_outcomes.clear()
        self.graph.events.clear()
        self.execute(job)
        self.assertEqual(self.graph.deleted_keys(), ["creative:201"])
        self.assertTrue(all(status == "deleted" for status in self.statuses(job).values()))

    def test_same_request_is_idempotent_before_and_after_worker_completion(self):
        job = self.preview()
        request_id = "request-with-stable-id-1234"
        first = self.execute(job, request_id=request_id, run=False)
        second = self.execute(job, ("video", "creative", "ad"), request_id=request_id, run=False)
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["run_id"], second["run_id"])
        self.assertEqual(len(self.workers.pending), 1)
        self.workers.run_next()
        self.assertTrue(self.execute(job, request_id=request_id)["duplicate"])
        self.assertEqual(self.graph.deleted_keys(), ["creative:201", "ad:101", "video:301"])
        self.assertEqual(self.workers.pending, [])
        with self.assertRaises(StoreError) as error:
            self.execute(job, ("ad",), request_id=request_id)
        self.assertEqual(error.exception.code, "request_conflict")

    def test_unknown_is_never_retried_and_reconciliation_only_reads(self):
        job = self.preview()
        self.graph.delete_outcomes["creative:201"] = ("unknown", {"code": "network_uncertain"})
        self.execute(job, ("creative",))
        self.assertEqual(self.statuses(job)["creative:201"], "unknown")
        self.graph.events.clear()
        self.execute(job, ("creative",))
        self.assertEqual(self.graph.deleted_keys(), [])
        result = self.service.reconcile(SESSION, job["job_id"], {"preview_id": job["preview_id"]})
        self.assertEqual((result["read_only"], result["checked"]), (True, 1))
        self.assertEqual(self.graph.events, [("GET_RECONCILE", "creative:201")])
        self.assertEqual(self.statuses(job)["creative:201"], "unknown")
        self.graph.reconcile_outcomes["creative:201"] = ("already_deleted", {"confirmed_deleted": True, "proof": {"id": "201", "status": "DELETED"}})
        self.service.reconcile(SESSION, job["job_id"], {"preview_id": job["preview_id"]})
        self.assertEqual(self.statuses(job)["creative:201"], "already_deleted")
        self.execute(job, ("creative",))
        self.assertEqual(self.graph.deleted_keys(), [])

    def test_product_access_revocation_hides_task_and_prevents_execution(self):
        job = self.preview()
        self.source.allowed = False
        self.assertEqual(self.service.list_jobs(SESSION)["items"], [])
        for action in (lambda: self.execute(job), lambda: self.service.detail(SESSION, job["job_id"]),
                       lambda: self.service.reconcile(SESSION, job["job_id"], {"preview_id": job["preview_id"]})):
            with self.assertRaises(AssetError) as error:
                action()
            self.assertEqual(error.exception.status, 403)
        self.assertEqual(self.workers.pending, [])
        self.assertEqual(self.graph.events, [])

    def test_parent_product_mapping_change_requires_a_new_preview(self):
        job = self.preview()
        self.source.catalog[0]["parent_id"] = "2"
        with self.assertRaises(AssetError) as error:
            self.execute(job)
        self.assertEqual((error.exception.code, error.exception.status), ("product_mapping_changed", 409))
        self.assertEqual(self.graph.events, [])

    def test_module_revocation_before_worker_starts_stops_all_writes(self):
        job = self.preview()
        self.execute(job, run=False)
        self.module_allowed = False
        self.workers.run_next()
        self.assertEqual(self.graph.events, [])
        self.assertEqual(self.store.get_job(job["job_id"])["status"], "interrupted")
        self.assertTrue(all(status == "pending" for status in self.statuses(job).values()))

    def test_permission_revocation_between_objects_stops_same_phase(self):
        for permission in ("module", "product"):
            with self.subTest(permission=permission):
                self.new_environment()
                self.source.ads = [ad(), ad(2)]
                job = self.preview()

                def revoke(_obj):
                    if permission == "module":
                        self.module_allowed = False
                    else:
                        self.source.allowed = False

                self.graph.after_delete = revoke
                self.execute(job, ("ad",))
                self.assertEqual(self.graph.deleted_keys(), ["ad:101"])
                self.assertEqual(self.statuses(job)["ad:102"], "pending")
                self.assertEqual(self.store.get_job(job["job_id"])["status"], "interrupted")

    def test_frozen_preview_rejects_added_object_ids_and_stale_preview_id(self):
        job = self.preview()
        for field in ("objects", "object_ids", "ad_ids", "creative_ids", "video_ids"):
            with self.subTest(field=field), self.assertRaises(AssetError) as error:
                self.execute(job, extra={field: ["999"]})
            self.assertEqual(error.exception.code, "frozen_preview_only")
        with self.assertRaises(StoreError) as error:
            self.execute(job, extra={"preview_id": "stale-preview"})
        self.assertEqual(error.exception.code, "preview_mismatch")
        self.assertEqual(self.workers.pending, [])
        self.assertEqual(self.graph.events, [])
        self.assertEqual(len(self.store.get_job(job["job_id"])["objects"]), 3)

    def test_ledger_finish_failure_stops_further_deletes_and_fences_the_active_one(self):
        job = self.preview()
        self.execute(job, run=False)
        with patch.object(self.store, "finish_object", side_effect=StoreError("simulated persistence failure", "ledger_error")):
            self.workers.run_next()
        self.assertEqual(self.graph.deleted_keys(), ["creative:201"])
        self.assertEqual(self.statuses(job), {"creative:201": "unknown", "ad:101": "pending", "video:301": "pending"})
        self.assertEqual(self.store.get_job(job["job_id"])["status"], "interrupted")

    def test_unverified_ad_blocks_its_linked_creative_and_video(self):
        self.graph.inspect_errors["ad:101"] = GraphError("creative_changed", "Current creative differs")
        job = self.preview()
        self.assertEqual(job["status"], "ready")
        self.assertTrue(all(status == "blocked" for status in self.statuses(job).values()))
        self.execute(job)
        self.assertEqual(self.graph.deleted_keys(), [])

    def test_source_ownership_conflict_cannot_be_removed_by_live_graph_success(self):
        self.source.ads[0]["reason"] = "Conflicting source product"
        job = self.preview()
        self.assertTrue(all(status == "blocked" for status in self.statuses(job).values()))
        self.execute(job)
        self.assertEqual(self.graph.deleted_keys(), [])

    def test_video_requires_live_creative_proof_and_new_live_video_is_frozen(self):
        self.graph.creative_videos["201"] = ["302"]
        job = self.preview()
        self.assertEqual(self.statuses(job)["video:301"], "blocked")
        self.assertEqual(self.statuses(job)["video:302"], "pending")
        self.execute(job, ("video",))
        self.assertEqual(self.graph.deleted_keys(), ["video:302"])

    def test_missing_source_creative_is_discovered_from_verified_ad(self):
        self.source.ads = [ad(creative="", videos=[])]
        job = self.preview()
        self.assertEqual(set(self.statuses(job)), {"ad:101", "creative:201", "video:301"})
        self.assertTrue(all(status == "pending" for status in self.statuses(job).values()))

    def test_shared_creative_and_video_are_deduplicated_across_ads(self):
        self.source.ads = [ad(), ad(2, creative="201", videos=["301"])]
        self.graph.ad_creatives["102"] = "201"
        job = self.preview()
        self.assertEqual(len(job["objects"]), 4)
        self.execute(job)
        self.assertEqual(self.graph.deleted_keys(), ["creative:201", "ad:101", "ad:102", "video:301"])

    def test_outside_shared_reference_blocks_assets_while_independent_ad_is_eligible(self):
        self.source.references = [dict(key="creative:201", ad_id="999"), dict(key="video:301", ad_id="999")]
        job = self.preview()
        self.assertEqual(self.statuses(job), {"creative:201": "blocked", "ad:101": "pending", "video:301": "blocked"})
        self.execute(job)
        self.assertEqual(self.graph.deleted_keys(), ["ad:101"])

    def test_incomplete_reference_query_blocks_assets(self):
        self.source.reference_error = AssetError("reference_check_incomplete", "Reference query could not complete")
        job = self.preview()
        self.assertEqual(self.statuses(job), {"creative:201": "blocked", "ad:101": "pending", "video:301": "blocked"})
        self.execute(job)
        self.assertEqual(self.graph.deleted_keys(), ["ad:101"])

    def test_new_shared_reference_is_checked_again_immediately_before_execution(self):
        job = self.preview()
        self.source.references = [dict(key="creative:201", ad_id="999")]
        self.execute(job, ("creative",))
        self.assertEqual(self.graph.deleted_keys(), [])
        self.assertEqual(self.statuses(job)["creative:201"], "blocked")

    def test_source_unavailable_fails_preview_and_never_becomes_zero_hit_ready(self):
        self.source.resolve_error = AssetError("source_unavailable", "Database unavailable", 503)
        job = self.preview()
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["error"]["code"], "source_unavailable")
        with self.assertRaises(StoreError):
            self.execute(job)
        self.assertEqual(self.graph.events, [])

    def test_zero_hit_preview_is_explicit_and_has_no_delete_side_effect(self):
        self.source.ads = []
        job = self.preview()
        self.assertEqual((job["status"], job["objects"]), ("ready", []))
        self.execute(job)
        self.assertEqual(self.graph.events, [])
        self.assertEqual(self.store.get_job(job["job_id"])["status"], "completed")

    def test_detail_paginates_and_hides_worker_and_credential_metadata(self):
        job = self.preview()
        detail = self.service.detail(SESSION, job["job_id"], page=2, page_size=1)
        self.assertEqual((detail["total"], len(detail["objects"]), detail["summary"]["total"]), (3, 1, 3))
        for field in ("actor", "preview_owner", "user_ids"):
            self.assertNotIn(field, detail)
        self.assertNotIn("user_ids", detail["objects"][0])
        other = dict(SESSION, user_id="another-operator")
        with self.assertRaises(AssetError) as error:
            self.service.detail(other, job["job_id"])
        self.assertEqual(error.exception.status, 403)

    def test_running_task_cannot_reconcile_or_start_a_second_distinct_run(self):
        job = self.preview()
        self.execute(job, run=False)
        with self.assertRaises(AssetError) as error:
            self.service.reconcile(SESSION, job["job_id"], {"preview_id": job["preview_id"]})
        self.assertEqual(error.exception.code, "job_running")
        with self.assertRaises(StoreError):
            self.execute(job, run=False)
        self.assertEqual(self.graph.events, [])
        self.assertEqual(len(self.workers.pending), 1)

    def test_bounded_reconciliation_eventually_checks_each_unknown_object(self):
        objects = [dict(key="creative:" + str(n), kind="creative", object_id=str(n), status="unknown",
                        product_ids=["1"], content_ids=["100"], account_ids=["444"], user_ids=["803"])
                   for n in (201, 202, 203)]
        job = self.store.create_job(dict(job_id="unknown-batch", preview_id="unknown-preview", actor=actor_key(SESSION),
                                        products=[deepcopy(PRODUCT)], ids=["100"], input_type="content_id", status="partial", objects=objects))
        for _ in objects:
            result = self.service.reconcile(SESSION, job["job_id"], {"preview_id": job["preview_id"]})
            self.assertGreaterEqual(result["checked"], 1)
            self.assertLessEqual(result["checked"], 10)
            self.assertTrue(result["read_only"])
        self.assertEqual({key for method, key in self.graph.events if method == "GET_RECONCILE"},
                         {"creative:201", "creative:202", "creative:203"})
        self.assertEqual(self.graph.deleted_keys(), [])
        self.assertTrue(all(status == "unknown" for status in self.statuses(job).values()))

    def begin_recheck(self, job, request_id="recheck-request-123456", run=True):
        result = self.service.recheck(SESSION, job["job_id"], {"preview_id": job["preview_id"], "request_id": request_id})
        if run and not result["duplicate"]:
            self.workers.run_next()
        return result

    def test_recheck_preserves_frozen_successes_and_is_get_only(self):
        self.source.reference_error = AssetError("reference_check_incomplete", "timeout")
        job = self.preview()
        self.source.reference_error = None
        self.execute(job, ("ad",))
        self.graph.events.clear()
        with self.store._transaction(False) as conn:
            attempts = conn.execute("SELECT COUNT(*) FROM fb_asset_delete_v2_attempts").fetchone()[0]
        result = self.begin_recheck(job)
        self.assertTrue(result["read_only"])
        self.assertEqual(self.statuses(job), {"creative:201": "pending", "ad:101": "deleted", "video:301": "pending"})
        self.assertEqual(self.graph.deleted_keys(), [])
        fresh = self.store.get_job(job["job_id"])
        self.assertEqual(fresh["preview_id"], job["preview_id"])
        self.assertEqual(fresh["recheck"]["status"], "completed")
        with self.store._transaction(False) as conn:
            self.assertEqual(attempts, conn.execute("SELECT COUNT(*) FROM fb_asset_delete_v2_attempts").fetchone()[0])

    def test_recheck_is_idempotent_and_excludes_concurrent_execution(self):
        self.source.reference_error = AssetError("reference_check_incomplete", "timeout")
        job = self.preview()
        first = self.begin_recheck(job, run=False)
        second = self.begin_recheck(job, run=False)
        self.assertEqual(first["operation_id"], second["operation_id"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(len(self.workers.pending), 1)
        with self.assertRaises(StoreError):
            self.execute(job, run=False)
        with self.assertRaises(StoreError):
            self.begin_recheck(job, request_id="another-recheck-request", run=False)
        self.workers.run_next()
        self.assertTrue(self.begin_recheck(job)["duplicate"])

    def test_recheck_cannot_change_ids_or_clear_permanent_ownership_conflicts(self):
        self.source.ads[0]["reason"] = "Source ownership conflict"
        job = self.preview()
        with self.assertRaises(AssetError):
            self.service.recheck(SESSION, job["job_id"], {"preview_id": job["preview_id"], "request_id": "recheck-request-123456", "video_ids": ["999"]})
        self.begin_recheck(job)
        self.assertTrue(all(s == "blocked" for s in self.statuses(job).values()))
        self.assertEqual(self.graph.events, [])

    def test_recheck_checks_current_permissions_and_keeps_source_failures_blocked(self):
        self.source.reference_error = AssetError("video_index_incomplete", "stream failed")
        job = self.preview()
        self.begin_recheck(job, run=False)
        self.module_allowed = False
        self.workers.run_next()
        self.assertEqual(self.store.get_job(job["job_id"])["recheck"]["status"], "interrupted")
        self.assertEqual(self.statuses(job)["video:301"], "blocked")
        self.assertEqual(self.graph.deleted_keys(), [])

    def test_expired_video_snapshot_is_checked_again_after_graph_read(self):
        job = self.preview()
        self.source.snapshot_error = AssetError("video_index_expired", "expired")
        self.execute(job, ("video",))
        self.assertEqual(self.graph.deleted_keys(), [])
        self.assertEqual(self.statuses(job)["video:301"], "blocked")
        self.assertTrue(self.source.fresh_checks[-1])


if __name__ == "__main__":
    unittest.main()
