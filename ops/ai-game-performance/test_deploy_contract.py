import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]


class DeployContractTests(unittest.TestCase):
    def test_nginx_private_cache_and_login_contract(self):
        config = (HERE / "ai-game-performance-auth.conf").read_text(encoding="utf-8")
        self.assertIn("auth_request /_tt_minis_report_auth", config)
        self.assertIn('private, max-age=900', config)
        self.assertIn('private, no-store', config)
        self.assertIn("@ai_game_report_login", config)
        self.assertIn("next=/reports/ai-game-performance/", config)
        self.assertNotIn("@tt_minis_report_login", config)

    def test_service_uses_shared_lock_and_data_disk(self):
        service = (REPO / "deploy" / "ai-game-performance-refresh.service").read_text(encoding="utf-8")
        timer = (REPO / "deploy" / "ai-game-performance-refresh.timer").read_text(encoding="utf-8")
        self.assertIn("/tmp/tt_minis_multi_dim_dashboard.lock", service)
        self.assertIn("SuccessExitStatus=75", service)
        self.assertIn("/mnt/data-disk/ai-game-performance", service)
        self.assertIn("--refresh-cache --publish", service)
        self.assertIn("*:12,42:00", timer)


if __name__ == "__main__":
    unittest.main()
