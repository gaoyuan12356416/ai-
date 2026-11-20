import hashlib
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest

from features.ai_video_editing import catalog_imports, render_worker, studio, youtube_regular
from features.ai_video_editing.catalog import source_key
from features.ai_video_editing.runtime import Fault, Runtime, owner, packed, probe
from features.ai_video_editing.youtube_subtitles import parse_srt


TEST_ENCODER = os.environ.get("YOUTUBE_REGULAR_TEST_ENCODER", "libx264")


def ffmpeg(*args):
    return subprocess.check_output(["ffmpeg", "-v", "error", "-nostdin", "-y", *map(str, args)], stderr=subprocess.PIPE)


def input_video(root, name, size, color, frequency, *, captions=False, seconds=2):
    target = root / (name + ".mp4")
    args = ["-f", "lavfi", "-i", f"color=c={color}:s={size}:r=30:d={seconds}",
            "-f", "lavfi", "-i", f"sine=frequency={frequency}:sample_rate=48000:d={seconds}"]
    if captions:
        srt = root / (name + ".srt")
        srt.write_text("1\n00:00:00,250 --> 00:00:01,500\nOriginal words\n", encoding="utf8")
        args.extend(["-i", str(srt), "-map", "0:v", "-map", "1:a", "-map", "2:s",
                     "-c:s", "mov_text", "-metadata:s:s:0", "language=eng"])
    ffmpeg(*args, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", target)
    return target


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "requires FFmpeg media tools")
class OrdinaryVideo(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        self.rt = Runtime(self.base / "private")
        studio.setup(self.rt)
        catalog_imports.setup(self.rt)
        self.actor = {"tenant_key": "t", "user_id": "editor"}
        self.sources = []

    def asset(self, name, file, episode, *, catalog=False):
        digest = hashlib.sha256(file.read_bytes()).hexdigest()
        a = self.rt.import_asset(file, digest, name, "Demo", self.actor, episode)
        if catalog:
            key = source_key(["123", "DramaWave", "US", "en"])
            with self.rt.db(True) as db:
                db.execute("INSERT INTO catalog_imports(id,owner,product,source_key,episode,status,asset_id,title,created,updated) "
                           "VALUES(?,?,'dramawave',?,?,'ready',?,'Demo · 第1集',1,1)",
                           ("ci" + a["id"], owner(self.actor), key, episode, a["id"]))
        self.sources.append({"source_id": a["id"], "title": name, "series_code": "Demo", "duration_ms": a["duration_ms"],
                             "candidates": [{"id": name, "tag": "hook", "score": 0.9, "reason": "evidence",
                                             "start_ms": 400, "end_ms": 600, "anchor_ms": 500}]})
        return a

    def run_render(self, task):
        job = self.rt.claim()
        self.assertEqual(task["id"], job["id"])
        files = {c["source_id"]: Path(self.rt.asset(c["source_id"], self.actor)["path"]) for c in job["unit"]["clips"]}
        folder = self.base / task["id"]
        output = folder / "output.mp4"
        receipt = render_worker.render(job["unit"], files, output, folder, encoder=TEST_ENCODER)
        self.rt.update_job(job["id"], job["lease"], "complete", output, receipt["sha256"])
        self.assertTrue(self.rt.completion_receipt(job["id"], job["lease"], receipt["sha256"]))
        return self.rt.root / "outputs" / (job["id"] + ".mp4")

    def test_hook_episode_eight_render_refine_original_sound_and_captions(self):
        colors = ["red", "green", "blue", "yellow", "cyan"]
        episodes = [self.asset(str(i), input_video(self.base, "ep" + str(i), "320x180" if i != 2 else "180x320",
                                                color, 210 + i * 50), i, catalog=True)
                    for i, color in enumerate(colors, 1)]
        hook = self.asset("8", input_video(self.base, "ep8", "180x320", "white", 880, captions=True, seconds=5),
                          8, catalog=True)
        body = [a["id"] for a in episodes]
        profile = {"body_source_ids": body, "hook_source_ids": [hook["id"]]}
        units, _ = studio.prepare(self.rt, self.actor, {"analysis_id": "sa_demo", "sources": self.sources},
                                  {"youtube_regular": profile})
        u = units[0]
        self.assertEqual([c["source_id"] for c in u["clips"]], [hook["id"], *body])
        self.assertLess(u["clips"][0]["end_ms"], hook["duration_ms"])
        self.assertEqual(u["youtube_regular"]["canvas"], [320, 180])
        self.assertEqual(u["youtube_regular"]["drama"]["content_id"], "123")
        with self.assertRaises(Fault):
            self.rt.save(self.actor, {"name": "spoofed", "units": units, "operation_id": "forged"})
        project = self.rt.save(self.actor, {"name": "Demo", "units": units, "operation_id": "initial"}, regular=True)["item"]
        original = self.rt.render(project["id"], self.actor, {"expected_version": 1, "operation_id": "first"})["items"][0]
        original_file = self.run_render(original)
        digest = hashlib.sha256(original_file.read_bytes()).hexdigest()
        data = youtube_regular.timeline(self.rt, self.actor, original["id"])
        self.assertEqual(data["task"]["output"]["metadata"]["width"], 320)
        edits = [{"id": c["id"], "start_ms": c["start_ms"], "end_ms": c["end_ms"]} for c in data["clips"]]
        edits[0]["end_ms"] += 2000
        edits[2], edits[3] = edits[3], edits[2]
        edits.pop()
        args = {"operation_id": "save-refinement", "clips": edits}
        revised = youtube_regular.refine(self.rt, self.actor, original["id"], args)["item"]
        self.assertEqual(revised, youtube_regular.refine(self.rt, self.actor, original["id"], args)["item"])
        self.assertEqual((revised["version"], revised["parent_task_id"], revised["original_task_id"]),
                         (2, original["id"], original["id"]))
        with self.assertRaises(Fault):
            youtube_regular.refine(self.rt, {"tenant_key": "other", "user_id": "editor"}, original["id"], args)
        new_file = self.run_render(revised)
        self.assertEqual(hashlib.sha256(original_file.read_bytes()).hexdigest(), digest)
        self.assertEqual([c["source_id"] for c in youtube_regular.timeline(self.rt, self.actor, revised["id"])["clips"]],
                         [hook["id"], body[0], body[2], body[1], body[3]])
        meta = probe(new_file)
        self.assertEqual((meta["width"], meta["height"], meta["has_audio"]), (320, 180, True))
        soft = parse_srt(ffmpeg("-i", new_file, "-map", "0:s:0", "-f", "srt", "-"))
        self.assertEqual(soft, [(250, 1500, "Original words")])
        hook_seconds = (render_worker.frame(edits[0]["end_ms"]) - render_worker.frame(edits[0]["start_ms"])) / 30
        def pixel(seconds, x):
            return list(ffmpeg("-ss", str(seconds), "-i", new_file, "-frames:v", "1", "-an", "-sn",
                               "-vf", "format=rgb24,crop=1:1:%d:90" % x, "-f", "rawvideo", "-"))
        self.assertTrue(all(v < 10 for v in pixel(.2, 4)), "portrait opener should have side padding")
        self.assertTrue(all(v > 235 for v in pixel(.2, 160)), "opener should show episode 8")
        for index, channel in enumerate([0, 2, 1]):  # revised body is red, blue, green
            rgb = pixel(hook_seconds + index * 2 + .5, 160)
            self.assertGreater(rgb[channel], max(v for k, v in enumerate(rgb) if k != channel) + 70)
        sample = ffmpeg("-ss", "0.8", "-i", new_file, "-t", "0.2", "-vn", "-sn", "-ac", "1", "-ar", "48000", "-f", "s16le", "-")
        values = struct.unpack("<%dh" % (len(sample) // 2), sample)
        upward = sum(a <= 0 < b for a, b in zip(values, values[1:]))
        self.assertAlmostEqual(upward / .2, 880, delta=20)

    def test_rotation_and_pixel_aspect_from_first_positive_source(self):
        path = input_video(self.base, "upright", "320x180", "red", 440)
        rotated = self.base / "rotated.mp4"
        try:
            ffmpeg("-display_rotation", "90", "-i", path, "-map", "0", "-c", "copy", rotated)
        except subprocess.CalledProcessError:
            # Older deployment FFmpeg writes the same MOV display matrix via stream metadata.
            ffmpeg("-i", path, "-map", "0", "-c", "copy", "-metadata:s:v:0", "rotate=90", rotated)
        a = self.asset("rotated", rotated, 1)
        unit = studio.prepare(self.rt, self.actor, {"sources": self.sources, "analysis_id": "sa_rot"},
                              {"youtube_regular":{"body_source_ids":[a["id"]], "hook_source_ids":[]}})[0][0]
        self.assertEqual(unit["youtube_regular"]["canvas"], [180, 320])
        folder = self.base / "rotate_render"
        out = folder / "out.mp4"
        render_worker.render(unit, {a["id"]:Path(a["path"])}, out, folder, encoder=TEST_ENCODER)
        self.assertEqual((probe(out)["display_width"], probe(out)["display_height"]), (180,320))

    def test_non_square_pixels_and_mismatched_drama(self):
        a = self.asset("first", input_video(self.base, "first", "100x100", "red", 300), 1, catalog=True)
        path = self.base / "sar.mp4"
        ffmpeg("-f", "lavfi", "-i", "color=s=100x100:d=1", "-vf", "setsar=2", "-c:v", "libx264", path)
        b = self.asset("second", path, 2, catalog=True)
        self.assertEqual((b["display_width"], b["display_height"]), (200, 100))
        with self.rt.db(True) as db:
            db.execute("UPDATE catalog_imports SET source_key=? WHERE asset_id=?",
                       (source_key(["other","DramaWave","US","en"]),b["id"]))
        with self.assertRaisesRegex(Fault, "同一部剧"):
            youtube_regular.catalog_identity(self.rt, self.actor, [a["id"],b["id"]])
        self.assertEqual(youtube_regular.output_canvas(2160,3840), [1080,1920])

    def test_silent_variable_rate_source_and_fractional_reordered_cuts(self):
        source = self.base / "vfr.mp4"
        ffmpeg("-f", "lavfi", "-i", "testsrc2=s=196x288:d=4:r=15", "-vf",
               "select='not(mod(n,3))+lt(t,2)'", "-vsync", "vfr", "-c:v", "mpeg4", source)
        folder = self.base / "vfr_result"
        unit = {"youtube_regular": {"canvas": [196,288]}, "clips": [
            {"source_id":"a","start_ms":1550,"end_ms":2910}, {"source_id":"a","start_ms":28,"end_ms":1050}]}
        receipt = render_worker.render(unit, {"a":source}, folder/"out.mp4", folder, encoder=TEST_ENCODER)
        frames = sum(render_worker.frame(c["end_ms"]) - render_worker.frame(c["start_ms"]) for c in unit["clips"])
        self.assertEqual(receipt["frames"], frames)
        self.assertAlmostEqual(receipt["duration_ms"], frames * 1000 / 30, delta=1)


if __name__ == "__main__":
    unittest.main()
