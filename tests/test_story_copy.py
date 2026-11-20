"""Offline acceptance of the same decisions called by the native owner paths."""

import json
import random
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from features.story_copy import alerts, bridge, generator, model, sources, store, worker
from features.story_copy import evidence, settings
from ops.story_copy import refresh_click_hooks, repair_drafts

ROOT = Path(__file__).resolve().parents[1]


class CaptionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        db = self.root / "source.db"
        self.fb = db
        for patcher in [
            patch.object(store, "ROOT", self.root),
            patch.object(store, "DB", self.root / "copy.db"),
            patch.object(store, "TAG_POSTS", str(db)),
            patch.object(
                store,
                "SOURCES",
                {k: (e, str(db), tbl) for k, (e, _, tbl) in store.SOURCES.items()},
            ),
        ]:
            patcher.start()
            self.addCleanup(patcher.stop)
        store.initialize()
        with store.connection(db) as d:
            for t in ("x_post_queue", "x_auto_task", "fb_auto_task"):
                d.execute("CREATE TABLE " + t + "(id INTEGER PRIMARY KEY)")
            d.execute(
                "CREATE TABLE fb_video_task(task_id TEXT,caption TEXT,short_url TEXT,long_url TEXT,content_id TEXT,snapshot_json TEXT,material_id TEXT,source_kind TEXT)"
            )
            d.execute(store.RECEIPTS)
        for ep in model.ENDPOINTS:
            store.change_setting(ep, True, "fixture")

    def task(self, i, f="xm"):
        t = sources.base(
            f,
            i,
            "slot-" + str(i),
            "story",
            "en",
            "DramaWave",
            "The Rivals",
            "Two heirs clash over a letter",
            1,
            material_id=str(i),
            urgent=True,
        )
        if f == "xd":
            t["episode"] = 3
        return t

    def item(self, t):
        rng = random.Random(t["key"])

        def sentence():
            return " ".join(
                "".join(rng.choices("abcdefghijklmnopqrstuvwxyz", k=5))
                for n in range(5)
            )

        return dict(
            id=t["key"],
            language=t["language"],
            refs=["series"],
            scope="series",
            angle="identity",
            motif="intrigue",
            hooks=[
                "to find " + sentence().split()[0],
                "to see " + sentence().split()[0],
            ],
            opening=sentence(),
            turn=sentence(),
            tags=["ShortDrama", "Mystery"],
        )

    def test_deadline_priority_and_near_group_does_not_claim_tomorrows_work(self):
        now = time.time()
        later = dict(self.task(1), content="old-group", publish_at=now + 7200)
        near = dict(self.task(2), content="near-group", publish_at=now + 30, urgent=False)
        tomorrow = dict(self.task(3), content="near-group", publish_at=now + 86400)
        sooner = dict(self.task(4), content="near-group", publish_at=now + 10, urgent=False)
        with store.connection() as c:
            for t in (later, near, tomorrow, sooner):
                store.enqueue(c, t)
            c.commit()
            _, rows = store.claim(c)
            self.assertEqual([t["key"] for t in rows], [sooner["key"], near["key"]])
            _, rows = store.claim(c)
            self.assertEqual([t["key"] for t in rows], [later["key"]])

    def test_future_schedule_retains_collection_window(self):
        now = time.time()
        with store.connection() as c:
            t = dict(self.task(1), urgent=False, publish_at=now + 3600)
            store.enqueue(c, t)
            c.commit()
            self.assertFalse(store.claim(c)[1])
            with patch.object(store.time, "time", return_value=now + 61):
                self.assertEqual(len(store.claim(c)[1]), 1)

    def test_deadline_refresh_preserves_facts_and_finalized_copy(self):
        t = dict(self.task(1), publish_at=100)
        with store.connection() as c:
            store.enqueue(c, t)
            changed = dict(t, publish_at=200, facts={"series": "Changed source fact"})
            store.enqueue(c, changed)
            payload = json.loads(c.execute("SELECT payload FROM jobs").fetchone()[0])
            self.assertEqual(payload["publish_at"], 200)
            self.assertEqual(payload["facts"], t["facts"])
            c.execute("UPDATE jobs SET state='ready'")
            c.commit()
            store.enqueue(c, dict(t, publish_at=300))
            self.assertFalse(c.in_transaction)
            self.assertEqual(json.loads(c.execute("SELECT payload FROM jobs").fetchone()[0]), payload)

    def test_due_fresh_job_precedes_future_retry(self):
        now = time.time()
        with store.connection() as c:
            old = dict(self.task(1), publish_at=now + 3600)
            store.enqueue(c, old)
            c.commit()
            b, rows = store.claim(c)
            worker.generate_batch(c, b, rows, self.provider_down)
            fresh = dict(self.task(2), content="near", publish_at=now + 10)
            store.enqueue(c, fresh)
            c.commit()
            _, rows = store.claim(c)
            self.assertEqual([t["key"] for t in rows], [fresh["key"], old["key"]])

    def test_native_schedule_mapping_all_entry_points(self):
        stamp = "2026-11-20T01:00:00Z"
        epoch = sources.publish_time(stamp)
        self.assertEqual(epoch, sources.publish_time("invalid", "2026-11-20T09:00:00+08:00"))
        self.assertIsNone(sources.publish_time("invalid", ""))
        fb = dict(id=1, content_id="story", planned_publish_at_utc=stamp)
        self.assertEqual(sources.fb_task("fa", fb)["publish_at"], epoch)
        video = dict(_ord=1, task_id="v1", content_id="story", planned_at_utc=stamp)
        self.assertEqual(sources.fb_task("fv", video)["publish_at"], epoch)
        tag = dict(id=1, content_id="story", material_id="m1", language="en", template_version=1,
                   scheduled_at_utc=stamp, selection_json={"material": {"material_id": "m1", "content_id": "story"}})
        self.assertEqual(sources.tag_task(tag)["publish_at"], epoch)
        with store.connection(self.fb) as c:
            c.execute("CREATE TABLE x_post_manual_run(id INTEGER,trigger_source TEXT,scheduled_at TEXT,planned_publish_at_utc TEXT,external_task_key TEXT,template_version INTEGER,copy_contract_version INTEGER)")
            c.execute("INSERT INTO x_post_manual_run VALUES(1,'manual',?,'','',1,1)", (stamp,))
            c.execute("CREATE TABLE x_post_schedule_run(id INTEGER,run_date TEXT,publish_time TEXT,planned_publish_at_utc TEXT)")
            c.execute("INSERT INTO x_post_schedule_run VALUES(1,'2026-11-20','09:00:00','')")
            row = dict(id=1, manual_run_id=1, source_type="material", content_id="story", material_id="m1",
                       material_language="en", drama_name="Title", description="Fact")
            self.assertEqual(sources.x_task(c, row)["publish_at"], epoch)
            row.update(manual_run_id=None, schedule_run_id=1)
            self.assertEqual(sources.x_task(c, row)["publish_at"], epoch)

    def test_three_generation_workers_claim_once_and_overlap(self):
        tasks = [dict(self.task(i), content="story-" + str(i)) for i in range(1, 4)]
        with store.connection() as c:
            for t in tasks:
                store.enqueue(c, t)
        barrier = threading.Barrier(3)
        seen = []
        lock = threading.Lock()

        def run(rows, emit, usage):
            with lock:
                seen.extend(t["key"] for t in rows)
            barrier.wait(timeout=10)
            for t in rows:
                emit(self.item(t))

        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(worker.generation_cycle, run) for _ in range(3)]
            self.assertTrue(all(f.result(timeout=15) for f in futures))
        self.assertCountEqual(seen, [t["key"] for t in tasks])
        with store.connection() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM jobs WHERE state='ready'").fetchone()[0], 3)
            self.assertEqual(c.execute("SELECT count(*) FROM batches WHERE finished IS NOT NULL AND accepted=1").fetchone()[0], 3)
            self.assertEqual(c.execute("SELECT count(*) FROM retries").fetchone()[0], 0)

    def test_older_tag_publisher_schedule_schema(self):
        stamp = "2026-11-20T01:00:00Z"
        with store.connection(self.fb) as c:
            c.execute("CREATE TABLE x_post_manual_run(id INTEGER,trigger_source TEXT,external_task_key TEXT,template_version INTEGER,copy_contract_version INTEGER,scheduled_at TEXT)")
            c.execute("INSERT INTO x_post_manual_run VALUES(1,'manual','',1,1,?)", (stamp,))
            c.execute("CREATE TABLE x_post_schedule_run(id INTEGER,run_date TEXT,publish_time TEXT)")
            c.execute("INSERT INTO x_post_schedule_run VALUES(1,'2026-11-20','09:00:00')")
            row = dict(id=1, manual_run_id=1, source_type="material", content_id="story", material_id="m1",
                       material_language="en", drama_name="Title", description="Fact")
            for manual, scheduled in [(1, None), (None, 1)]:
                row.update(manual_run_id=manual, schedule_run_id=scheduled)
                task = sources.x_task(c, row, tag=True)
                self.assertEqual(task["family"], "tm")
                self.assertEqual(task["publish_at"], sources.publish_time(stamp))

    def test_live_batch_recovery_does_not_interrupt_other_worker(self):
        with store.connection() as c:
            for i in (1, 2):
                store.enqueue(c, dict(self.task(i), content="story-" + str(i)))
        barrier = threading.Barrier(2)
        release = threading.Event()
        original = worker.generate_batch

        def generate(c, batch, rows, runner):
            barrier.wait(timeout=10)
            if rows[0]["key"] == "slot-1":
                raise RuntimeError("fixture worker failure")
            self.assertTrue(release.wait(timeout=10))
            original(c, batch, rows, lambda items, emit, usage: [emit(self.item(t)) for t in items])

        with patch.object(worker, "generate_batch", side_effect=generate), ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(worker.generation_cycle) for _ in range(2)]
            try:
                deadline = time.monotonic() + 10
                while not any(f.done() for f in futures) and time.monotonic() < deadline:
                    time.sleep(0.01)
                failed = next(f for f in futures if f.done())
                with self.assertRaisesRegex(RuntimeError, "fixture worker failure"):
                    failed.result()
                with store.connection() as c:
                    self.assertEqual(c.execute("SELECT state FROM jobs WHERE key='slot-2'").fetchone()[0], "generating")
                    self.assertEqual(c.execute("SELECT count(*) FROM batches WHERE finished IS NULL").fetchone()[0], 1)
                    self.assertEqual(c.execute("SELECT count(*) FROM retries WHERE key='slot-2'").fetchone()[0], 0)
            finally:
                release.set()
            for f in futures:
                if f is not failed:
                    self.assertTrue(f.result(timeout=10))
        with store.connection() as c:
            self.assertEqual(c.execute("SELECT state FROM jobs WHERE key='slot-2'").fetchone()[0], "ready")

    def test_native_receipt_wins_against_inflight_generation(self):
        task = self.task(1)
        with store.connection() as c:
            store.enqueue(c, task)
        entered = threading.Event()
        release = threading.Event()

        def run(rows, emit, usage):
            entered.set()
            self.assertTrue(release.wait(timeout=10))
            emit(self.item(task))

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(worker.generation_cycle, run)
            try:
                self.assertTrue(entered.wait(timeout=10))
                with store.connection(self.fb) as native:
                    bridge.receipt(native, task, "legacy", "not_ready_at_publish", "frozen", "", None)
                with store.connection() as c:
                    worker.sync_receipts(c)
            finally:
                release.set()
            self.assertTrue(future.result(timeout=10))
        with store.connection() as c:
            row = c.execute("SELECT state,final,draft FROM jobs").fetchone()
            self.assertEqual(tuple(row), ("legacy", "frozen", None))
            self.assertEqual(c.execute("SELECT count(*) FROM retries").fetchone()[0], 0)

    def test_worker_concurrency_configuration(self):
        for value, expected in [("3", 3), ("1", 1), ("0", 3), ("bad", 3)]:
            with patch.dict("os.environ", {"STORY_COPY_CONCURRENCY": value}):
                self.assertEqual(worker.concurrency(), expected)

    def test_prompt_uses_current_facts_without_history_or_variation(self):
        tasks = [self.task(1), self.task(2, "fa")]
        for t in tasks:
            t["facts"] = {"series": "Ada hides the seal. Ben finds the ribbon."}
        request = json.loads(generator.prompt(tasks))
        self.assertEqual(request["click_prefix"], "watch full now")
        self.assertEqual(len(request["fact_text"]), 1)
        self.assertNotIn("recent", request)
        for item in request["tasks"]:
            self.assertNotIn("focus", item)
            self.assertNotIn("suggested_angle", item)

    def test_manual_repair_preserves_native_receipts_and_auto_retry_history(self):
        task = self.task(1)
        with store.connection() as c:
            store.enqueue(c, task)
            c.commit()
            first, _ = store.claim(c)
            store.fail(c, first, task["key"], "codex_generation_failed")
            c.execute("UPDATE retries SET retry_after=0")
            c.commit()
            second, _ = store.claim(c)
            store.fail(c, second, task["key"], "codex_generation_failed")
            c.commit()
            before = dict(c.execute("SELECT * FROM jobs").fetchone())
            original_retry = dict(c.execute("SELECT * FROM retries").fetchone())
            draft = model.validate(task, self.item(task))
            with store.connection(self.fb) as src:
                src.execute("INSERT INTO story_copy_receipt VALUES(?,?,?,?,?,?,?,?)",
                            (task["key"], "x_material", "legacy", "", "frozen text", "https://example.org/x", second, 1))
            self.assertFalse(repair_drafts.save_candidate(c, before, draft, 99))
            with store.connection(self.fb) as src:
                src.execute("DELETE FROM story_copy_receipt")
            self.assertTrue(repair_drafts.save_candidate(c, before, draft, 99))
            self.assertFalse(repair_drafts.save_candidate(c, before, draft, 100))
            repaired = dict(c.execute("SELECT * FROM jobs").fetchone())
            self.assertEqual((repaired["state"], repaired["reason"], repaired["batch"]), ("ready", "", 99))
            self.assertEqual(dict(c.execute("SELECT * FROM retries").fetchone()), original_retry)
            revised = dict(draft, cta="different pending CTA")
            self.assertTrue(repair_drafts.save_candidate(c, repaired, revised))
            self.assertFalse(repair_drafts.save_candidate(c, repaired, revised))

    def test_100_and_101_partial_failure(self):
        with store.connection() as c:
            for i in range(1, 102):
                store.enqueue(c, self.task(i))
            c.commit()
            batch, rows = store.claim(c)
            self.assertEqual(100, len(rows))
            calls = []

            def partial(part, emit, usage):
                calls.append(len(part))
                usage.update(inputTokens=1500, outputTokens=19000)
                for t in part[:97]:
                    emit(self.item(t))
                raise RuntimeError("generation_timeout")

            worker.generate_batch(c, batch, rows, partial)
            self.assertEqual(calls, [100])
            self.assertEqual(
                c.execute("SELECT count(*) FROM jobs WHERE state='ready'").fetchone()[
                    0
                ],
                97,
            )
            self.assertEqual(
                c.execute("SELECT count(*) FROM jobs WHERE state='retry_wait'").fetchone()[
                    0
                ],
                3,
            )
            sent = []
            alerts.deliver(c, lambda event, body: sent.append(body) or "receipt")
            self.assertEqual(sent, [])
            retry_batch, retry_rows = store.claim(c)
            self.assertEqual(len(retry_rows), 4)
            self.assertEqual({t["key"] for t in retry_rows}, {self.task(i)["key"] for i in range(98, 102)})

            def second_failure(part, emit, usage):
                emit(self.item(self.task(101)))
                raise RuntimeError("generation_timeout")

            worker.generate_batch(c, retry_batch, retry_rows, second_failure)
            self.assertEqual(c.execute("SELECT count(*) FROM jobs WHERE state='ready'").fetchone()[0], 98)
            self.assertEqual(c.execute("SELECT count(*) FROM jobs WHERE state='failed'").fetchone()[0], 3)
            alerts.deliver(c, lambda event, body: sent.append(body) or "receipt")
            self.assertEqual(len(sent), 1)
            self.assertIn("影响 3 条", sent[0])
            self.assertIn("重试一次仍失败", sent[0])
            self.assertFalse(store.claim(c)[1])
            for t in rows[97:]:
                self.assertEqual(
                    bridge.select(t, "原正文", "https://example.org/task")[2], "原正文"
                )

    @staticmethod
    def provider_down(rows, emit, usage):
        raise RuntimeError("codex_generation_failed")

    def test_retry_joins_next_queue_and_keeps_prompt_groups_separate(self):
        with store.connection() as c:
            old = [self.task(i, f) for i, f in enumerate(("fa", "xm", "xd", "ta"), 1)]
            for t in old:
                store.enqueue(c, t)
            c.commit()
            first, rows = store.claim(c)
            worker.generate_batch(c, first, rows, self.provider_down)
            fresh = dict(self.task(5), content="other", title="The Seal", product="hotdrama")
            fresh["facts"] = {"series": "A queen hides a seal."}
            store.enqueue(c, fresh)
            c.commit()
            second, rows = store.claim(c)
            self.assertEqual(len(rows), 5)
            seen = []

            def success(chunk, emit, usage):
                self.assertEqual(len({model.group_key(t) for t in chunk}), 1)
                request = json.loads(generator.prompt(chunk))
                seen.append((request["title"], request["product"], len(chunk)))
                for t in chunk:
                    emit(self.item(t))

            worker.generate_batch(c, second, rows, success)
            self.assertEqual(seen, [("The Rivals", "dramawave", 4), ("The Seal", "hotdrama", 1)])
            self.assertEqual(c.execute("SELECT count(*) FROM jobs WHERE state='ready' AND reason=''").fetchone()[0], 5)
            retries = c.execute("SELECT * FROM retries").fetchall()
            self.assertEqual(len(retries), 4)
            for r in retries:
                self.assertEqual((r["first_batch"], r["retry_batch"], r["state"]), (first, second, "succeeded"))
                self.assertEqual(r["first_reason"], "codex_generation_failed")
            self.assertEqual(c.execute("SELECT count(*) FROM alerts").fetchone()[0], 0)
            self.assertEqual(bridge.select(old[0], "legacy", "https://example.org/t")[0], "ai")

    def test_retry_survives_restart_and_runs_without_new_work_after_window(self):
        with store.connection() as c:
            store.enqueue(c, self.task(1))
            c.commit()
            first, rows = store.claim(c)
            worker.generate_batch(c, first, rows, self.provider_down)
            due = c.execute("SELECT retry_after FROM retries").fetchone()[0]
        store.initialize()
        with store.connection() as c:
            worker.restore(c)
            with patch.object(store.time, "time", return_value=due - 1):
                self.assertFalse(store.claim(c)[1])
            with patch.object(store.time, "time", return_value=due):
                second, rows = store.claim(c)
            self.assertEqual(len(rows), 1)
            worker.generate_batch(c, second, rows, lambda *args: None)
            r = c.execute("SELECT * FROM retries").fetchone()
            self.assertEqual(r["state"], "failed")
            self.assertEqual(r["reason"], "missing_model_row")
            self.assertEqual(r["first_reason"], "codex_generation_failed")
            self.assertEqual(c.execute("SELECT count(*) FROM alerts").fetchone()[0], 1)
            with patch.object(store.time, "time", return_value=due + 86400):
                self.assertFalse(store.claim(c)[1])

    def test_invalid_generated_copy_also_gets_one_retry(self):
        with store.connection() as c:
            task = self.task(1)
            store.enqueue(c, task)
            c.commit()
            first, rows = store.claim(c)
            worker.generate_batch(c, first, rows, lambda rows, emit, usage: emit(dict(self.item(task), language="fr")))
            self.assertEqual(c.execute("SELECT state FROM jobs").fetchone()[0], "retry_wait")
            self.assertEqual(c.execute("SELECT count(*) FROM alerts").fetchone()[0], 0)
            c.execute("UPDATE retries SET retry_after=0")
            c.commit()
            second, rows = store.claim(c)
            worker.generate_batch(c, second, rows, lambda rows, emit, usage: emit(self.item(task)))
            self.assertEqual(c.execute("SELECT state FROM jobs").fetchone()[0], "ready")
            self.assertEqual(c.execute("SELECT count(*) FROM alerts").fetchone()[0], 0)

    def test_interrupted_attempts_keep_successes_and_never_retry_twice(self):
        with store.connection() as c:
            for i in (1, 2):
                store.enqueue(c, self.task(i))
            c.commit()
            first, rows = store.claim(c)
            store.accept(c, first, rows[0], self.item(rows[0]))
            worker.restore(c)
            self.assertEqual(c.execute("SELECT count(*) FROM jobs WHERE state='ready'").fetchone()[0], 1)
            self.assertEqual(c.execute("SELECT count(*) FROM jobs WHERE state='retry_wait'").fetchone()[0], 1)
            self.assertEqual(c.execute("SELECT count(*) FROM alerts").fetchone()[0], 0)
            c.execute("UPDATE retries SET retry_after=0")
            c.commit()
            second, rows = store.claim(c)
            self.assertEqual(len(rows), 1)
            worker.restore(c)
            worker.restore(c)
            self.assertEqual(c.execute("SELECT count(*) FROM alerts").fetchone()[0], 1)
            self.assertEqual(c.execute("SELECT state,reason FROM retries").fetchone()[:], ("failed", "worker_interrupted"))
            self.assertFalse(store.claim(c)[1])

    def test_native_receipt_cancels_waiting_or_inflight_retry_without_alert(self):
        for i, inflight in enumerate((False, True), 1):
            with self.subTest(inflight=inflight), store.connection() as c:
                task = self.task(i)
                store.enqueue(c, task)
                c.commit()
                first, rows = store.claim(c)
                worker.generate_batch(c, first, rows, self.provider_down)
                if inflight:
                    c.execute("UPDATE retries SET retry_after=0")
                    c.commit()
                    second, rows = store.claim(c)
                with store.connection(self.fb) as native:
                    bridge.receipt(native, task, "legacy", "codex_generation_failed", "frozen text", "https://example.org/t", first)
                worker.sync_receipts(c)
                if inflight:
                    worker.generate_batch(c, second, rows, lambda rows, emit, usage: emit(self.item(task)))
                job = c.execute("SELECT * FROM jobs WHERE key=?", (task["key"],)).fetchone()
                self.assertEqual((job["state"], job["final"], job["draft"]), ("legacy", "frozen text", None))
                self.assertEqual(c.execute("SELECT state FROM retries WHERE key=?", (task["key"],)).fetchone()[0], "finalized")
                self.assertEqual(c.execute("SELECT count(*) FROM alerts").fetchone()[0], 0)
                self.assertFalse(store.claim(c)[1])

    def test_retry_schema_preserves_historical_failures(self):
        with store.connection() as c:
            store.enqueue(c, self.task(1))
            c.execute("UPDATE jobs SET state='failed',reason='codex_generation_failed'")
            c.execute("DROP TABLE retries")
        store.initialize()
        with store.connection() as c:
            self.assertFalse(store.claim(c)[1])
            self.assertEqual(c.execute("SELECT count(*) FROM retries").fetchone()[0], 0)

    def test_retry_prompts_keep_languages_and_facts_separate(self):
        english = self.task(1)
        french = dict(self.task(2), language="fr", title="Le Sceau", facts={"series": "La reine cache un sceau."})
        groups = list(generator.splits([english, french, self.task(3)]))
        self.assertEqual([len(g) for g in groups], [2, 1])
        requests = [json.loads(generator.prompt(g)) for g in groups]
        self.assertEqual([r["title"] for r in requests], ["The Rivals", "Le Sceau"])
        self.assertEqual({t["language"] for t in requests[0]["tasks"]}, {"en"})
        self.assertEqual({t["language"] for t in requests[1]["tasks"]}, {"fr"})
        self.assertNotIn("Two heirs", json.dumps(requests[1]))

    def test_publish_batch_alert_waits_for_remaining_retry(self):
        with store.connection() as c:
            task = dict(self.task(1), publish_batch="native-batch")
            store.enqueue(c, task)
            c.commit()
            first, rows = store.claim(c)
            worker.generate_batch(c, first, rows, self.provider_down)
            other = dict(self.task(2), publish_batch="native-batch")
            alerts.add(c, "other-final-failure", [worker.alert_item(other, "codex_generation_failed")])
            c.commit()
            sent = []
            alerts.deliver(c, lambda event, body: sent.append(body) or "receipt")
            self.assertEqual(sent, [])
            c.execute("UPDATE retries SET retry_after=0")
            c.commit()
            second, rows = store.claim(c)
            worker.generate_batch(c, second, rows, self.provider_down)
            alerts.deliver(c, lambda event, body: sent.append(body) or "receipt")
            self.assertEqual(len(sent), 1)
            self.assertIn("影响 2 条", sent[0])

    def test_generation_ignores_unavailable_history_and_uses_repeated_copy(self):
        with store.connection() as c:
            tasks = [self.task(i, f) for i, f in enumerate(("fa", "xm", "xd", "ta"), 1)]
            for task in tasks:
                store.enqueue(c, task)
            c.commit()
            batch, rows = store.claim(c)
            repeated = self.item(rows[0])
            repeated["hooks"] = ["to find the seal"]
            # Missing history tables/native databases must not affect generation.
            c.execute("DROP TABLE history")
            c.commit()
            queries = []
            c.set_trace_callback(queries.append)

            def provider(part, emit, usage):
                for task in part:
                    emit(dict(repeated, id=task["key"]))

            with patch.object(store, "connection", side_effect=sqlite3.OperationalError("database is locked")):
                worker.generate_batch(c, batch, rows, provider)
            c.set_trace_callback(None)
            self.assertEqual(c.execute("SELECT count(*) FROM jobs WHERE state='ready'").fetchone()[0], 4)
            self.assertEqual(c.execute("SELECT count(DISTINCT hash) FROM jobs").fetchone()[0], 1)
            self.assertEqual(c.execute("SELECT count(*) FROM alerts").fetchone()[0], 0)
            self.assertFalse(any("SELECT draft" in q or "json_extract(draft" in q for q in queries))
        for task in tasks:
            self.assertEqual(bridge.select(task, "legacy", "https://example.org/post")[0], "ai")

    def test_short_batch_window_and_request_budget(self):
        with store.connection() as c:
            t = self.task(1)
            t["urgent"] = False
            store.enqueue(c, t)
            c.commit()
            self.assertFalse(store.claim(c)[1])
            c.execute("UPDATE jobs SET created=0")
            c.commit()
            self.assertEqual(len(store.claim(c)[1]), 1)
        with patch.dict("os.environ", {"STORY_COPY_OUTPUT_BUDGET": "2600"}):
            pieces = list(generator.splits([self.task(i) for i in range(100)]))
            self.assertEqual(sum(map(len, pieces)), 100)
            self.assertLessEqual(max(map(len, pieces)), 10)

    def test_store_down_does_not_block_native_fb_claim_or_change_legacy(self):
        snapshot = {
            "material": {
                "drama_name": "Title",
                "drama_description": "An heir is missing",
                "language": "en",
                "source_app_id": 1479,
            }
        }
        with store.connection(self.fb) as src:
            src.execute(
                "INSERT INTO fb_video_task VALUES('abc','keep exact','', '', 'story', ?, '5','manual')",
                (store.dump(snapshot),),
            )
            row = src.execute("SELECT * FROM fb_video_task").fetchone()
            with patch.object(store, "DB", self.root / "missing.db"):
                bridge.fb_final(src, "fv", row)
            self.assertEqual(
                src.execute("SELECT caption FROM fb_video_task").fetchone()[0],
                "keep exact",
            )
            self.assertEqual(
                src.execute("SELECT reason FROM story_copy_receipt").fetchone()[0],
                "caption_store_unavailable",
            )
            bridge.fb_final(src, "fv", row)
            self.assertEqual(
                src.execute("SELECT count(*) FROM story_copy_receipt").fetchone()[0], 1
            )

    def test_sqlite_lock_and_manual_priority(self):
        t = self.task(2)
        with store.connection() as c:
            store.enqueue(c, t)
            c.commit()
            self.assertEqual(
                bridge.select(dict(t, manual=True), "手填", "")[0], "manual"
            )
            self.assertEqual(bridge.select(t, "exact legacy", "")[2], "exact legacy")
        for t in ("x_material", "x_drama", "fb"):
            store.change_setting(t, False, "fixture")
        self.assertTrue(store.setting("x_tag_auto"))
        with store.connection() as c:
            self.assertFalse(store.eligible(c, "xm", 1000))
            self.assertFalse(store.eligible(c, "tm", 1000))
            self.assertTrue(store.eligible(c, "ta", 1000))

    def test_identical_copy_across_channels_and_story_only_episode(self):
        with store.connection() as c:
            ts = [self.task(i + 1, f) for i, f in enumerate(["fa", "xm", "xd", "ta"])]
            for t in ts:
                store.enqueue(c, t)
            c.commit()
            batch, rows = store.claim(c)
            sample = self.item(ts[0])
            self.assertTrue(store.accept(c, batch, ts[0], sample))
            for t in ts[1:]:
                self.assertTrue(
                    store.accept(
                        c, batch, t, dict(sample, id=t["key"], tags=["Other", "Tags"])
                    )
                )
            t = ts[2]
            with self.assertRaisesRegex(ValueError, "episode_evidence_missing"):
                model.validate(t, dict(self.item(t), scope="episode"))
            self.assertFalse(store.accept(c, batch, t, self.item(t)))
            selected = bridge.select(t, "fallback", "https://example.org/unique")
            self.assertEqual(selected[0], "ai")
            lines = selected[2].splitlines()
            self.assertEqual(
                [lines[0], lines[2], lines[5]], ["https://example.org/unique", "", ""]
            )
            self.assertIn("Episode 3", lines[1])
            self.assertTrue(lines[1].startswith("👆 watch full now"))
            self.assertEqual(len(lines), 7)

    def test_existing_unique_index_migrates_and_history_allows_reuse(self):
        with store.connection() as c:
            c.execute("DROP INDEX jobs_exact")
            c.execute("CREATE UNIQUE INDEX jobs_exact ON jobs(product,content,lang,hash) WHERE hash IS NOT NULL")
            first = self.task(1)
            store.enqueue(c, first)
            c.commit()
            batch, _ = store.claim(c)
            item = self.item(first)
            store.accept(c, batch, first, item)
            old = dict(c.execute("SELECT * FROM jobs WHERE key=?", (first["key"],)).fetchone())
            c.execute("INSERT INTO history VALUES(?,?,?,?,?,?)", (*model.group_key(first), old["hash"], old["plot"], 1))
        store.initialize()
        store.initialize()
        with store.connection() as c:
            self.assertEqual(old, dict(c.execute("SELECT * FROM jobs WHERE key=?", (first["key"],)).fetchone()))
            for i in (2, 3):
                task = self.task(i)
                store.enqueue(c, task)
                c.commit()
                batch, _ = store.claim(c)
                repeated = dict(item, id=task["key"])
                if i == 3:
                    repeated["turn"] += "s"
                worker.generate_batch(c, batch, [task], lambda rows, emit, usage: emit(repeated))
                self.assertEqual(bridge.select(task, "legacy", "https://example.org/post")[0], "ai")
            self.assertEqual(c.execute("SELECT count(*) FROM jobs WHERE state='ready'").fetchone()[0], 3)
            self.assertEqual(c.execute("SELECT count(*) FROM alerts").fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT count(*) FROM jobs WHERE hash=?", (old["hash"],)).fetchone()[0], 2)
            store.enqueue(c, first)
            self.assertEqual(c.execute("SELECT count(*) FROM jobs").fetchone()[0], 3)

    def test_manual_frozen_and_correct_product_link(self):
        snap = {
            "language": "zh-tw",
            "story_copy_manual": True,
            "material": {
                "drama_name": "劇",
                "drama_description": "棋子失蹤",
                "language": "zh-tw",
            },
        }
        with store.connection(self.fb) as src:
            src.execute(
                "INSERT INTO fb_video_task VALUES('ab','运营正文','https://ai.yingliangads.com/m/abcdefgh','target','story',?,'5','manual')",
                (store.dump(snap),),
            )
            row = src.execute("SELECT * FROM fb_video_task").fetchone()
            bridge.fb_final(src, "fv", row)
            self.assertEqual(
                tuple(
                    src.execute(
                        "SELECT source,final_text FROM story_copy_receipt"
                    ).fetchone()
                ),
                ("manual", "运营正文"),
            )
        t = self.task(5)
        item = self.item(t)
        current = model.render(
            t, model.validate(t, item), "https://ai.yingliangads.com/m/abcdefgh"
        )
        self.assertEqual(
            current.splitlines()[0], "https://ai.yingliangads.com/m/abcdefgh"
        )
        with self.assertRaises(ValueError):
            model.render(
                t,
                model.validate(t, dict(item, opening="中" * 200)),
                "https://example.org/one",
            )

    def test_fb_added_link_failure_restores_legacy_before_graph(self):
        with store.connection(self.fb) as c:
            c.execute("DROP TABLE fb_auto_task")
            c.execute(
                "CREATE TABLE fb_auto_task(id INTEGER, status TEXT,lease_owner TEXT,message_text TEXT,short_url TEXT,long_url TEXT,unknown_outcome INTEGER,graph_post_id TEXT,selection_json TEXT)"
            )
            config = store.dump(
                {"story_copy": {"legacy_before_extra_link": "original Page text"}}
            )
            c.execute(
                "INSERT INTO fb_auto_task VALUES(2,'running','worker','AI text','https://example.org/fb','target',0,'',?)",
                (config,),
            )
            c.execute(
                "INSERT INTO story_copy_receipt VALUES('fb:auto:2','fb','ai','','AI text','https://example.org/fb',9,1)"
            )
            task = dict(c.execute("SELECT * FROM fb_auto_task").fetchone())

        class Owner:
            def connect(inner):
                return store.connection(self.fb)

        self.assertTrue(bridge.restore_fb_optional_link(Owner(), task))
        self.assertEqual(task["message_text"], "original Page text")
        self.assertEqual(task["short_url"], "")
        with store.connection(self.fb) as c:
            self.assertEqual(
                tuple(
                    c.execute(
                        "SELECT source, reason FROM story_copy_receipt"
                    ).fetchone()
                ),
                ("legacy", "short_link_unavailable"),
            )
        self.assertFalse(
            bridge.restore_fb_optional_link(Owner(), dict(task, selection_json="{}"))
        )

    def test_output_stream_and_alert_retry(self):
        rows = []
        stream = generator.Lines(rows.append)
        stream.feed('{"id":"1"}\n{"i')
        stream.feed('d":"2"}\n{"id":')
        stream.finish()
        self.assertEqual([x["id"] for x in rows], ["1", "2"])
        with store.connection() as c:
            alerts.add(c, "e1", [worker.alert_item(self.task(1), "quota_unavailable")])
            c.commit()

            def offline(*args):
                raise OSError()

            alerts.deliver(c, offline)
            self.assertIsNone(c.execute("SELECT sent FROM alerts").fetchone()[0])
            c.execute("UPDATE alerts SET next_try=0")
            alerts.deliver(c, lambda *_: "receipt1")
            alerts.add(c, "e2", [worker.alert_item(self.task(2), "quota_unavailable")])
            c.commit()
            alerts.deliver(c, lambda *_: self.fail("duplicate incident"))
            self.assertEqual(
                c.execute("SELECT count(*) FROM alerts WHERE sent IS NULL").fetchone()[
                    0
                ],
                1,
            )

    def test_ten_languages_rendering(self):
        samples = json.loads(
            (ROOT / "tests/fixtures/story-copy-reviewed.json").read_text(
                encoding="utf-8"
            )
        )
        plots = []
        hooks = [
            "看誰敢動他的學生",
            "to see why Selene walks",
            "para ver a quién elige",
            "para ver a magia de Selena",
            "und entdecke Miras Pakt",
            "帰還の条件を探ろう",
            "기사의 정체를 확인해 봐",
            ", by poznać wybór smoka",
            " a odhal Bjornův původ",
            "dan lihat Gabe",
        ]
        for i, s in enumerate(samples):
            t = dict(self.task(i + 1), language=s["lang"])
            item = dict(
                self.item(t),
                opening=s["opening"],
                turn=s["turn"],
                hooks=[hooks[i]],
                tags=s["tags"].split(),
            )
            draft = model.validate(t, item)
            text = model.render(t, draft, s["url"])
            self.assertEqual(text.splitlines()[0], s["url"])
            self.assertTrue(
                text.splitlines()[1].startswith(
                    "👆 " + model.click_prefix(s["lang"]).rstrip()
                )
            )
            self.assertEqual(text.splitlines()[-1], s["tags"])
            self.assertLessEqual(model.weight(text), 280)
            plot = model.normalized(s["opening"] + s["turn"])
            plots.append(plot)
        self.assertEqual(10, len(plots))

    def test_hooks_across_all_endpoints_and_batches(self):
        link = "https://example.org/abc"
        with store.connection() as c:
            for i, f in enumerate(("fa", "xm", "xd", "ta"), 1):
                t = self.task(i, f)
                store.enqueue(c, t)
                c.commit()
                b, _ = store.claim(c)
                item = self.item(t)
                item["hooks"] = ["to find the seal", "to learn who hid it " + str(i)]
                store.accept(c, b, t, item)
                lines = bridge.select(t, "legacy", link)[2].splitlines()
                self.assertEqual(lines[0], link)
                self.assertTrue(lines[1].startswith("👆 watch full now"))
                self.assertEqual(lines[1].count("👆"), 1)
                self.assertEqual("Episode 3" in lines[1], f == "xd")
            self.assertEqual(
                1,
                c.execute(
                    "SELECT count(DISTINCT json_extract(draft,'$.hook_key')) FROM jobs"
                ).fetchone()[0],
            )
            t = self.task(5, "fa")
            store.enqueue(c, t)
            c.commit()
            b, _ = store.claim(c)
            repeated = dict(
                self.item(t), hooks=["to find the seal!", "TO FIND THE SEAL"]
            )
            worker.generate_batch(
                c, b, [t], lambda chunk, emit, usage: emit(repeated)
            )
            selected = bridge.select(t, "original", link)
            self.assertEqual(selected[:2], ("ai", ""))
            self.assertIn("👆 watch full now", selected[2])
            self.assertIn("to find the seal!", selected[2])
        t = dict(self.task(20), language="zh-Hant-TW")
        candidates = ["👆 點連結看他反擊", "立即觀看完整版", "看她反擊"]
        self.assertEqual(
            [x["cta"] for x in model.click_options(t, candidates)],
            ["👆 立即觀看完整版，看她反擊"],
        )
        self.assertEqual(model.click_prefix("zh-SG"), "立即观看完整版")
        self.assertEqual(model.click_prefix("en-US"), "watch full now")
        self.assertEqual(model.click_prefix("jp"), "今すぐ全編を視聴")
        for hooks in (None, [], ["watch full now"], "not an array"):
            item = dict(self.item(self.task(30)), hooks=hooks)
            with self.assertRaisesRegex(ValueError, "invalid_click_hook"):
                model.validate(self.task(30), item)

    def test_upgrade_only_unfrozen_draft(self):
        t = self.task(1, "fa")
        with store.connection() as c:
            store.enqueue(c, t)
            old = store.dump(
                dict(
                    cta="See more",
                    opening="A ring.",
                    turn="Who hid it?",
                    icon="◇",
                    tags=["Ring", "Drama"],
                )
            )
            c.execute("UPDATE jobs SET draft=?,state='ready'", (old,))
            c.commit()
            opts = model.click_options(t, ["to find the ring"])
            with store.connection(self.fb) as src:
                src.execute(
                    "INSERT INTO story_copy_receipt VALUES(?,?,?,?,?,?,?,?)",
                    (
                        t["key"],
                        "fb",
                        "ai",
                        "",
                        "frozen",
                        "https://example.org/old",
                        1,
                        1,
                    ),
                )
            self.assertFalse(refresh_click_hooks.upgrade(c, t, old, opts))
            with store.connection(self.fb) as src:
                src.execute("DELETE FROM story_copy_receipt")
            self.assertTrue(refresh_click_hooks.upgrade(c, t, old, opts))
            self.assertFalse(refresh_click_hooks.upgrade(c, t, old, opts))
            after = json.loads(c.execute("SELECT draft FROM jobs").fetchone()[0])
            self.assertEqual(after["previous_cta"], "See more")
            self.assertEqual(after["cta"], "👆 watch full now to find the ring")
            self.assertEqual(after["opening"], "A ring.")

    def test_busy_publisher_only_defers_that_hook_and_reuses_generation(self):
        tasks = [self.task(1, "fa"), self.task(2, "fa")]
        old = store.dump(
            dict(
                cta="Watch",
                opening="A ring.",
                turn="Who hid it?",
                icon="◇",
                tags=["Drama", "Ring"],
            )
        )
        with store.connection() as c:
            for t in tasks:
                store.enqueue(c, t)
            c.execute("UPDATE jobs SET state='ready',draft=?", (old,))
        audit = self.root / "hooks.sqlite3"
        refresh_click_hooks.prepare_audit(audit)
        apply = refresh_click_hooks.upgrade

        def busy_once(c, t, raw, opts, reason=None):
            if t["key"] == tasks[0]["key"]:
                raise sqlite3.OperationalError("database is locked")
            return apply(c, t, raw, opts, reason)

        def provider(instructions, request, emit, usage):
            for t in tasks:
                emit(dict(id=t["key"], hooks=["to find the ring", "to see who hid it"]))
            usage.update(inputTokens=90, outputTokens=33)

        with patch.object(generator, "run_request", side_effect=provider) as gen:
            with patch.object(refresh_click_hooks, "upgrade", side_effect=busy_once):
                self.assertEqual(
                    refresh_click_hooks.refresh(
                        [(t, old) for t in tasks], audit, False
                    ),
                    {tasks[0]["key"]},
                )
            self.assertFalse(
                refresh_click_hooks.refresh([(tasks[0], old)], audit, True)
            )
            self.assertEqual(gen.call_count, 1)
        with store.connection() as c:
            self.assertEqual(
                c.execute(
                    "select count(distinct json_extract(draft,'$.hook_key')) from jobs"
                ).fetchone()[0],
                1,
            )

    def test_reviewed_episode_and_switch_watermark(self):
        with store.connection() as c:
            t = self.task(8, "xd")
            evidence.save(
                c,
                dict(
                    product=t["product"],
                    content=t["content"],
                    language=t["language"],
                    material_id=t["material_id"],
                    episode=3,
                    source_ref="editorial/test/3",
                    summary="She discovers the seal.",
                ),
            )
            store.enqueue(c, t)
            frozen = json.loads(
                c.execute(
                    "SELECT payload FROM jobs WHERE key=?", (t["key"],)
                ).fetchone()[0]
            )
            self.assertIn("episode", frozen["facts"])
            self.assertEqual(
                model.validate(
                    frozen, dict(self.item(t), scope="episode", refs=["episode"])
                )["refs"],
                ["episode"],
            )
        with store.connection(self.fb) as src:
            src.execute("INSERT INTO x_post_queue VALUES(10)")
        store.change_setting("x_drama", False, "fixture")
        with store.connection() as c:
            self.assertTrue(store.eligible(c, "xd", 8))
            self.assertFalse(store.eligible(c, "xd", 11))
        nav = [{"items": [{"key": "xTagAutoPublishTemplates", "module": "x_accounts"}]}]
        self.assertTrue(
            settings.access(
                {"permissions": {"x_accounts": True}}, nav, settings.EDIT["x_tag_auto"]
            )
        )
        self.assertFalse(
            settings.access({"permissions": {}}, nav, settings.EDIT["x_tag_auto"])
        )

    def test_source_x_native_final_offline_and_tag_identity(self):
        with store.connection(self.fb) as c:
            c.execute("DROP TABLE x_post_queue")
            c.execute(
                "CREATE TABLE x_post_queue(id INTEGER,source_type TEXT,manual_run_id INTEGER,content_id TEXT,account_drama_language TEXT,material_language TEXT,drama_name TEXT,description TEXT,material_id TEXT,body_template TEXT)"
            )
            c.execute(
                "CREATE TABLE x_post_manual_run(id INTEGER,trigger_source TEXT,external_task_key TEXT,template_version INT,copy_contract_version INT)"
            )
            c.execute(
                "INSERT INTO x_post_manual_run VALUES(1,'auto_template','x-auto-task-35',7,1)"
            )
            c.execute(
                "INSERT INTO x_post_queue VALUES(3,'material',1,'story','ja','ja','TITLE','FACT','55','{{url}}')"
            )
            row = dict(c.execute("SELECT * FROM x_post_queue").fetchone())
            self.assertEqual(sources.x_task(c, row, True)["key"], "tag:auto:35")
            self.assertIsNone(sources.x_task(c, row, False))
            c.execute(
                "UPDATE x_post_manual_run SET trigger_source='manual',external_task_key=''"
            )
            self.assertEqual(sources.x_task(c, row, True)["endpoint"], "x_material")
            self.assertEqual(
                bridge.x_final(
                    c,
                    {"queue_id": 3, "long_url": "", "short_url": ""},
                    "原稿",
                    "https://example.org/3",
                    self.fb,
                ),
                "原稿",
            )
            self.assertEqual(
                c.execute("SELECT source FROM story_copy_receipt").fetchone()[0],
                "legacy",
            )


if __name__ == "__main__":
    unittest.main()
