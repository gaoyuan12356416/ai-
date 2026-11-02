"""Slow completed renders may establish capacity without an unbounded history."""
import datetime as dt
from pathlib import Path
import tempfile
import unittest

from features.pipeline_dispatch.core import DispatchStore, stamp


class LongTaskCapacityTests(unittest.TestCase):
    def test_low_memory_drains_immediately_but_cpu_waits_and_active_claims_survive(self):
        for cpu, available, shrink_at in ((30, .14, 0), (86, .7, 120)):
            with self.subTest(cpu=cpu, available=available), tempfile.TemporaryDirectory() as directory:
                clock = [dt.datetime(2026, 11, 2, 8, tzinfo=dt.timezone.utc)]
                store = DispatchStore(Path(directory) / "pressure.sqlite", clock=lambda: clock[0])
                store.channel_enabled("fb", True)
                metrics = {"cpu_percent": cpu, "memory_available_fraction": available}
                store.register_node("cpu-main", ["prepare:fb"], metrics)
                claims = []
                for index in range(3):
                    store.offer({"source_key": "fb:auto:" + str(index), "source_id": str(index),
                        "channel": "fb", "family": "auto", "stage": "prepare",
                        "scheduled_at": stamp(clock[0]), "capabilities": ["prepare:fb"],
                        "resource_keys": ["fb:page:" + str(index)]})
                    store.worker_heartbeat("cpu-main", str(index), "prepare")
                    if index < 2:
                        claims.append(store.claim("cpu-main", str(index), "prepare")["job"])

                for elapsed in range(0, shrink_at + 1, 60):
                    clock[0] = dt.datetime(2026, 11, 2, 8, tzinfo=dt.timezone.utc) + dt.timedelta(seconds=elapsed)
                    store.register_node("cpu-main", ["prepare:fb"], metrics)
                    for claim in claims:
                        self.assertTrue(store.heartbeat(claim)["accepted"])
                    before = [store.get_job(claim["id"]) for claim in claims]
                    store.adapt_capacity()
                    self.assertEqual(store.config()["hosts"]["cpu-main"]["prepare"]["target"],
                                     1 if elapsed >= shrink_at else 2)
                    self.assertEqual([store.get_job(claim["id"]) for claim in claims], before)
                self.assertTrue(all(store.get_job(claim["id"])["status"] == "running" for claim in claims))
                store.worker_heartbeat("cpu-main", "2", "prepare")
                self.assertIsNone(store.claim("cpu-main", "2", "prepare")["job"])

    def test_three_long_renders_allow_growth_but_keep_cooldown_and_sample_expiry(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = [dt.datetime(2026, 11, 2, 8, tzinfo=dt.timezone.utc)]
            store = DispatchStore(Path(directory) / "capacity.sqlite", clock=lambda: clock[0])
            store.channel_enabled("fb", True)
            metrics = {"cpu_percent": 30, "memory_available_fraction": .7, "cpu_count": 16}

            def refresh():
                store.register_node("cpu-main", ["prepare:fb", "backend:cpu"], metrics)
                store.worker_heartbeat("cpu-main", "prepare-1", "prepare")

            def offer(index):
                store.offer({"source_key": "fb:auto:" + str(index), "source_id": str(index),
                    "channel": "fb", "family": "auto", "stage": "prepare",
                    "scheduled_at": stamp(clock[0]), "capabilities": ["prepare:fb"],
                    "resource_keys": ["fb:page:" + str(index)]})

            def target():
                return store.config()["hosts"]["cpu-main"]["prepare"]["target"]

            refresh()
            offer(1)
            for index in range(1, 4):
                refresh()
                job = store.claim("cpu-main", "prepare-1", "prepare")["job"]
                self.assertEqual(job["source_key"], "fb:auto:" + str(index))
                # Actual lease renewals and a verified portable completion, not
                # synthesized sample counters or short diagnostic jobs.
                for _ in range(28):
                    clock[0] += dt.timedelta(minutes=1)
                    store.heartbeat(job)
                store.complete(job, "render-" + str(index), {"artifact": {
                    "url": "https://media.example/" + str(index) + ".mp4",
                    "sha256": "a" * 64, "size": 1024}}, canonical_verified=True)
                offer(index + 1)
                refresh()
                store.adapt_capacity()
                self.assertEqual(target(), 2 if index < 3 else 3)

            # At this point completions are 56, 28 and 0 minutes old. The old
            # 30-minute window could never collect all three serial samples.
            self.assertEqual(store.adapt_capacity(), [])
            clock[0] += dt.timedelta(minutes=4)
            refresh(); store.adapt_capacity()
            self.assertEqual(target(), 3)
            clock[0] += dt.timedelta(minutes=1)
            refresh(); store.adapt_capacity()
            self.assertEqual(target(), 4)

            admin = {"is_admin": True}
            for ceiling in (3, 4):
                store.set_capacity({"host_id": "cpu-main", "stage": "prepare", "max_workers": ceiling,
                    "expected_version": store.config()["version"]}, admin)
            clock[0] += dt.timedelta(hours=6)
            refresh(); store.adapt_capacity()
            self.assertEqual(target(), 3, "expired historical completions must not grow capacity")


if __name__ == "__main__":
    unittest.main()
