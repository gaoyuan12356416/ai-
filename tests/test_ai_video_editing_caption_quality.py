import subprocess
import shutil
import unittest
from unittest.mock import patch

from features.ai_video_editing import narration, speech
from features.ai_video_editing.smart_common import SmartError
from features.ai_video_editing.subtitle_protection import mirror_graph, resolve


class CaptionQualityTest(unittest.TestCase):
    def test_narration_evidence_follows_final_cut_order(self):
        source = {"source_id": "a", "title": "Story", "candidates": [
            {"engine": "multimodal", "start_ms": 0, "end_ms": 5000, "reason": "First"},
            {"engine": "multimodal", "start_ms": 8000, "end_ms": 12000, "reason": "Last"},
        ]}
        clips = [{"source_id": "a", "start_ms": 8000, "end_ms": 12000},
                 {"source_id": "a", "start_ms": 0, "end_ms": 5000}]
        sections = narration.ordered_context([source], clips, "en")
        self.assertEqual([s["events"][0]["description"] for s in sections], ["Last", "First"])
        self.assertEqual([s["start"] for s in sections], [0, 4000])
        with self.assertRaises(SmartError):
            narration.write([source], clips, {"language": "en"}, model=lambda _: {
                "segments": [{"id": 0, "text": "too many words " * 20}, {"id": 1, "text": "A pause."}]
            })

    def test_closing_quotes_remain_attached_to_spoken_sentence(self):
        result = speech.caption_groups('He whispers, "Stay here." Then leaves.', [
            {"start": i * 300, "end": i * 300 + 250, "text": word}
            for i, word in enumerate(["He", "whispers", "Stay", "here", "Then", "leaves"])
        ])
        self.assertEqual(result[0]["text"], 'He whispers, "Stay here."')
        self.assertEqual(result[1]["text"], "Then leaves.")
        self.assertEqual(result[1]["start"], 1200)

    def test_overspeed_voice_fails_instead_of_compressing_unboundedly(self):
        with patch.object(speech, "render_voice", return_value=(b"\0\0" * 48000, [])):
            with self.assertRaises(SmartError) as error:
                speech.synthesize({"language": "en", "cues": [{"start": 0, "end": 1000, "text": "Hello."}]})
        self.assertEqual(error.exception.code, "speech_too_long")

    def test_ocr_timeout_uses_protected_fallback(self):
        class Runtime:
            def asset(self, sid, actor):
                return {"path": "source.mp4"}
        with patch("shutil.which", return_value="tesseract"), patch("subprocess.run", side_effect=subprocess.TimeoutExpired("ffmpeg", 15)):
            result = resolve(Runtime(), {}, [{"source_id": "a", "start_ms": 10000, "end_ms": 12000}], None)
        self.assertTrue(result["enabled"])
        self.assertEqual((result["top"], result["bottom"]), (55, 92))

    @unittest.skipUnless(shutil.which("ffmpeg"), "requires FFmpeg")
    def test_protected_mirror_preserves_luma_and_chroma_pixels(self):
        # Test every pixel in Y, U and V planes. Chroma has half-height coordinates.
        width, height = 64, 96
        planes = [(width, height), (width // 2, height // 2), (width // 2, height // 2)]
        raw = bytes((x * 3 + y * 5 + n * 31) % 256 for n, (w, h) in enumerate(planes) for y in range(h) for x in range(w))
        graph = mirror_graph("0:v", "out", {"mode": "manual", "top": 50, "bottom": 75}, "test")
        result = subprocess.run(["ffmpeg", "-v", "error", "-f", "rawvideo", "-pix_fmt", "yuv420p", "-s", "64x96", "-i", "pipe:0", "-filter_complex", graph, "-map", "[out]", "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "yuv420p", "pipe:1"], input=raw, capture_output=True, check=True).stdout
        offset = 0
        for w, h in planes:
            for y in range(h):
                row = raw[offset+y*w:offset+(y+1)*w]
                expected = row if h*.5 <= y <= h*.75 else row[::-1]
                self.assertEqual(result[offset+y*w:offset+(y+1)*w], expected)
            offset += w*h


if __name__ == "__main__":
    unittest.main()
