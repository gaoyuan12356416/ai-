import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]


class DeployContractTests(unittest.TestCase):
    def test_nginx_is_public_and_noindex(self):
        config = (HERE / "opay-excellent-creatives-public.conf").read_text(encoding="utf-8")
        self.assertIn("/reports/opay-excellent-creatives/", config)
        self.assertIn('X-Robots-Tag "noindex, nofollow"', config)
        self.assertEqual(config.count("auth_request off;"), 5)
        self.assertEqual(config.count("auth_basic off;"), 5)
        self.assertNotIn("feishu", config.casefold())
        self.assertIn('Cache-Control "no-store"', config)
        self.assertIn("immutable", config)

    def test_timers_use_days_three_and_five(self):
        service = (REPO / "deploy" / "opay-excellent-creatives-refresh@.service").read_text(
            encoding="utf-8"
        )
        initial = (REPO / "deploy" / "opay-excellent-creatives-initial.timer").read_text(
            encoding="utf-8"
        )
        final = (REPO / "deploy" / "opay-excellent-creatives-final.timer").read_text(
            encoding="utf-8"
        )
        self.assertIn("--stage %i --refresh --publish", service)
        self.assertIn("/mnt/data-disk/opay-excellent-creatives", service)
        self.assertIn("SuccessExitStatus=75", service)
        self.assertIn("ProtectHome=read-only", service)
        self.assertIn("EnvironmentFile=/etc/opay-excellent-creatives.env", service)
        self.assertIn("/tmp/opay-excellent-creatives.lock", service)
        self.assertNotIn("tt_minis_multi_dim_dashboard.lock", service)
        self.assertIn("*-*-03 10:00:00", initial)
        self.assertIn("@initial.service", initial)
        self.assertIn("*-*-05 10:00:00", final)
        self.assertIn("@final.service", final)

    def test_video_frame_has_bounded_opencv_fallback(self):
        generator = (HERE / "opay_excellent_creatives.py").read_text(encoding="utf-8")
        helper = (HERE / "extract_video_frame.py").read_text(encoding="utf-8")
        self.assertIn('ROOT / "extract_video_frame.py"', generator)
        self.assertIn("timeout=max(20, MEDIA_TIMEOUT_SECONDS * 3)", generator)
        self.assertIn("cv2.VideoCapture", helper)
        self.assertIn("CAP_PROP_POS_MSEC", helper)


if __name__ == "__main__":
    unittest.main()
