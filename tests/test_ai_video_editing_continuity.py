import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from features.ai_video_editing import smart_api, story_long, story_planner, story_receipts, studio
from features.ai_video_editing import story_scenes as scenes
from features.ai_video_editing.runtime import Fault, Runtime, owner, packed


class ContinuityTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.rt = Runtime(Path(directory.name) / "db")
        studio.setup(self.rt)
        self.actor = {"tenant_key": "t", "user_id": "u"}
        self.story = [
            {
                "source_id": s,
                "title": s,
                "episode": n,
                "identity": ("provider", "series"),
                "asset": {"sha256": s * 64, "path": directory.name + "/" + s},
                "duration_ms": 40000,
                "candidates": [{"start_ms": 2000, "end_ms": 4500, "reason": "action", "transcript": "line"}],
            }
            for n, s in enumerate(("a", "b", "c"), 1)
        ]
        self.clip = {"seconds": None, "opening_take": 0, "ending_take": 0}
        general = dict(
            place="院中",
            cause="接到请求",
            action="人物听到了消息",
            result="人物为此采取了行动",
            line="我听到了",
        )
        self.map = [
            dict(general, start=1000, end=6000, first=2, last=6, dependent=False),
            dict(general, start=6000, end=15000, first=7, last=15, dependent=True),
            dict(general, start=16000, end=25000, first=17, last=25, dependent=False),
            dict(general, start=26000, end=35000, first=27, last=35, dependent=False),
        ]
        self.raw = {
            "kind": "primary",
            "ids": ["1.2", "2.2", "3.1", "3.2"],
            "links": ["接到求助后赶回", "孩子倒下后求医", "医生解释病情"],
            "story": "家人得知消息，赶回来陪孩子就医。",
            "question": "能否治好？",
            "issue": "",
        }

    def test_reaction_bridges_the_next_location_without_adding_the_next_substory(self):
        with (
            patch.object(scenes, "index_episode", return_value=self.map),
            patch.object(scenes, "infer", return_value={"routes": [self.raw]}),
        ):
            plan = scenes.routes(self.rt, self.actor, self.story, self.clip)
        spans, notes, problems, question = scenes.cut(plan, self.story, self.clip, 0)
        self.assertEqual(
            [(c["source_id"], c["start_ms"], c["end_ms"]) for c in spans],
            [("a", 1000, 15000), ("b", 1000, 15000), ("c", 1000, 15000)],
        )
        self.assertFalse(problems)
        self.assertIn("治好", question)
        self.assertIn("求医", spans[-1]["reason"])
        self.assertTrue(notes)  # a complete short story is not filled with the unrelated last scene
        short = {**self.clip, "seconds": 28}
        self.assertIn("42 秒", scenes.cut(plan, self.story, short, 0)[2][0])

    def test_source_map_reuse_and_valid_alternates_without_changed_frozen_input(self):
        for s in self.story:
            Path(s["asset"]["path"]).write_bytes(b"x")
        mapping = {
            "scenes": [
                {"first": 1, "last": 5, "action": "收到消息"},
                {"first": 6, "last": 10, "action": "赶回"},
                {"first": 11, "last": 18, "action": "送医"},
                {"first": 19, "last": 24, "action": "另一场行动"},
            ]
        }
        primary = {**self.raw, "ids": ["1.1", "2.3", "3.2"], "links": ["得到消息", "到达现场"]}
        opposite = {**primary, "kind": "ending", "ids": ["1.1", "2.3", "3.3"]}
        directed = []

        def model(prompt, *args):
            if prompt.startswith(scenes.MAP_PROMPT):
                return mapping
            directed.append(prompt)
            return {"routes": [primary, opposite]}

        with (
            patch.object(scenes, "sheet_files", return_value=[]),
            patch.object(scenes, "shot_changes", return_value=[]),
            patch.object(scenes, "infer", side_effect=model) as infer,
        ):
            first = scenes.routes(self.rt, self.actor, self.story, self.clip)
            self.assertEqual(infer.call_count, 4)
            second = scenes.routes(self.rt, self.actor, list(reversed(self.story)), self.clip)
            self.assertEqual(infer.call_count, 4)
            self.assertEqual(first, second)
            base = scenes.cut(first, self.story, self.clip, 0)[0]
            alt = scenes.cut(second, self.story, {**self.clip, "ending_take": 1}, 0)[0]
            self.assertEqual(base[0], alt[0])
            self.assertNotEqual(base[-1]["start_ms"], alt[-1]["start_ms"])
            rev = scenes.cut(first, list(reversed(self.story)), self.clip, 0)
            self.assertEqual([s["source_id"] for s in rev[0]], ["c", "b", "a"])
            self.assertTrue(rev[1])
            self.assertEqual(len(directed), 1)

    def test_invalid_identifiers_missing_source_and_unexplained_junction(self):
        catalog = {
            f"{j}.1": {**self.map[0], "number": 1, "group": j - 1, "source": s["source_id"]}
            for j, s in enumerate(self.story, 1)
        }
        for ids, links in [
            (["1.1", "3.1"], ["jump"]),
            (["1.1", "2.1", "3.9"], ["a", "b"]),
            (["1.1", "2.1", "3.1"], ["a", ""]),
        ]:
            self.assertIsNone(
                scenes.read_route({**self.raw, "ids": ids, "links": links}, catalog, self.story)
            )
        gap = scenes.read_route(
            {**self.raw, "ids": ["1.1", "2.1", "3.1"], "links": ["A", "B"], "issue": "来信从未出现在素材中"},
            catalog,
            self.story,
        )
        self.assertIn("来信", scenes.cut(([gap], catalog), self.story, self.clip, 0)[2][0])
        rows = [{"first": 2, "last": 5, "action": "动作落定"}, {"first": 5, "last": 6, "action": "重叠"}]
        self.assertEqual(scenes.scene_bounds(rows, 8, 1000, 7900, [(133, 0.96)]), [])
        rows[1]["first"] = 6
        out = scenes.scene_bounds(rows, 8, 1000, 7900, [(133, 0.96), (4600, 0.8)])
        self.assertEqual(out[0]["start"], 133)
        self.assertEqual(out[0]["end"], out[1]["start"])

    def test_long_form_preserves_source_and_episode_order_without_calling_model(self):
        cfg = studio.configuration({"clip": {"method": "cliffhanger", "strategy_version": 3}})
        sources = [dict(s, duration_ms=240000, series_code="One") for s in reversed(self.story)]
        with patch.object(story_long, "profile", return_value=None), patch.object(scenes, "routes") as routes:
            plan = story_planner.assemble(self.rt, self.actor, sources, cfg, "analysis")
            out = plan["outputs"][0]
            self.assertEqual(plan["selection_order"], ["c", "b", "a"])
            self.assertEqual([c["episode"] for c in out["segments"]], [1, 2, 3])
            self.assertEqual(out["duration_ms"], 720000)
            self.assertEqual(plan["conflicts"], [])
            cfg["clip"]["story_order"] = "selection"
            self.assertEqual(
                [
                    c["episode"]
                    for c in story_planner.assemble(self.rt, self.actor, sources, cfg, "analysis")["outputs"][
                        0
                    ]["segments"]
                ],
                [3, 2, 1],
            )
            cfg["merge"] = False
            self.assertEqual(
                len(story_planner.assemble(self.rt, self.actor, sources, cfg, "analysis")["conflicts"]), 1
            )
            routes.assert_not_called()

    def test_render_bounds_and_requested_upper_limit(self):
        c = dict(self.clip, strategy_version=3)
        source = dict(self.story[0], duration_ms=1400000)

        def result(length):
            return ([dict(source_id="a", start_ms=0, end_ms=length)], [])

        with patch("features.ai_video_editing.smart_model.model_config", return_value=("", "", "")):
            for length in (600000, 1200000):
                self.assertFalse(story_long.edit(self.rt, self.actor, [source], c, 0, result(length))[2])
            for length in (599966, 1200034):
                self.assertTrue(story_long.edit(self.rt, self.actor, [source], c, 0, result(length))[2])
            self.assertTrue(
                story_long.edit(self.rt, self.actor, [source], dict(c, seconds=900), 0, result(901000))[2]
            )
            for seconds in (75, 599, 1201):
                with self.assertRaises(Fault):
                    studio.configuration({"clip": dict(c, method="cliffhanger", seconds=seconds)})

    def test_recurring_poster_matches_across_different_encodes_but_not_blank_openings(self):
        w, h = story_long.W, story_long.H
        pic = bytes(190 if (x // 8 + y // 7) % 2 else 55 for y in range(h) for x in range(w))
        master = bytes(min(255, x + 25) for x in pic)
        master = master[: w * 70] + bytes([75]) * (w * (h - 70))
        scene, other = bytes([85]) * len(pic), bytes([160]) * len(pic)
        own = ([pic] * 4 + [scene] * 10, [], 0)
        peer = ([master] * 4 + [other] * 10, [], 0)
        self.assertGreater(story_long.bounds(own, [peer], set(), 70000)[0], 0)
        self.assertEqual(story_long.bounds(([scene] * 4 + [other] * 10, [], 0), [peer], set(), 70000)[0], 0)

    def test_long_compression_checks_the_measured_cut_and_all_selected_sources(self):
        c = dict(self.clip, strategy_version=3)
        sources = [dict(s, duration_ms=500000) for s in self.story]
        spans = [dict(source_id=s["source_id"], start_ms=200, end_ms=498900) for s in sources]
        route = [dict(s, start_ms=0, end_ms=275000) for s in spans]
        with (
            patch("features.ai_video_editing.smart_model.model_config", return_value=("test", "", "")),
            patch.object(scenes, "routes", return_value=True),
            patch.object(scenes, "cut") as cut,
        ):
            cut.return_value = (route, [], [], "下一步")
            groups, _, problems, _ = story_long.edit(self.rt, self.actor, sources, c, 0, (spans, []))
            self.assertFalse(problems)
            self.assertEqual([r["start_ms"] for r in groups], [200] * 3)
            self.assertEqual(story_long.output_ms(groups), 824400)
            cut.return_value = (route[:-1], [], [], "下一步")
            self.assertTrue(story_long.edit(self.rt, self.actor, sources, c, 0, (spans, []))[2])

    def test_long_default_shortage_and_frozen_old_sessions(self):
        sid = "aa_" + "1" * 32
        meta = {"duration_ms": 228000, "width": 720, "height": 1280, "has_audio": True}
        with self.rt.db(True) as db:
            db.execute(
                "INSERT INTO assets VALUES(?,?,?,?,?,?,?,?)",
                (sid, owner(self.actor), "原片", "剧", 1, "2" * 64, packed(meta), 1),
            )
            db.execute("UPDATE studio_story_defaults SET version=2")

        def create():
            return studio.dispatch(self.rt, self.actor, "POST", "/sessions", {"source_ids": [sid]}, {})[
                "item"
            ]

        old = create()
        with self.rt.db(True) as db:
            db.execute("UPDATE studio_story_defaults SET version=3")
        new = create()
        self.assertEqual(new["config"]["clip"]["strategy_version"], 3)
        self.assertEqual(
            story_receipts.session_config({"clip": {"method": "cliffhanger", "seconds": 75}}, 3)["clip"][
                "strategy_version"
            ],
            2,
        )
        self.assertEqual(
            studio.session(self.rt, self.actor, old["id"])["config"]["clip"]["strategy_version"], 2
        )
        with self.assertRaisesRegex(Fault, "6 分 12 秒"):
            studio.submit(self.rt, self.actor, new["id"], {"operation_id": "short"})
        with self.rt.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)
        snap = smart_api.snapshot(self.rt.asset(sid, self.actor))
        receipt = smart_api.assemble(self.rt, self.actor, [dict(snap, engine="signal")])
        with patch.object(
            story_long, "profile", side_effect=AssertionError("must reject before media reads")
        ):
            plan = story_receipts.ensure(self.rt, self.actor, new["id"], new, receipt)
        self.assertIn("6 分 12 秒", plan["conflicts"][0])
        self.assertEqual(plan["outputs"][0]["coverage"][0]["source_id"], sid)
        self.assertEqual(plan["outputs"][0]["segments"], [])
