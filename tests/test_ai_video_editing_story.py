import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from features.ai_video_editing import smart_api, studio, story_planner, story_receipts
from features.ai_video_editing.runtime import Fault, Runtime, owner, packed
from features.ai_video_editing.story_edges import snap
from features.ai_video_editing import story_cut_review


class StoryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.rt = Runtime(Path(self.temp.name) / "private")
        studio.setup(self.rt)
        self.actor = {"tenant_key": "qa", "user_id": "a"}
        self.sources = []
        for n in range(1, 21):
            sid = "aa_" + ("%032x" % n)
            sha = "%064x" % n
            with self.rt.db(True) as db:
                db.execute(
                    "INSERT INTO assets VALUES(?,?,?,?,?,?,?,?)",
                    (
                        sid,
                        owner(self.actor),
                        f"Source {n}",
                        "One",
                        n,
                        sha,
                        packed({"duration_ms": 20000, "width": 720, "height": 1280, "has_audio": False}),
                        1,
                    ),
                )
            source = smart_api.snapshot(self.rt.asset(sid, self.actor))
            source.update(
                engine="multimodal",
                candidates=[
                    {
                        "id": f"e{i}",
                        "tag": tag,
                        "engine": "multimodal",
                        "start_ms": t,
                        "end_ms": t + 3100,
                        "anchor_ms": t + 1400,
                        "category": tag,
                        "reason": f"actual action {n}/{i}",
                        "transcript": f"Line {n}/{i}",
                    }
                    for i, (t, tag) in enumerate(
                        [(600, "conflict"), (5000, "turn"), (10500, "emotion"), (16000, "cliffhanger")]
                    )
                ],
            )
            self.sources.append(source)
        self.edges = patch.object(
            story_planner,
            "boundaries",
            return_value={"cuts": [], "captions": [], "quiet": [], "cues": [], "signatures": []},
        )
        self.measure = self.edges.start()
        self.addCleanup(self.edges.stop)
        lock = patch.object(story_planner, "model_config", return_value=("", "", ""))
        lock.start()
        self.addCleanup(lock.stop)

    def call(self, verb, path, body=None, actor=None):
        return studio.dispatch(self.rt, actor or self.actor, verb, path, body or {}, {})

    def mode(self, n):
        with self.rt.db(True) as db:
            db.execute("UPDATE studio_story_defaults SET version=? WHERE singleton=1", (n,))

    def make(self, sources=None):
        src = list(reversed(self.sources[:3])) if sources is None else sources
        analysis = smart_api.assemble(self.rt, self.actor, src)
        s = self.call("POST", "/sessions", {"source_ids": [a["source_id"] for a in src]})["item"]
        frozen = {
            "config": s["config"],
            "source_ids": s["source_ids"],
            "selection_revision": s["selection_revision"],
        }
        return s, frozen, analysis

    def test_default_frozen_preview_and_render_with_idempotent_submission(self):
        old, _, _ = self.make()
        self.mode(2)
        s, frozen, analysis = self.make()
        sid = s["id"]
        self.assertNotIn("clip", old["config"])
        plan = story_receipts.ensure(self.rt, self.actor, sid, frozen, analysis)
        output = plan["outputs"][0]
        self.assertEqual([c["episode"] for c in output["coverage"]], [1, 2, 3])
        self.assertEqual(plan["selection_order"], s["source_ids"])
        self.assertTrue(all(c["segments"] for c in output["coverage"]))
        self.assertEqual(self.call("GET", "/sessions/" + sid)["item"]["edit_plan"]["id"], plan["id"])
        measured = self.measure.call_count
        body = {"edit_plan_id": plan["id"], "operation_id": "one-click"}
        first = self.call("POST", "/sessions/" + sid + "/submit", body)
        self.assertEqual(first, self.call("POST", "/sessions/" + sid + "/submit", body))
        with self.rt.db(True) as db:
            db.execute(
                "UPDATE studio_runs SET status='preparing',state=? WHERE id=?",
                (packed({"analysis_id": analysis["analysis_id"]}), first["run_id"]),
            )
        self.assertTrue(studio.tick(self.rt))
        with self.rt.db() as db:
            unit = json.loads(db.execute("SELECT unit_data FROM tasks").fetchone()[0])
        self.assertEqual(
            [(c["source_id"], c["start_ms"], c["end_ms"]) for c in unit["clips"]],
            [(c["source_id"], c["start_ms"], c["end_ms"]) for c in output["segments"]],
        )
        self.assertEqual(self.measure.call_count, measured)
        with self.assertRaises(Fault):
            self.call(
                "GET",
                "/sessions/" + sid + "/edit-plans/" + plan["id"],
                actor={"tenant_key": "other", "user_id": "a"},
            )
        self.mode(1)
        self.assertEqual(
            self.call("GET", "/sessions/" + sid)["item"]["config"]["clip"]["strategy_version"], 2
        )
        self.assertNotIn("clip", self.make()[0]["config"])

    def test_new_revision_never_mutates_the_previous_one(self):
        self.mode(2)
        s, frozen, analysis = self.make()
        sid = s["id"]
        p1 = story_receipts.ensure(self.rt, self.actor, sid, frozen, analysis)
        frozen["config"]["clip"].update(story_order="selection", ending_take=1)
        p2 = story_receipts.ensure(self.rt, self.actor, sid, frozen, analysis)
        self.assertNotEqual(p1["id"], p2["id"])
        self.assertEqual([c["episode"] for c in p2["outputs"][0]["coverage"]], [3, 2, 1])
        self.assertEqual(story_receipts.read(self.rt, self.actor, sid, p1["id"])["outputs"], p1["outputs"])
        with self.assertRaisesRegex(Fault, "方案已更新"):
            self.call(
                "POST",
                "/sessions/" + sid + "/submit",
                {"config": frozen["config"], "edit_plan_id": p1["id"], "operation_id": "stale"},
            )
        other = self.make()[0]["id"]
        with self.assertRaises(Fault):
            story_receipts.read(self.rt, self.actor, other, p2["id"])

    def test_twenty_sources_too_short_no_partial_task_and_no_silent_omission(self):
        self.mode(2)
        s, frozen, analysis = self.make(self.sources)
        frozen["config"]["clip"]["seconds"] = 2
        p = story_receipts.ensure(self.rt, self.actor, s["id"], frozen, analysis)
        self.assertEqual(len(p["outputs"][0]["coverage"]), 20)
        self.assertTrue(p["conflicts"])
        self.assertIn("20", p["conflicts"][0])
        rid = self.call(
            "POST",
            "/sessions/" + s["id"] + "/submit",
            {"operation_id": "limited", "config": frozen["config"]},
        )["run_id"]
        with self.rt.db(True) as db:
            db.execute(
                "UPDATE studio_runs SET status='preparing',state=? WHERE id=?",
                (packed({"analysis_id": analysis["analysis_id"]}), rid),
            )
        studio.tick(self.rt)
        with self.rt.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM tasks").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT status FROM studio_runs").fetchone()[0], "plan_attention")

    def test_reorder_and_append_reuse_measured_original_windows(self):
        self.edges.stop()
        self.mode(2)
        s, frozen, analysis = self.make()
        measured = {"cuts": [], "captions": [], "quiet": [], "cues": [], "signatures": []}
        with (
            patch("features.ai_video_editing.story_edges.metrics", return_value=measured) as decode,
            patch("features.ai_video_editing.language_media.probe", return_value=(20, [])),
        ):
            story_receipts.ensure(self.rt, self.actor, s["id"], frozen, analysis)
            first = decode.call_count
            self.assertGreater(first, 0)
            frozen["config"]["clip"]["story_order"] = "selection"
            story_receipts.ensure(self.rt, self.actor, s["id"], frozen, analysis)
            self.assertEqual(decode.call_count, first)
            new, f2, a2 = self.make(list(reversed(self.sources[:4])))
            story_receipts.ensure(self.rt, self.actor, new["id"], f2, a2)
            names = {c.args[0].stem for c in decode.call_args_list[first:]}
            self.assertEqual(names, {self.sources[3]["source_id"]})

    def test_grouped_outputs_use_idempotent_project_batches(self):
        self.mode(2)
        s, frozen, analysis = self.make(self.sources[:4])
        frozen["config"]["merge"] = False
        frozen["config"]["clip"].update(script_count=2, variant_count=3)
        rid = self.call(
            "POST", "/sessions/" + s["id"] + "/submit", {"config": frozen["config"], "operation_id": "many"}
        )["run_id"]
        for attempt in range(2):
            with self.rt.db(True) as db:
                db.execute(
                    "UPDATE studio_runs SET status='preparing',expires=0,state=? WHERE id=?",
                    (packed({"analysis_id": analysis["analysis_id"]}), rid),
                )
            studio.tick(self.rt)
            with self.rt.db() as db:
                self.assertEqual(db.execute("SELECT count(*) FROM projects").fetchone()[0], 2)
                self.assertEqual(db.execute("SELECT count(*) FROM tasks").fetchone()[0], 24)

    def test_dialogue_finishes_and_suspense_stops_before_any_visible_payoff(self):
        # The subtitle can change in the last two frames at a boundary.
        grid = story_cut_review.sample_times(8850, 11550, 96600)
        self.assertIn(11515, grid)
        times = list(range(87000, 96000, 600))
        evidence = {"cuts": [93400], "cues": []}
        review = story_cut_review.decision(
            {"after_line": 12, "first_answer": 5, "cut_reason": "来人伸手"}, times, 87000, 91350, evidence
        )
        self.assertEqual(review["dialogue_end"], 93400)
        self.assertEqual(review["suspense_end"], 88800)
        self.assertLess(review["suspense_end"], times[4])
        self.assertEqual(
            story_cut_review.decision({"after_line": 29, "first_answer": -1}, times, 87000, 91350, evidence),
            {"dialogue_end": 91350},
        )

    def test_visual_outgoing_review_reuses_other_episodes_when_selection_changes(self):
        self.measure.side_effect = lambda *a: {
            "cuts": [],
            "captions": [],
            "quiet": [],
            "cues": [],
            "signatures": [],
        }
        self.mode(2)
        s, frozen, analysis = self.make()

        def decisions(prompt, images, folder):
            return {
                "windows": [
                    {"id": str(i + 1), "first_answer": 3, "after_line": 0} for i in range(len(images))
                ]
            }

        with (
            patch.object(story_cut_review, "model_config", return_value=("fixture", "", "")),
            patch.object(story_cut_review, "contact_sheet"),
            patch.object(story_cut_review, "model_json", side_effect=decisions) as model,
        ):
            plan = story_receipts.ensure(self.rt, self.actor, s["id"], frozen, analysis)
            first = model.call_count
            self.assertEqual(first, 3)
            self.assertLess(plan["outputs"][0]["segments"][-1]["end_ms"], 19100)
            frozen["config"]["clip"]["story_order"] = "selection"
            story_receipts.ensure(self.rt, self.actor, s["id"], frozen, analysis)
            self.assertEqual(model.call_count, first)
            ns, nf, na = self.make(list(reversed(self.sources[:4])))
            story_receipts.ensure(self.rt, self.actor, ns["id"], nf, na)
            self.assertEqual(model.call_count, first + 1)

    def test_uncertain_order_short_silent_source_and_title_boundary(self):
        self.mode(2)
        s, frozen, _ = self.make()
        rows = story_planner.resolve_sources(self.rt, self.actor, list(reversed(self.sources[:3])))
        rows[0]["identity"] = None
        self.assertEqual(story_planner.ordered(rows, "episode"), (rows, "selection"))
        only = dict(
            rows[0],
            duration_ms=480,
            candidates=[
                dict(
                    rows[0]["candidates"][0],
                    start_ms=0,
                    end_ms=480,
                    anchor_ms=200,
                    transcript="",
                    tag="signal",
                )
            ],
        )
        plan = story_planner.assemble(self.rt, self.actor, [only], frozen["config"], "unit")
        self.assertEqual(plan["outputs"][0]["duration_ms"], 480)
        self.assertTrue(plan["warnings"])
        item = dict(
            only["candidates"][0], start_ms=0, end_ms=4000, anchor_ms=2600, reason="片头海报切到人物对话"
        )
        data = {"cuts": [300, 3700], "captions": [], "quiet": [], "cues": [], "signatures": []}
        self.assertEqual(snap(item, data, 14000)[:2], (300, 3700))
        self.assertIsNone(snap(item, {**data, "cuts": []}, 14000))
