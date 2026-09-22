"""Real threads + SQLite, fake Graph: verify budgets, stage barriers and aborts."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import time
import unittest
import uuid
import requests

from features.fb_ad_asset_delete.core import actor_key
from features.fb_ad_asset_delete.execution import ExecutionBudget
from features.fb_ad_asset_delete.service import Service
from features.fb_ad_asset_delete.graph import GraphClient
from features.fb_ad_asset_delete.store import Store, StoreError
from test_fb_ad_asset_delete_service import FakeSource, SESSION, PRODUCT


class Probe:
    def __init__(self, target=1):
        self.lock = threading.Lock()
        self.active = self.peak = 0
        self.accounts, self.account_peaks = Counter(), Counter()
        self.events, self.target = [], target
        self.full = threading.Event()
        self.failure = None

    def write(self, obj, account):
        with self.lock:
            self.active += 1
            self.accounts[account] += 1
            self.peak = max(self.peak, self.active)
            self.account_peaks[account] = max(self.account_peaks[account], self.accounts[account])
            self.events.append(("start", obj["kind"], obj["object_id"], account))
            if self.active >= self.target:
                self.full.set()
        try:
            if not self.full.wait(8):
                raise RuntimeError("workers failed to overlap")
            time.sleep(0.015)
            return "deleted", {"success": True}
        finally:
            with self.lock:
                self.events.append(("end", obj["kind"], obj["object_id"], account))
                self.accounts[account] -= 1
                self.active -= 1


class Graph:
    def __init__(self, probe):
        self.probe = probe

    def inspect(self, obj, *args):
        return "pending", {}

    def delete(self, obj):
        return self.probe.write(obj, obj["account_ids"][0])

    def prepare_video_account_delete(self, obj, account):
        return "fake-secret", dict(delete_mode="ad_account_video", delete_account_id=account,
            delete_endpoint="act_" + account + "/advideos", credential_kind="user", credential_user_id="803")

    def delete_video_account(self, obj, account, prepared=None):
        return self.probe.write(obj, account)


class ConcurrentExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "ledger.sqlite3")
        self.budget = ExecutionBudget(8)

    def job(self, name, accounts, phases=("video",)):
        base = 10000 if name == "first" else 20000
        objects = [dict(key=f"{kind}:{base + i}", kind=kind, object_id=str(base + i), status="pending",
                       account_ids=[account], product_ids=["1"], user_ids=["803"], ad_ids=[str(100 + i)],
                       video_account_sources=[dict(source_row_id=str(700 + i), account_id=account,
                           ad_id=str(100 + i), product_id="1", user_id="803")])
                   for kind in phases for i, account in enumerate(accounts)]
        return self.store.create_job(dict(job_id=name, preview_id="preview-" + name, actor=actor_key(SESSION),
            status="ready", input_type="content_id", ids=["100"], products=[PRODUCT], objects=objects))

    def run_job(self, service, job, phases=("video",)):
        run = self.store.claim_run(job["job_id"], job["preview_id"], actor_key(SESSION), phases, uuid.uuid4().hex)
        service._run(job["job_id"], run["run_id"], phases, SESSION)
        return self.store.get_job(job["job_id"])

    def test_two_jobs_share_eight_slots_and_same_accounts_never_overlap(self):
        probe = Probe(target=8)
        service = Service(self.store, FakeSource(), lambda: Graph(probe), execution_budget=self.budget)
        jobs = [self.job(name, [str(400 + i) for i in range(12)]) for name in ("first", "second")]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda job: self.run_job(service, job), jobs))
        self.assertEqual(probe.peak, 8)
        self.assertEqual(set(probe.account_peaks.values()), {1})
        self.assertEqual(len([e for e in probe.events if e[0] == "start"]), 24)
        self.assertTrue(all(r["status"] == "completed" for r in results))
        self.assertFalse(self.budget.accounts)

    def test_phase_barriers_with_parallel_accounts(self):
        probe = Probe(target=4)
        service = Service(self.store, FakeSource(), lambda: Graph(probe), execution_budget=self.budget)
        phases = ("creative", "ad", "video")
        result = self.run_job(service, self.job("first", [str(400 + i) for i in range(4)], phases), phases)
        self.assertEqual(result["status"], "completed")
        for previous, following in zip(phases, phases[1:]):
            self.assertLess(max(i for i, e in enumerate(probe.events) if e[:2] == ("end", previous)),
                            min(i for i, e in enumerate(probe.events) if e[:2] == ("start", following)))

    def test_ledger_failure_drains_requests_before_fencing_and_stops_next_objects(self):
        probe = Probe(target=2)
        budget = ExecutionBudget(2)
        service = Service(self.store, FakeSource(), lambda: Graph(probe), execution_budget=budget)
        job = self.job("first", ["400", "401", "400", "401"])
        original = self.store.finish_video_account
        failed = threading.Event()

        def finish(*args, **kwargs):
            if args[3] == "400" and not failed.is_set():
                failed.set()
                raise StoreError("simulated durable write failure")
            return original(*args, **kwargs)

        self.store.finish_video_account = finish
        result = self.run_job(service, job)
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(probe.active, 0)
        self.assertLess(len([e for e in probe.events if e[0] == "start"]), 4)
        self.assertTrue(any(o["status"] == "unknown" for o in result["objects"]))
        self.assertTrue(any(o["status"] == "pending" for o in result["objects"]))

    def test_throttle_feedback_paces_account_without_poisoning_others(self):
        self.budget.observe(("400",), {"http_status": 400, "error": {"code": 80004}, "usage": {}})
        self.assertGreater(self.budget.cooldowns["400"], time.monotonic())
        self.assertEqual(self.budget.global_until, 0)
        self.budget.wait(("401",), threading.Event())
        self.budget.observe(("400",), {"http_status": 400, "error": {"code": 4}, "usage": {}})
        self.assertGreater(self.budget.global_until, time.monotonic())

    def test_retry_after_is_not_shortened_to_one_hour(self):
        before = time.monotonic()
        self.budget.observe(("400",), {"http_status": 429, "retry_after_seconds": 7200})
        self.assertGreaterEqual(self.budget.cooldowns["400"], before + 7200)

    def test_revocation_after_claim_is_recorded_as_not_sent(self):
        source, probe = FakeSource(), Probe()
        service = Service(self.store, source, lambda: Graph(probe), execution_budget=self.budget)
        claim = self.store.claim_video_account

        def revoke(*args, **kwargs):
            result = claim(*args, **kwargs)
            source.allowed = False
            return result

        self.store.claim_video_account = revoke
        result = self.run_job(service, self.job("first", ["400"]))
        self.assertFalse(probe.events)
        self.assertEqual(result["status"], "interrupted")
        child = result["objects"][0]["video_account_results"][0]
        self.assertEqual(child["status"], "failed")
        self.assertTrue(child["result"]["delete_not_sent"])

    def test_revocation_during_request_cooldown_prevents_delete(self):
        source, probe, waits = FakeSource(), Probe(), []

        class GuardedGraph(Graph):
            def delete_video_account(inner, *args, **kwargs):
                inner.request_guard()
                return super().delete_video_account(*args, **kwargs)

        def wait(accounts, stop):
            waits.append(accounts)
            if len(waits) == 3:  # operation acquisition twice, then immediately before HTTP
                source.allowed = False
                return True
            return False

        self.budget.wait = wait
        service = Service(self.store, source, lambda: GuardedGraph(probe), execution_budget=self.budget)
        result = self.run_job(service, self.job("first", ["400"]))
        self.assertFalse(probe.events)
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(result["objects"][0]["video_account_results"][0]["status"], "failed")

    def test_real_graph_merges_fresh_checks_and_never_replays_timeout(self):
        events = []

        class Response:
            headers = {}

            def __init__(self, status, data):
                self.status_code, self.data = status, data

            def json(self):
                return self.data

        class Transport:
            def request(inner, method, url, **kwargs):
                events.append((method, dict(kwargs["params"])))
                if method == "GET":
                    return Response(200, {"data": []})
                if kwargs["params"]["video_id"] == "10001":
                    raise requests.Timeout("synthetic timeout")
                return Response(400, {"error": {"code": 100, "message": "Param video_id is not a valid video ID"}})

        def credential(obj, account):
            row = obj["video_account_sources"][0]
            return dict(token="fake-current-credential", credential_kind="user", credential_user_id="803",
                credential_relation="publish_queue_user", credential_default_token="-1", credential_publish_queue_id="500",
                credential_source_row_id=row["source_row_id"], credential_ad_id=row["ad_id"],
                credential_product_id="1", credential_source_user_id="803")

        service = Service(self.store, FakeSource(), lambda: GraphClient(lambda users: "", transport=Transport(),
            video_account_credential_provider=credential), execution_budget=self.budget)
        job = self.job("first", ["400"] * 12)
        result = self.run_job(service, job)
        self.assertEqual([m for m, p in events], ["DELETE"] * 12 + ["GET"])
        outcomes = {o["object_id"]: o["status"] for o in result["objects"]}
        self.assertEqual(outcomes.pop("10001"), "unknown")
        self.assertEqual(set(outcomes.values()), {"deleted"})
        before = list(events)
        self.run_job(service, job)
        self.assertEqual(events, before)
        self.assertNotIn("fake-current-credential", str(result))


if __name__ == "__main__":
    unittest.main()
