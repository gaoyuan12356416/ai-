"""Signed-window eligibility, exact planned release and rechecks on the original TT task."""

from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from features.tt_auto_posts.publisher import AutoPostExecutor
from features.tt_auto_posts.selector import ResolverMaterialValidator
from features.tt_promotion import pub_launch
from features.tt_promotion.pub_native import Runtime, promotion_executor
from features.tt_promotion.pub_store import connection, iso, now
from features.tt_promotion.publishing import Publisher
from scripts import test_tt_auto_post_selector as fixtures
from test_tt_promotion_publishing import NativeFixture


class ReleaseTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = str(Path(directory.name) / "promotion.sqlite3")
        self.runtime = NativeFixture(self.path)
        self.pub = Publisher(self.path, self.runtime)
        self.actor = dict(user_id="author", tenant_key="one")
        self.channels = [dict(account_id="12", eligible=True, enabled=True, drama_language="en")]
        self.cfg = dict(
            name="Future",
            drama_launch_window_days="【-1～0】",
            model="rules",
            material_window_days=0,
            account_ids=["12"],
            schedule=dict(mode="fixed", times=["08:30"]),
        )
        self.template = self.pub.handle("POST", "/templates", self.cfg, self.actor, self.channels)
        self.base = "/templates/" + str(self.template["id"])

    def test_elapsed_bounds_include_exact_second(self):
        epoch = int(fixtures.NOW.timestamp())
        for raw, window, endpoints in [
            (0, 0, (1, epoch)),
            ("7", 7, (epoch - 7 * 86400, epoch)),
            ("【-1～0】", "-1~0", (epoch, epoch + 86400)),
            ("[-2~-1]", "-2~-1", (epoch + 86400, epoch + 2 * 86400)),
            ("1~1", "1~1", (epoch - 86400, epoch - 86400)),
            ("0~0", "0~0", (epoch, epoch)),
        ]:
            self.assertEqual(pub_launch.normalize(raw), window)
            self.assertEqual(pub_launch.bounds(window, fixtures.NOW), endpoints)
        for raw in ("-1", "0~-1", "-3651~0", "0~3651", True, "unlimited"):
            with self.assertRaises(ValueError):
                pub_launch.normalize(raw)

    def test_native_source_metrics_and_strict_future_candidate(self):
        epoch = int(fixtures.NOW.timestamp())
        # The real strict material resolver validates its mapped drama's delivery time against its clock.
        clock_calls = []

        class Resolver:
            _now_fn = staticmethod(lambda: fixtures.NOW)

            def resolve(self, material):
                clock_calls.append(int(self._now_fn().timestamp()))
                if clock_calls[-1] < epoch + 86400:
                    raise AssertionError("future material validated too early")
                return fixtures.FakeValidator().validate(material)

        src = fixtures.FakeSource(
            [
                fixtures.drama("C1", deploy_time=epoch - 1),
                fixtures.drama("C2", deploy_time=epoch + 86400),
                fixtures.drama("C3", deploy_time=epoch + 86401),
            ],
            {"C2": [fixtures.material("201", "C2")]},
        )
        metrics = fixtures.FakeMetricStore()
        validator = ResolverMaterialValidator(Resolver())
        rt = Runtime.__new__(Runtime)
        rt.store = self.pub.store
        rt.native = SimpleNamespace(
            store=rt.store,
            selector=fixtures.selector(src, metrics, validator=validator),
            legacy_reader=fixtures.FakeLegacyReader(),
        )
        template = rt.store.get_template(self.template["id"])
        with patch("features.tt_promotion.pub_native.old_databases", return_value=(self.path, self.path)):
            choice = rt.preview(
                template, self.channels[0], {"mode": "template", "_catalog_at": iso(fixtures.NOW)}, "seed", ()
            )
        self.assertEqual((choice["material_id"], choice["deploy_time"]), ("201", epoch + 86400))
        self.assertEqual(clock_calls, [epoch + 86400])
        self.assertEqual(validator.resolver._now_fn(), fixtures.NOW)
        self.assertEqual(src.drama_call["now_epoch"], epoch + 86400)
        self.assertEqual(src.drama_call["deploy_since_epoch"], epoch)
        self.assertEqual([r[3] for r in metrics.loads if r[0] == "rows"], [("C2",)])

    def test_preview_and_frozen_schedule_wait_on_same_task(self):
        anchor = now().replace(microsecond=0)
        release = anchor + timedelta(minutes=20)
        pick = self.runtime.preview
        self.runtime.preview = lambda *args: dict(pick(*args), deploy_time=int(release.timestamp()))

        def post(endpoint, **args):
            return self.pub.handle(
                "POST", self.base + endpoint, dict(expected_version=1, **args), self.actor, self.channels
            )

        preview = post("/preview")
        sample = preview["items"][0]
        self.assertEqual(sample["scheduled_at_utc"], iso(release))
        self.assertLess(sample["planned_at_utc"], sample["scheduled_at_utc"])
        with connection(self.path) as db:
            self.assertEqual(db.execute("select count(*) from ttp_slot").fetchone()[0], 0)
        later = iso(release + timedelta(minutes=10))
        self.assertEqual(post("/preview", scheduled_at=later)["items"][0]["scheduled_at_utc"], later)
        run = post("/run", operation_id="future-release", preview_id=preview["preview_id"])
        self.assertEqual(run["items"][0]["scheduled_at_utc"], iso(release))
        for _ in range(2):
            self.pub.work(lambda _: self.channels)
        rows = self.pub.run_detail(run["id"], self.actor, self.pub.channel_map(self.channels))["items"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            (rows[0]["scheduled_at_utc"], rows[0]["release_at_utc"]), (iso(release), iso(release))
        )
        task = self.pub.store.get_task(rows[0]["id"])
        self.assertEqual(
            int(datetime.fromisoformat(task.scheduled_at_utc).timestamp()), int(release.timestamp())
        )
        with connection(self.path, write=True) as db:
            db.execute(
                "update tt_auto_task set content_id='cid',material_id='51',status='publishing' where id=?",
                (task.id,),
            )
        task = self.pub.store.get_task(task.id)
        source = SimpleNamespace(
            schema="catalog",
            app_id=1479,
            _stream_select=lambda sql, params: [
                {"release_at": int((release + timedelta(hours=1)).timestamp())}
            ],
        )
        executor = promotion_executor(
            SimpleNamespace(
                executor=SimpleNamespace(store=self.pub.store, now_fn=lambda: anchor),
                selector=SimpleNamespace(source=source),
            ),
            self.path,
        )
        with (
            patch("features.tt_promotion.pub_native.conflicts", return_value=[]),
            patch.object(AutoPostExecutor, "_publish") as publish,
        ):
            updated = executor._publish(task)
            publish.assert_not_called()
            self.assertEqual(updated.id, task.id)
            self.assertEqual(updated.status, "retry_wait")
            with connection(self.path) as db:
                self.assertEqual(
                    db.execute("SELECT claim_phase FROM tt_auto_task WHERE id=?", (task.id,)).fetchone()[0],
                    "publish",
                )
                slot = db.execute("select scheduled_at from ttp_slot").fetchone()[0]
                self.assertEqual(slot, iso(release + timedelta(hours=1)))
            # Receipt-bearing tasks must reach the native reconciliation-only flow.
            evidence = SimpleNamespace(publish_id="receipt", unknown_outcome=False)
            executor._publish(evidence)
            publish.assert_called_once()

    def test_native_select_uses_projected_rules_without_altering_version(self):
        t = self.pub.store.get_template(self.template["id"])
        adjusted = {**t.config, "drama_rule": {**t.config["drama_rule"], "sort_by": "x_income"}}
        projected = pub_launch.native_config(adjusted)
        self.assertEqual(projected["drama_rule"]["sort_by"], "roas")
        self.assertEqual(adjusted["drama_rule"]["sort_by"], "x_income")
        executor = promotion_executor(
            SimpleNamespace(executor=SimpleNamespace(store=self.pub.store)), self.path
        )

        def check(phase, task, token):
            self.assertEqual(
                phase.store.get_template(t.id, version=t.version).config["drama_launch_window_days"], 0
            )
            return "original-task"

        with patch.object(AutoPostExecutor, "_select", check):
            self.assertEqual(executor._select(None, "claim"), "original-task")
        self.assertEqual(self.pub.store.get_template(t.id).config["drama_launch_window_days"], "-1~0")
