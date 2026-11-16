"""Offline acceptance of the same decisions called by the native owner paths."""

import json
import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from features.story_copy import alerts, bridge, generator, model, sources, store, worker
from features.story_copy import evidence, settings

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
            cta="Watch now",
            opening=sentence(),
            turn=sentence(),
            tags=["ShortDrama", "Mystery"],
        )

    def test_shared_synopsis_assigns_distinct_plot_beats(self):
        tasks = [self.task(1), self.task(2, "fa")]
        for t in tasks:
            t["facts"] = {"series": "Ada hides the seal. Ben finds the ribbon."}
        request = json.loads(generator.prompt(tasks, []))
        self.assertEqual(len(request["fact_text"]), 1)
        self.assertEqual([x["focus"]["sentence"] for x in request["tasks"]], [1, 2])

    def test_100_and_101_partial_failure(self):
        with store.connection() as c:
            for i in range(1, 102):
                store.enqueue(c, self.task(i))
            c.commit()
            batch, rows = store.claim(c)
            self.assertEqual(100, len(rows))
            calls = []

            def partial(part, prior, emit, usage):
                calls.append(len(part))
                usage.update(inputTokens=1500, outputTokens=19000)
                for t in part[:97]:
                    emit(self.item(t))
                raise RuntimeError("generation_timeout")

            worker.generate_batch(c, batch, rows, partial, seed=False)
            self.assertEqual(calls, [100])
            self.assertEqual(
                c.execute("SELECT count(*) FROM jobs WHERE state='ready'").fetchone()[
                    0
                ],
                97,
            )
            self.assertEqual(
                c.execute("SELECT count(*) FROM jobs WHERE state='failed'").fetchone()[
                    0
                ],
                3,
            )
            sent = []
            alerts.deliver(c, lambda event, body: sent.append(body) or "receipt")
            self.assertEqual(len(sent), 1)
            self.assertIn("影响 3 条", sent[0])
            self.assertEqual(len(store.claim(c)[1]), 1)
            for t in rows[97:]:
                self.assertEqual(
                    bridge.select(t, "原正文", "https://example.org/task")[2], "原正文"
                )

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
            pieces = list(generator.splits([self.task(i) for i in range(100)], []))
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

    def test_same_drama_across_channels_and_story_only_episode(self):
        with store.connection() as c:
            ts = [self.task(i + 1, f) for i, f in enumerate(["fa", "xm", "xd", "ta"])]
            for t in ts:
                store.enqueue(c, t)
            c.commit()
            batch, rows = store.claim(c)
            sample = self.item(ts[0])
            self.assertTrue(store.accept(c, batch, ts[0], sample))
            for t in ts[1:]:
                with self.assertRaisesRegex(ValueError, "duplicate"):
                    store.accept(
                        c, batch, t, dict(sample, id=t["key"], tags=["Other", "Tags"])
                    )
            t = ts[2]
            with self.assertRaisesRegex(ValueError, "episode_evidence_missing"):
                model.validate(t, dict(self.item(t), scope="episode"))
            self.assertTrue(store.accept(c, batch, t, self.item(t)))
            selected = bridge.select(t, "fallback", "https://example.org/unique")
            self.assertEqual(selected[0], "ai")
            lines = selected[2].splitlines()
            self.assertEqual(
                [lines[0], lines[2], lines[5]], ["https://example.org/unique", "", ""]
            )
            self.assertIn("Episode 3", lines[1])
            self.assertEqual(len(lines), 7)

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

    def test_ten_languages_and_dedup_calibration(self):
        samples = json.loads(
            (ROOT / "tests/fixtures/story-copy-reviewed.json").read_text(
                encoding="utf-8"
            )
        )
        plots = []
        for i, s in enumerate(samples):
            t = dict(self.task(i + 1), language=s["lang"])
            item = dict(
                self.item(t),
                opening=s["opening"],
                turn=s["turn"],
                cta=s["cta"],
                tags=s["tags"].split(),
            )
            draft = model.validate(t, item)
            text = model.render(t, draft, s["url"])
            self.assertEqual(text.splitlines()[0], s["url"])
            self.assertEqual(text.splitlines()[-1], s["tags"])
            self.assertLessEqual(model.weight(text), 280)
            plot = model.normalized(s["opening"] + s["turn"])
            self.assertTrue(model.near(plot, plot[:-1] + "a"))
            self.assertFalse(any(model.near(plot, old) for old in plots))
            plots.append(plot)
        self.assertEqual(10, len(plots))

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
