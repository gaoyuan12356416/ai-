"""Run the established safety suite against the isolated pool-dialog rollout."""
import unittest
from unittest.mock import patch

import deploy_fb_page_list_performance as historical_release
import test_fb_page_list_performance_deploy as inherited
from deploy_fb_pool_load_performance import _release as release


class PoolPerformanceReleaseTests(inherited.PerformanceReleaseTests):
    def setUp(self):
        self.scope = patch.object(inherited, "release", release)
        self.scope.start()
        self.addCleanup(self.scope.stop)

    def test_historical_release_configuration_is_unchanged(self):
        self.assertEqual(historical_release.BASELINE, "661be45")
        self.assertEqual(historical_release.REMOTE_REF, "refs/heads/codex/fb-page-list-performance-20261020")
        self.assertIn("features/fb_video_publish/sources.py", historical_release.ALLOWED)
        self.assertIsNot(release, historical_release)
        self.assertEqual(release.BASELINE, "0770d15")
        self.assertEqual(release.REMOTE_REF, "refs/heads/codex/fb-pool-load-performance-20261020")

    def test_only_reviewed_gateway_and_four_statics_are_deployable(self):
        self.assertEqual(release.ALLOWED, {
            "features/fb_video_publish/gateway.py",
            "static/fb-pages.html", "static/fb-video-publish.html",
            "static/fb-video-publish-records.html", "static/fb-video-publish.js",
        })
        for relative in ("features/fb_video_publish/pool.py", "features/fb_video_publish/sources.py",
                         "features/fb_video_publish/service.py"):
            with patch.object(release.base, "run", return_value=relative):
                with self.assertRaisesRegex(RuntimeError, "outside performance patch"):
                    release.stage_targets()


if __name__ == "__main__":
    unittest.main()
