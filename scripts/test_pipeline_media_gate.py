import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from features.pipeline_dispatch import media_decision, media_gate
from features.pipeline_dispatch.adapters import (
    LedgerAdapter,
    LedgerSpec,
    fb_video_appointment,
)
from features.pipeline_dispatch.core import DispatchStore, stamp
from features.pipeline_dispatch.service import Coordinator


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = dt.datetime(2026, 11, 19, 12, tzinfo=dt.timezone.utc)
        self.store = DispatchStore(
            Path(self.temp.name) / "dispatch.db", clock=lambda: self.now
        )
        self.store.channel_enabled("fb", True)
        self.base = {
            "source_key": "fb:auto:1",
            "source_id": "1",
            "channel": "fb",
            "family": "auto",
            "stage": "prepare",
            "scheduled_at": stamp(self.now - dt.timedelta(hours=2)),
            "source_created_at": stamp(self.now - dt.timedelta(hours=2)),
            "source_status": "planned",
            "capabilities": ["prepare:fb"],
            "resource_keys": ["fb:page:a"],
            "execution_available": True,
            "media_context": media_decision.build_context(
                "https://example.org/media.mp4", mode="random_overlay"
            ),
            "canonical_fingerprint": "abc",
            "media_publish_at": stamp(self.now + dt.timedelta(minutes=5)),
        }

    def worker(self):
        self.store.register_node("cpu-main", ["prepare:fb"])
        self.store.worker_heartbeat("cpu-main", "worker", "prepare")
        return self.store.claim("cpu-main", "worker", "prepare")["job"]

    def test_boundary_and_no_render_slots(self):
        job = self.store.offer(self.base)
        self.now -= dt.timedelta(milliseconds=1)
        media_gate.sweep(self.store)
        self.assertEqual(media_gate.pending(self.store, 5), [])
        self.now += dt.timedelta(milliseconds=1)
        media_gate.sweep(self.store)
        self.assertIsNone(self.worker())
        item, ticket, decision = media_gate.pending(self.store, 5)[0]
        self.assertEqual(
            (item["id"], decision["kind"]), (job["id"], "deadline_original")
        )
        media_gate.close(
            self.store,
            item,
            ticket,
            decision,
            {"retry": True, "reason": "native_unavailable"},
        )
        self.now += dt.timedelta(seconds=11)
        self.assertEqual(media_gate.pending(self.store, 5)[0][1], ticket)
        media_gate.close(self.store, item, ticket, decision, {"applied": True})
        self.assertEqual(self.store.get_job(job["id"])["hold_reason"], "metadata_only")
        self.assertIsNone(self.worker())

    def test_running_or_immediate_is_not_retired(self):
        first = self.store.offer(dict(self.base, media_publish_at=""))
        media_gate.sweep(self.store)
        self.assertEqual(self.worker()["id"], first["id"])
        self.store.offer(self.base)
        media_gate.sweep(self.store)
        self.assertEqual(media_gate.pending(self.store, 5), [])
        self.assertEqual(self.store.get_job(first["id"])["status"], "running")

    def test_reuse_then_bad_candidate_returns_to_render(self):
        artifact = {
            "url": "https://example.org/produced.mp4",
            "size": 1000,
            "sha256": "a" * 64,
            "mime": "video/mp4",
            "profile": "x-random-overlay-h264-720x1280-v1",
            "duration_seconds": 30,
        }
        self.store.offer(
            dict(
                self.base,
                source_key="x:queue:2",
                source_id="2",
                channel="x",
                family="queue",
                stage="publish",
                artifact=artifact,
                source_status="ready",
            )
        )
        job = self.store.offer(dict(self.base, media_publish_at=""))
        self.assertIsNone(self.worker())
        item, ticket, decision = media_gate.pending(self.store, 5)[0]
        self.assertEqual(decision["kind"], "cross_platform_reuse")
        media_gate.close(
            self.store, item, ticket, decision, {"candidate_invalid": True}
        )
        self.assertEqual(self.worker()["id"], job["id"])

    def test_raw_appointment_is_distinct_from_preparation_eligibility(self):
        ad = LedgerAdapter(
            LedgerSpec("fb", "auto", "unused", "unused"), driver=object()
        )
        native = {
            "id": 1,
            "status": "planned",
            "page_id": "1",
            "account_id": "2",
            "created_at": "2026-11-19T00:00:00Z",
            "scheduled_at": "2026-11-19T12:00:00Z",
            "_media_publish_at": "2026-11-19T12:00:00Z",
        }
        offer = ad.project(native, self.now)
        self.assertEqual(offer["media_publish_at"], "2026-11-19T12:00:00.000+00:00")
        self.assertEqual(offer["scheduled_at"], "2026-11-19T00:00:00Z")
        self.assertFalse(
            fb_video_appointment(
                dict(planned_at_utc=offer["scheduled_at"]),
                {"request_json": '{"schedule_mode":"now"}'},
            )
        )

    def test_fb_release_timing_preserves_preparation_on_the_previous_day(self):
        adapter = LedgerAdapter(LedgerSpec("fb", "auto", "", ""), driver=object())
        start, retry, release = (
            "2026-11-18T16:56:02Z",
            "2026-11-19T01:00:00Z",
            "2026-11-19T16:00:00Z",
        )
        row = dict(
            id=1,
            status="planned",
            page_id="1",
            account_id="2",
            created_at=start,
            scheduled_at=release,
            _media_publish_at=release,
            schedule_policy="target_midnight_v1",
            selection_json={
                "hit_publish_timing": {
                    "prepare_at_utc": start,
                    "publish_at_utc": release,
                }
            },
        )
        offer = adapter.project(row, self.now)
        self.assertEqual(offer["scheduled_at"], start)
        self.assertEqual(offer["media_publish_at"], "2026-11-19T16:00:00.000+00:00")
        self.assertEqual(
            adapter.project(dict(row, next_prepare_at_utc=retry), self.now)[
                "scheduled_at"
            ],
            retry,
        )
        row["selection_json"]["hit_publish_timing"]["publish_at_utc"] = retry
        self.assertEqual(adapter.project(row, self.now)["scheduled_at"], release)

    def test_x_preparation_open_time_is_independent_of_parent_appointment(self):
        adapter = LedgerAdapter(LedgerSpec("x", "queue", "", ""), driver=object())
        row = dict(
            id=7,
            status="queued",
            account_id=3,
            random_template_enabled=1,
            material_url="https://example.org/m.mp4",
            scheduled_at="2026-11-18T16:00:00Z",
            _media_publish_at="2026-11-19T08:00:00Z",
        )
        offer = adapter.project(row, self.now)
        self.assertEqual(offer["stage"], "prepare")
        self.assertEqual(offer["scheduled_at"], row["scheduled_at"])
        self.assertEqual(offer["media_publish_at"], "2026-11-19T08:00:00.000+00:00")

    def test_committed_decision_lost_ack_reuses_ticket_before_creating_publish(self):
        job = self.store.offer(self.base)
        media_gate.sweep(self.store)
        item, ticket, decision = media_gate.pending(self.store, 5)[0]
        current = dict(self.base)
        accepted = {}

        def commit(action, body):
            self.assertEqual(action, "media-decision")
            if not accepted:
                accepted[body["decision_id"]] = True
                current.update(
                    stage="publish",
                    source_status="ready",
                    scheduled_at=self.base["media_publish_at"],
                )
                raise ConnectionError("lost ACK")
            self.assertIn(body["decision_id"], accepted)
            return {"applied": True}

        reg = Mock()
        adapter = reg._adapter.return_value
        adapter.read.side_effect = lambda _: dict(current)
        adapter.project.side_effect = lambda row: row
        adapter.driver.control_only = False
        adapter.driver.call.side_effect = commit
        coord = object.__new__(Coordinator)
        coord.store, coord.registry = self.store, reg
        coord.deliver_media(item, ticket, decision)
        self.now += dt.timedelta(seconds=11)
        coord.deliver_media(*media_gate.pending(self.store, 3)[0])
        with self.store.transaction() as c:
            self.assertEqual(
                c.execute(
                    "SELECT count(*) FROM pipeline_job WHERE source_key=? AND stage='publish'",
                    (job["source_key"],),
                ).fetchone()[0],
                1,
            )
        self.assertEqual(self.store.get_job(job["id"])["status"], "completed")


if __name__ == "__main__":
    unittest.main()
