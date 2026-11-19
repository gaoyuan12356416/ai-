import subprocess
import shutil
import unittest
from unittest.mock import patch

from features.ai_video_editing import narration, speech
from features.ai_video_editing.smart_common import SmartError
from features.ai_video_editing.subtitle_protection import resolve


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

    def test_subtitle_glyphs_stay_readable_without_preserving_background_band(self):
        import cv2
        import numpy as np
        from features.ai_video_editing.protected_mirror import mirror_frame
        frame=np.zeros((1280,720,3),np.uint8)
        frame[:,:360]=[60,80,100]
        frame[:,360:]=[80,100,130]
        # Without subtitles every background pixel must flip, including the protected band.
        self.assertTrue(np.array_equal(mirror_frame(frame,55,92),cv2.flip(frame,1)))
        cv2.putText(frame,'Noah? Where are you?',(80,850),cv2.FONT_HERSHEY_SIMPLEX,1.2,(0,0,0),6)
        cv2.putText(frame,'Noah? Where are you?',(80,850),cv2.FONT_HERSHEY_SIMPLEX,1.2,(255,255,255),2)
        result=mirror_frame(frame,55,92)
        white=(frame[:,:,0]==255)
        self.assertTrue(np.array_equal(result[white],frame[white]))
        self.assertTrue(np.array_equal(result[900],frame[900,::-1]))
        short=np.full((1280,720,3),80,np.uint8)
        cv2.putText(short,'No!',(310,850),cv2.FONT_HERSHEY_SIMPLEX,1.2,(0,0,0),6)
        cv2.putText(short,'No!',(310,850),cv2.FONT_HERSHEY_SIMPLEX,1.2,(255,255,255),2)
        fixed=mirror_frame(short,55,92); glyphs=short[:,:,0]==255
        self.assertTrue(np.array_equal(fixed[glyphs],short[glyphs]))


if __name__ == "__main__":
    unittest.main()
