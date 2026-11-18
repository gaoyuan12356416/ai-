import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import shutil
import subprocess
import wave
from unittest.mock import patch

from features.ai_video_editing.runtime import Runtime, Fault, packed
from features.ai_video_editing import studio
from features.ai_video_editing.composition import normalize, write_ass


class StudioTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.rt = Runtime(Path(self.temp.name) / "private")
        studio.setup(self.rt)
        self.actor = {"tenant_key": "t", "user_id": "u"}
        self.other = {"tenant_key": "other", "user_id": "u"}
        self.asset = "aa_" + "1" * 32
        from features.ai_video_editing.runtime import owner

        with self.rt.db(True) as db:
            db.execute(
                "INSERT INTO assets VALUES(?,?,?,?,?,?,?,?)",
                (
                    self.asset,
                    owner(self.actor),
                    "test.mp4",
                    "Test",
                    1,
                    "a" * 64,
                    packed(
                        {
                            "duration_ms": 10000,
                            "width": 720,
                            "height": 1280,
                            "has_audio": True,
                        }
                    ),
                    1,
                ),
            )
        self.sid = self.call("POST", "/sessions", {"source_ids": [self.asset]})["item"][
            "id"
        ]

    def tearDown(self):
        self.temp.cleanup()

    def call(self, method, path, body=None, actor=None):
        return studio.dispatch(
            self.rt, actor or self.actor, method, path, body or {}, {}
        )

    def test_session_owner_and_version(self):
        with self.assertRaises(Fault):
            self.call("GET", "/sessions/" + self.sid, actor=self.other)
        self.call("PATCH", "/sessions/" + self.sid, {"version": 1, "title": "new"})
        with self.assertRaises(Fault):
            self.call("PATCH", "/sessions/" + self.sid, {"version": 1, "title": "late"})
        self.assertEqual(
            self.call("GET", "/sessions/" + self.sid)["item"]["title"], "new"
        )

    def test_submission_receipt_freezes_config_and_owner(self):
        body = {
            "operation_id": "click1",
            "config": {
                "clip": {
                    "method": "golden_three",
                    "script_count": 1,
                    "variant_count": 2,
                }
            },
        }
        a = self.call("POST", "/sessions/" + self.sid + "/submit", body)
        self.assertEqual(
            a, self.call("POST", "/sessions/" + self.sid + "/submit", body)
        )
        body["config"]["clip"]["variant_count"] = 3
        with self.assertRaises(Fault):
            self.call("POST", "/sessions/" + self.sid + "/submit", body)
        item = self.call("GET", "/sessions/" + self.sid)["item"]
        self.assertEqual(len(item["runs"]), 1)
        self.assertEqual(len(item["messages"]), 1)
        self.assertEqual(item["runs"][0]["config"]["clip"]["variant_count"], 2)

    def test_message_receipt_and_citations_persist(self):
        with patch(
            "features.ai_video_editing.questions.answer",
            return_value={
                "answer": "A key",
                "evidence": [{"source_id": self.asset, "start_ms": 0, "end_ms": 2000}],
            },
        ) as answer:
            body = {"text": "What is in the box?", "operation_id": "once"}
            first = self.call("POST", "/sessions/" + self.sid + "/messages", body)
            self.assertEqual(
                first, self.call("POST", "/sessions/" + self.sid + "/messages", body)
            )
            self.assertEqual(answer.call_count, 1)
        self.assertEqual(
            len(self.call("GET", "/sessions/" + self.sid)["item"]["messages"]), 2
        )

    def test_resume_prepared_project_does_not_regenerate(self):
        result = self.call(
            "POST",
            "/sessions/" + self.sid + "/submit",
            {"operation_id": "create", "config": {"clip": {"method": "hook_first"}}},
        )
        rid = result["run_id"]
        self.rt.save(
            self.actor,
            {
                "name": "ready",
                "operation_id": rid,
                "units": [
                    {
                        "id": "one",
                        "clips": [
                            {"source_id": self.asset, "start_ms": 0, "end_ms": 1000}
                        ],
                    }
                ],
            },
        )
        with self.rt.db(True) as db:
            db.execute("UPDATE studio_runs SET status='preparing' WHERE id=?", (rid,))
        with patch.object(
            studio,
            "prepare",
            side_effect=AssertionError("must recover existing project"),
        ):
            studio.tick(self.rt)
        run = self.call("GET", "/sessions/" + self.sid)["item"]["runs"][0]
        self.assertEqual(run["status"], "rendering")
        self.assertEqual(len(run["tasks"]), 1)

    def test_subtitle_text_cannot_inject_styles(self):
        c = normalize(
            {
                "watermark": "my mark",
                "cues": [
                    {"start": 0, "end": 999, "text": r"{\pos(0,0)}<tag>" + "\nhello"}
                ],
            },
            1000,
        )
        path = Path(self.temp.name) / "captions.ass"
        write_ass(c, path, 1000)
        text = path.read_text(encoding="utf8")
        self.assertNotIn(r"{\pos", text)
        self.assertIn(r"\Nhello", text)
        with self.assertRaises(Fault):
            normalize({"cues": [{"start": 0, "end": 2000, "text": "bad"}]}, 1000)

    def test_caption_translation_preserves_original_narration_voice(self):
        source = {
            "source_id": self.asset,
            "title": "test",
            "duration_ms": 10000,
            "candidates": [],
        }
        plan = {
            "drafts": [
                {
                    "units": [
                        {
                            "id": "one",
                            "clips": [
                                {
                                    "source_id": self.asset,
                                    "start_ms": 0,
                                    "end_ms": 10000,
                                }
                            ],
                        }
                    ]
                }
            ],
            "warnings": [],
        }
        config = {
            "merge": True,
            "narration": {
                "text": "你好。",
                "language": "zh",
                "seconds": 10,
                "speech": True,
                "style": {},
            },
            "translation": {
                "source_language": "zh",
                "language": "en",
                "speech": False,
                "mode": "bilingual",
                "style": {},
            },
        }
        with (
            patch.object(studio, "build_plans", return_value=plan),
            patch(
                "features.ai_video_editing.language_tools.generate",
                return_value={"cues": [{"start": 0, "end": 10000, "text": "Hello."}]},
            ),
            patch(
                "features.ai_video_editing.language_tools.synthesize",
                return_value={"audio_base64": "audio"},
            ) as synth,
            patch.object(studio, "store_audio", return_value="ag_" + "3" * 32),
        ):
            units, _ = studio.prepare(
                self.rt,
                self.actor,
                {"analysis_id": "test", "sources": [source]},
                config,
            )
        self.assertEqual(synth.call_args[0][0]["language"], "zh")
        self.assertEqual(synth.call_args[0][0]["cues"][0]["text"], "你好。")
        self.assertEqual(units[0]["composition"]["cues"][0]["text"], "你好。\nHello.")

    def test_export_owner_and_duplicate_rejection(self):
        with self.assertRaises(Fault):
            self.call("POST", "/exports", {"task_ids": ["at_" + "2" * 32]})
        with self.assertRaises(Fault):
            self.call("POST", "/exports", {"task_ids": ["same", "same"]})

    @unittest.skipUnless(
        shutil.which("ffmpeg") and shutil.which("ffprobe"), "requires FFmpeg"
    )
    def test_real_server_composition_with_every_effect_and_voice(self):
        from features.ai_video_editing.render_worker import render

        root = Path(self.temp.name)
        source = root / "source.mp4"
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=360x640:rate=30:duration=2",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=220:sample_rate=48000:duration=2",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-c:a",
                "aac",
                str(source),
            ],
            check=True,
        )
        aid = "ag_" + "3" * 32
        wav = root / "voice.wav"
        with wave.open(str(wav), "wb") as f:
            f.setnchannels(1)
            f.setsampwidth(2)
            f.setframerate(16000)
            f.writeframes(b"\0\0" * 32000)
        unit = {
            "clips": [{"source_id": self.asset, "start_ms": 0, "end_ms": 2000}],
            "composition": {
                "watermark": "AI REVIEW",
                "effects": list(studio.EFFECTS),
                "speech_id": aid,
                "original_volume": 0.15,
                "cues": [{"start": 0, "end": 2000, "text": "A real caption\n双语字幕"}],
            },
        }
        info = render(
            unit,
            {self.asset: source, aid: wav},
            root / "result.mp4",
            root / "render",
            encoder="libx264",
        )
        self.assertEqual(info["frames"], 60)
        self.assertAlmostEqual(info["duration_ms"], 2000, delta=40)


if __name__ == "__main__":
    unittest.main()
