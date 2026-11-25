import tempfile
import unittest
import time
from unittest.mock import patch
from types import SimpleNamespace
from pathlib import Path

from features.tt_auto_posts.core import TTPostAutoStore
from features.tt_promotion import pub_copy
from features.tt_promotion.publishing import Publisher, PublicationError, initialize
from features.tt_promotion.pub_store import connection, TASK_BASE


class NativeFixture:
    def __init__(self, path):
        self.store = TTPostAutoStore(path)
        self.blocked = set()
        self.input_count = 0

    def settings(self, acct):
        return {"privacy_level": "PUBLIC_TO_EVERYONE", "allow_comment": False}, 1

    def conflicts(self, account, at):
        return ["旧模板占用"] if account in self.blocked else []

    def preview(self, t, ch, opts, seed, blocked):
        self.input_count += 1
        ids = opts.get("material_ids") or ["51", "52", "53"]
        mid = opts.get("material_id") or next(i for i in ids if i not in blocked)
        return dict(
            content_id=opts.get("content_id") or "cid",
            material_id=mid,
            drama_name="Moon",
            description="Episode opening",
            reason="preferred_match",
            profile_version="one",
        )

    def step(self):
        return {"claimed": False}


class PublisherTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / "new.sqlite3")
        self.runtime = NativeFixture(self.path)
        self.p = Publisher(self.path, self.runtime)
        self.actor = dict(user_id="author", name="Operator", tenant_key="one", role="user")
        self.channels = [
            dict(account_id=i, enabled=True, eligible=True, drama_language="en") for i in ("12", "13")
        ]
        self.data = dict(
            name="test",
            account_ids=["12", "13"],
            caption_template="{desc} {code}",
            schedule=dict(mode="fixed", times=["01:00", "22:10"]),
            model="income",
            ai_copy=False,
        )
        self.t = self.call("POST", "/templates", self.data)
        self.base = "/templates/" + str(self.t["id"])

    def call(self, method, path, payload=None, channels=None, actor=None):
        return self.p.handle(
            method,
            path,
            payload or {},
            actor or self.actor,
            self.channels if channels is None else channels,
        )

    def rows(self, sql):
        with connection(self.path) as db:
            return [dict(r) for r in db.execute(sql)]

    def test_preview_has_no_reservation_copy_or_shortlink(self):
        self.runtime.blocked.add("13")
        rows = self.call("POST", self.base + "/preview", dict(expected_version=1))["items"]
        self.assertEqual([r["status"] for r in rows], ["eligible", "skipped"])
        self.assertTrue(rows[1]["conflicts"])
        self.assertIn("〔正式四位码〕", rows[1]["caption"])
        self.assertFalse(self.rows("select * from ttp_slot"))
        self.assertFalse(self.rows("select * from ttp_copy"))

    def test_draft_and_scope_and_version(self):
        data = dict(expected_version=1, operation_id="draft-key", account_id="12", material_id="51")
        draft = self.call("POST", self.base + "/draft", data)
        self.assertEqual(draft["state"], "waiting")
        self.assertTrue(pub_copy.process_one(self.path, lambda _: "New text {code}", table="ttp_draft"))
        got = self.call("GET", self.base + "/draft/" + draft["id"])
        self.assertEqual((got["source"], got["caption"]), ("ai", "New text {code}"))
        self.assertFalse(self.rows("select * from ttp_slot"))
        with self.assertRaises(PublicationError):
            self.call("GET", self.base, channels=self.channels[:1])
        with self.assertRaises(PublicationError):
            self.call("GET", self.base, actor=dict(self.actor, tenant_key="else"))
        self.assertEqual(self.call("POST", self.base, dict(self.data, expected_version=1))["version"], 2)
        with self.assertRaises(PublicationError):
            self.call("POST", self.base + "/run", dict(expected_version=1, operation_id="operation-id"))

    def test_one_receipt_and_independent_report_contract(self):
        args = dict(
            expected_version=1,
            account_ids=["12"],
            manual_caption="My text {code}",
            operation_id="operation-11",
        )
        job = self.call("POST", self.base + "/run", args)
        self.assertEqual(job["id"], self.call("POST", self.base + "/run", args)["id"])
        for _ in range(3):
            self.p.work(lambda actor: self.channels)
        tasks = self.rows("SELECT * FROM tt_promotion_report_task")
        self.assertEqual(len(tasks), 1)
        self.assertGreater(tasks[0]["id"], TASK_BASE)
        self.assertEqual(tasks[0]["template_creator_user_id"], "author")
        self.assertEqual(tasks[0]["template_tenant"], "one")
        self.assertEqual(tasks[0]["route_queue_id"], 7_000_000_000_000_000_000 + tasks[0]["id"])
        self.assertEqual(self.rows("SELECT * FROM tt_promotion_report_plan")[0]["task_id"], tasks[0]["id"])
        self.assertEqual(self.rows("SELECT source FROM ttp_copy")[0]["source"], "manual")
        with self.assertRaises(PublicationError):
            self.call("POST", self.base + "/run", dict(args, manual_caption="Different"))

    def test_conflict_is_local_and_no_double_reservation(self):
        self.runtime.blocked.add("12")
        self.call("POST", self.base + "/run", dict(expected_version=1, operation_id="with-conflict"))
        self.p.work(lambda actor: self.channels)
        states = self.rows("select account_id,state,reason from ttp_slot order by account_id")
        self.assertEqual([r["state"] for r in states], ["skipped", "reserved"])
        self.assertFalse(self.rows("SELECT * FROM tt_auto_task"))

    def test_ai_failure_is_single_retry_and_fallback(self):
        self.call("POST", self.base, dict(self.data, expected_version=1, ai_copy=True))
        self.call(
            "POST",
            self.base + "/run",
            dict(expected_version=2, operation_id="ai-automatic", account_ids=["12"]),
        )
        self.p.work(lambda actor: self.channels)

        def fail(facts):
            raise ValueError("bad")

        pub_copy.process_one(self.path, fail)
        with connection(self.path, write=True) as d:
            d.execute("update ttp_copy set next_at=''")
        pub_copy.process_one(self.path, fail)
        self.assertFalse(pub_copy.process_one(self.path, fail))
        self.assertEqual(
            self.rows("select state,attempts,source from ttp_copy"),
            [dict(state="final", attempts=2, source="template")],
        )

    def test_scheduler_reentry_and_draft_default(self):
        self.p.schedule()
        self.assertFalse(self.rows("select * from ttp_job"))
        self.call("POST", self.base + "/enable", dict(expected_version=1))
        self.p.schedule()
        count = len(self.rows("select * from tt_promotion_report_plan"))
        self.p.schedule()
        self.assertEqual(len(self.rows("select * from tt_promotion_report_plan")), count)
        self.call("POST", self.base + "/disable", dict(expected_version=1))
        for r in self.rows("select slot_state from tt_promotion_report_plan"):
            self.assertEqual(r["slot_state"], "skipped")
        self.assertEqual(
            self.rows("select seq from sqlite_sequence where name='tt_auto_task'")[0]["seq"], TASK_BASE
        )
        initialize(self.path)

    def test_day_preview_is_read_only_and_pages_account_slots(self):
        data = dict(expected_version=1, plan_date="2026-12-10", offset=1, limit=1)
        first = self.call("POST", self.base + "/preview", data)
        self.assertEqual(first["total"], 4)
        self.assertEqual(len(first["items"]), 1)
        self.assertEqual(first, self.call("POST", self.base + "/preview", data))
        self.assertFalse(self.rows("SELECT * FROM ttp_day"))
        self.assertFalse(self.rows("SELECT * FROM ttp_job"))

    def test_other_template_receipt_never_advances_series(self):
        self.call(
            "POST",
            self.base + "/run",
            dict(
                expected_version=1,
                account_ids=["13"],
                mode="material",
                material_id="51",
                operation_id="old-operation",
            ),
        )
        self.p.work(lambda actor: self.channels)
        with connection(self.path, write=True) as db:
            db.execute("INSERT INTO ttp_series VALUES('one','cid',?,'12','[\"51\",\"52\"]')", (self.t["id"],))
            series = db.execute("SELECT * FROM ttp_series").fetchone()
            with self.assertRaisesRegex(PublicationError, "其他任务占用"):
                self.p._series_state(db, series, ["51", "52"])

    def test_gpu_nested_public_id_is_bound_to_original_creator_and_receipt(self):
        from types import SimpleNamespace
        from features.tt_promotion.pub_native import ReceiptGPU

        task = SimpleNamespace(
            id=4000000000001,
            account_id="12",
            publish_id="v_pub_a",
            account_settings={"privacy_level": "PUBLIC_TO_EVERYONE"},
        )
        response = dict(
            job_id="tt-a",
            publish_id="v_pub_a",
            state="published",
            status={"post_ids": ["7280000111222333444"]},
        )
        delegate = SimpleNamespace(
            creator_info=lambda **k: dict(
                job_id="tt-a", source_account_id="12", creator_info={"creator_username": "test.user"}
            ),
            reconcile=lambda **k: dict(response),
        )
        gpu = ReceiptGPU(delegate, self.path, task)
        params = dict(job_id="tt-a", source_account_id="12", access_token="fixture")
        gpu.creator_info(**params)
        self.assertEqual(
            gpu.reconcile(**params)["publish_url"],
            "https://www.tiktok.com/@test.user/video/7280000111222333444",
        )
        self.assertEqual(self.rows("select publish_id from ttp_public_receipt")[0]["publish_id"], "v_pub_a")
        task.publish_id = "different"
        self.assertNotIn("publish_url", gpu.reconcile(**params))
        task.publish_id = "v_pub_a"
        task.account_settings["privacy_level"] = "SELF_ONLY"
        self.assertNotIn("publish_url", gpu.reconcile(**params))

    def test_paused_channel_does_not_inherit_healthy_authorization_reason(self):
        self.channels[0].update(enabled=False, reason="已通过")
        rows = self.call("POST", self.base + "/preview", {"expected_version": 1})["items"]
        self.assertEqual(rows[0]["reason"], "频道已在新版暂停参与")
        self.assertEqual(rows[1]["status"], "eligible")

    def test_pause_after_reservation_stops_the_gpu_write(self):
        from types import SimpleNamespace
        from features.tt_promotion.channels import initialize as init_channels
        from features.tt_promotion.pub_native import ReceiptGPU
        from features.tt_auto_posts.publisher import AutoPostExecutionError

        init_channels(self.path)
        self.call(
            "POST",
            self.base + "/run",
            dict(expected_version=1, operation_id="pause-after-queue", account_ids=["12"]),
        )
        for _ in range(2):
            self.p.work(lambda a: self.channels)
        task = self.runtime.store.get_task(self.rows("select id from tt_auto_task")[0]["id"])
        calls = []
        wrapped = ReceiptGPU(SimpleNamespace(publish=lambda **kw: calls.append(kw)), self.path, task)
        with connection(self.path, write=True) as d:
            d.execute(
                "insert into promotion_channel_setting(tenant,account_id,enabled,updated_by,updated_at) values('one','12',0,'test','now')"
            )
        with self.assertRaisesRegex(AutoPostExecutionError, "频道已在新版暂停参与"):
            wrapped.publish()
        self.assertEqual(calls, [])
        self.assertEqual(len(self.rows("select * from ttp_slot")), 1)

    def test_invalid_targets_and_caption(self):
        with self.assertRaises(PublicationError):
            self.call("POST", "/templates", dict(self.data, account_ids=["99"]))
        with self.assertRaises(PublicationError):
            self.call("POST", "/templates", dict(self.data, caption_template="{wrong}"))
        with self.assertRaises(PublicationError):
            self.call(
                "POST",
                self.base + "/preview",
                dict(expected_version=1, mode="serial", content_id="x", episodes=["2", "2"]),
            )
        with self.assertRaises(PublicationError):
            self.call("GET", "/runs/not-a-number")

    def ready(self, key, **args):
        for _ in range(200):
            data = self.call("POST", self.base + "/preview", dict(expected_version=1, preview_id=key, **args))
            if data["state"] == "ready":
                return data
            self.assertNotEqual(data["state"], "failed", data.get("error"))
            time.sleep(0.025)
        self.fail("preview did not finish")

    def test_52_slots_across_pages_share_occupancy_cooldown_and_beijing_seed(self):
        accounts = [str(i) for i in range(15, 28)]
        self.channels = [dict(self.channels[0], account_id=i) for i in accounts]
        self.t = self.call(
            "POST",
            "/templates",
            dict(
                self.data,
                account_ids=accounts,
                cooldown_days=7,
                schedule=dict(mode="fixed", times=["00:10", "09:00", "17:00", "23:10"]),
            ),
        )
        self.base = "/templates/" + str(self.t["id"])

        def pick(t, ch, opts, seed, blocked):
            series = next(i for i in range(4) if str(i) not in opts.get("cooling", []))
            material = next(
                str(1000 + series * 100 + n) for n in range(30) if str(1000 + series * 100 + n) not in blocked
            )
            return dict(content_id=str(series), material_id=material, drama_name="Show", reason="rules")

        self.runtime.preview = pick
        start = self.call(
            "POST",
            self.base + "/preview",
            dict(async_preview=True, plan_date="2027-01-20", expected_version=1, limit=1),
        )
        key = start["preview_id"]
        self.p = Publisher(self.path, self.runtime)
        result = self.ready(key, limit=24)
        self.assertEqual(result["summary"], dict(eligible=52, skipped=0, pending=0))
        all_rows = (
            result["items"]
            + self.ready(key, offset=24, limit=24)["items"]
            + self.ready(key, offset=48, limit=8)["items"]
        )
        self.assertEqual(len({r["material_id"] for r in all_rows}), 52)
        for acct in accounts:
            self.assertEqual(len({r["content_id"] for r in all_rows if r["account_id"] == acct}), 4)
        self.assertTrue(all(r["seed"].endswith(":2027-01-20") for r in all_rows))
        self.assertEqual(all_rows[7], self.ready(key, offset=7, limit=1)["items"][0])
        self.assertEqual(self.rows("SELECT * FROM ttp_slot"), [])

    def test_native_rules_and_resource_union_filter_metrics_before_loading(self):
        from scripts import test_tt_auto_post_selector as mocks
        from features.tt_promotion.pub_native import Runtime
        from features.tt_promotion.publishing import options

        source = mocks.FakeSource(
            [
                mocks.drama("C1"),
                mocks.drama("C2", series_code="Dw_70-A"),
                mocks.drama("C3", series_code="DW_70-A"),
                mocks.drama("C4", series_code="Dw_70-A", app_id=1480),
            ],
            {f"C{i}": [mocks.material(str(i * 100 + n), f"C{i}") for n in [1, 2]] for i in range(1, 4)},
        )
        source.schema = "catalog"

        day = mocks.complete_beijing_dates(mocks.NOW, 7)[-1]
        metrics = mocks.FakeMetricStore(
            [
                mocks.metric_row(day, f"C{i}", 100 * i + n, i * 20 + n, i * 10)
                for i in range(1, 4)
                for n in [1, 2]
            ]
        )
        rt = Runtime.__new__(Runtime)
        rt.store = self.runtime.store
        rt.native = SimpleNamespace(
            selector=mocks.selector(source, metrics), legacy_reader=mocks.FakeLegacyReader(), store=rt.store
        )
        template = self.call(
            "POST",
            "/templates",
            dict(
                self.data,
                model="rules",
                drama_launch_window_days=0,
                material_window_days=0,
                drama_rule=dict(sort_by="spend", sort_direction="desc", roas_min=10),
                material_rule=dict(sort_by="spend", sort_direction="asc", duration_min_seconds=20),
            ),
        )
        t = rt.store.get_template(template["id"])
        args = options(dict(mode="drama", content_ids=["C1"], resource_ids=["Dw_70-A"]))
        args["_catalog_at"] = mocks.NOW.isoformat()
        with patch("features.tt_promotion.pub_native.old_databases", return_value=(self.path, self.path)):
            session = rt.preview_session()
            first = session.preview(t, self.channels[0], args, "seed", ())
            second = session.preview(t, self.channels[1], args, "seed", ("201",))
        self.assertEqual((first["material_id"], second["material_id"]), ("201", "202"))
        self.assertEqual(first["material_metrics"]["spend"], "41")
        self.assertEqual([r[3] for r in metrics.loads if r[0] == "rows"], [("C1", "C2")])
        self.assertEqual(source.material_calls, [("C2", "en")])
        with patch("features.tt_promotion.pub_native.old_databases", return_value=(self.path, self.path)):
            for missing in ({"resource_ids": ["Dw_70-a"]}, {"content_ids": ["c2"]}):
                with self.assertRaises(mocks.NoEligibleMaterial):
                    session.preview(
                        t,
                        self.channels[0],
                        {**missing, "mode": "drama", "_catalog_at": mocks.NOW.isoformat()},
                        "s",
                        (),
                    )
        with self.assertRaises(PublicationError):
            options(dict(mode="drama", resource_ids=["00"]))
        with self.assertRaises(PublicationError):
            self.call("POST", "/templates", dict(self.data, drama_rule=dict(spend_min=100, spend_max=90)))

    def test_batch_draft_adoption_scope_and_frozen_provenance(self):
        preview = self.call("POST", self.base + "/preview", dict(expected_version=1))
        key = preview["preview_id"]
        args = dict(
            expected_version=1,
            preview_id=key,
            operation_id="draft-batch",
            items=[dict(account_id=r["account_id"], material_id=r["material_id"]) for r in preview["items"]],
        )
        with self.assertRaises(PublicationError):
            self.call("POST", self.base + "/drafts", args, actor=dict(self.actor, user_id="colleague"))
        drafts = self.call("POST", self.base + "/drafts", args)["items"]
        for _ in drafts:
            pub_copy.process_one(self.path, lambda _: "AI {code}", table="ttp_draft")
        items = [
            dict(draft_id=d["id"], account_id=d["account_id"], material_id=d["material_id"]) for d in drafts
        ]
        items[0]["edited_caption"] = "Revised {code}"
        args = dict(expected_version=1, operation_id="reviewed-drafts", preview_id=key, drafts=items)
        with self.assertRaises(PublicationError):
            self.call("POST", self.base + "/run", dict(args, drafts=[dict(items[0], material_id="500")]))
        job = self.call("POST", self.base + "/run", args)
        self.p.work(lambda a: self.channels)
        self.assertEqual(
            self.rows("select source,caption_body from ttp_copy order by rowid"),
            [
                dict(source="manual", caption_body="Revised {code}"),
                dict(source="ai", caption_body="AI {code}"),
            ],
        )
        detail = self.call("GET", "/runs/" + str(job["id"]))
        self.assertEqual([r["preview_comparison"]["status"] for r in detail["items"]], ["matched", "matched"])
        self.assertEqual(detail["items"][0]["copy_provenance"]["draft_id"], drafts[0]["id"])
        self.assertEqual(self.call("POST", self.base + "/run", args)["id"], job["id"])

    def test_recheck_and_missing_receipt_bucket(self):
        preview = self.call("POST", self.base + "/preview", dict(expected_version=1, account_ids=["12"]))
        job = self.call(
            "POST",
            self.base + "/run",
            dict(
                expected_version=1,
                operation_id="changed-item",
                account_ids=["12"],
                preview_id=preview["preview_id"],
            ),
        )
        fn = self.runtime.preview
        self.runtime.preview = lambda *args, **kw: dict(fn(*args, **kw), material_id="99")
        for _ in range(2):
            self.p.work(lambda a: self.channels)
        url = "/runs/" + str(job["id"])
        self.assertEqual(self.call("GET", url)["items"][0]["preview_comparison"]["status"], "changed")
        for status, unknown, publish, expected in [
            ("retry_wait", 1, "", "review"),
            ("published", 0, "", "review"),
            ("published", 0, "real-receipt", "confirmed"),
        ]:
            with connection(self.path, write=True) as db:
                db.execute(
                    "UPDATE tt_auto_task SET status=?,unknown_outcome=?,publish_id=?",
                    (status, unknown, publish),
                )
            result = self.call("GET", url)
            self.assertEqual(result["items"][0]["result_bucket"], expected)
            self.assertEqual(result["status"], "needs_review" if expected == "review" else "published")

    def test_daily_draft_reuse_checks_story_facts_and_manual_has_priority(self):
        args = dict(expected_version=1, account_ids=["12"])
        day = self.call("POST", self.base + "/preview", dict(args, plan_date="2027-01-20"))
        orig = day["items"][0]
        drafts = self.call(
            "POST",
            self.base + "/drafts",
            dict(
                args,
                operation_id="day-draft-reuse",
                preview_id=day["preview_id"],
                items=[dict(account_id="12", material_id=orig["material_id"])],
            ),
        )
        d = drafts["items"][0]
        pub_copy.process_one(self.path, lambda _: "AI {code}", table="ttp_draft")
        adopt = [dict(draft_id=d["id"], account_id="12", material_id=orig["material_id"])]
        fn = self.runtime.preview
        self.runtime.preview = lambda *a, **kw: dict(fn(*a, **kw), description="Different episode facts")
        bad = self.call("POST", self.base + "/preview", args)
        self.assertNotEqual(bad["items"][0]["copy_facts_key"], orig["copy_facts_key"])
        with self.assertRaises(PublicationError):
            self.call(
                "POST",
                self.base + "/run",
                dict(args, operation_id="wrong-facts", preview_id=bad["preview_id"], drafts=adopt),
            )
        self.runtime.preview = fn
        good = self.call("POST", self.base + "/preview", args)
        self.call(
            "POST",
            self.base + "/run",
            dict(
                args,
                operation_id="right-facts",
                preview_id=good["preview_id"],
                manual_caption="Human final",
                drafts=adopt,
            ),
        )
        self.p.work(lambda a: self.channels)
        self.assertEqual(
            self.rows("SELECT source,caption_body FROM ttp_copy"),
            [dict(source="manual", caption_body="Human final")],
        )

    def test_async_conflicts_appear_before_cold_eligible_selection(self):
        import threading

        self.runtime.blocked.add("13")
        entered, release = threading.Event(), threading.Event()
        fn = self.runtime.preview

        def slow(*a, **kw):
            entered.set()
            release.wait(3)
            return fn(*a, **kw)

        self.runtime.preview = slow
        key = self.call("POST", self.base + "/preview", dict(expected_version=1, async_preview=True))[
            "preview_id"
        ]
        try:
            self.assertTrue(entered.wait(2))
            peek = self.call("POST", self.base + "/preview", dict(expected_version=1, preview_id=key))
            self.assertEqual([r["status"] for r in peek["items"]], ["pending", "skipped"])
            self.assertTrue(peek["items"][1]["conflicts"])
        finally:
            release.set()
        ready = self.ready(key)
        self.assertNotIn("material_id", ready["items"][1])
        full = self.ready(key, include_conflict_candidates=True)
        self.assertEqual(full["items"][1]["status"], "skipped")
        self.assertIn("material_id", full["items"][1])


if __name__ == "__main__":
    unittest.main()
