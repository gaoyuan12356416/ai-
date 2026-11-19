"""Both frozen random sizes must run side by side on one preparation host."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from features.fb_gpu import prepare_worker as fb
from features.random_gpu import compositor
from features.random_gpu.profiles import HD, SD, SD_PROFILES, canvas
from features.tt_gpu import worker as tt
from features.x_posts import random_template_gpu as x


class RandomCanvas(unittest.TestCase):
    def setUp(self):
        self.assets = {key: Path(key) for key in ("border","corners","opacity_video","tint")}
        self.recipe = {"rotation_millidegrees":100, "scale_bp":10000, "tint_opacity_bp":900,
                       "source_overlay":{"version":1,"opacity_bp":400,"scale_bp":12000}}
        self.info = {"duration":10., "has_audio":True}

    def test_cpu_graphs_match_frozen_request(self):
        for old in (fb.CPU_PROFILE, tt.RANDOM_OVERLAY_CPU_PROFILE, x.CPU_PROFILE):
            for prof, size in ((old,HD),(SD_PROFILES[old],SD)):
                config = SimpleNamespace(ffmpeg="ffmpeg", ffmpeg_bin="ffmpeg", profile=prof,
                    compositor_backend="cpu_legacy", video_encoder="libx264", max_output_bytes=500*1024**2)
                src, out = Path("src.mp4"), Path("out.mp4")
                if old == tt.RANDOM_OVERLAY_CPU_PROFILE:
                    cmd = tt.build_random_overlay_command(config,src,out,self.info,10,self.recipe,self.assets)
                elif old == x.CPU_PROFILE:
                    cmd = x.random_command(config,src,out,self.info,self.recipe,self.assets)
                else:
                    cmd = fb.build_command(config,src,out,self.info,self.recipe,self.assets)
                graph = cmd[cmd.index("-filter_complex")+1]
                self.assertIn("libx264",cmd)
                self.assertGreaterEqual(graph.count("scale=%d:%d" % size),6)
                if size == SD:
                    self.assertNotIn("720:1280",graph)

    def test_kernel_geometry_tracks_freeze(self):
        for w,h in (SD,HD):
            text = compositor.kernel_source(self.recipe,(w,h))
            self.assertIn(f"#define SCENE_WIDTH {w}\n",text)
            self.assertIn(f"#define SCENE_HEIGHT {h}\n",text)
            self.assertIn(f"#define SCENE_MAIN_HEIGHT {h}\n",text)

    def test_unrelated_profile_retains_legacy_contract(self):
        self.assertEqual(canvas(tt.DIRECT_OUTRO_H264_PROFILE),HD)
        self.assertNotIn(tt.DIRECT_OUTRO_H264_PROFILE,SD_PROFILES)


if __name__ == "__main__":
    unittest.main()
