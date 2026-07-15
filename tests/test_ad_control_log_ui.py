from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"
PAGES = (
    "ad-control.html",
    "ad-control-rules.html",
    "ad-control-account-pools.html",
    "ad-control-bindings.html",
    "ad-control-run.html",
    "ad-control-tokens.html",
    "ad-control-logs.html",
)


class LogUiTests(unittest.TestCase):
    def test_all_pages_use_same_cache_buster(self):
        for name in PAGES:
            html = (STATIC / name).read_text(encoding="utf-8")
            self.assertIn("/ad-control-pages.css?v=20260715copylog3", html, name)
            self.assertIn("/ad-control-pages.js?v=20260715copylog3", html, name)
            for stale in ("20260715copy2", "20260715log2", "20260715log1"):
                self.assertNotIn(stale, html, name)

    def test_log_copy_separates_flow_meta_and_storage(self):
        source = (STATIC / "ad-control-pages.js").read_text(encoding="utf-8")
        for text in ("本轮扫描", "白名单候选", "规则命中", "本批计划", "待后续处理"):
            self.assertIn(text, source)
        self.assertIn("Meta 执行结果", source)
        self.assertIn("调控日志存储", source)
        self.assertIn("ads_ai.ad_control_action_log", source)
        self.assertNotIn('logCountPill("目标"', source)

    def test_list_keeps_target_details_lazy(self):
        source = (STATIC / "ad-control-pages.js").read_text(encoding="utf-8")
        self.assertIn('include_targets: "false"', source)
        self.assertIn('view: "daily"', source)
        self.assertIn("data-lazy-targets", source)
        self.assertIn("if (!details || details.dataset.targetsLoaded) return", source)

    def test_daily_cards_preserve_raw_batch_drilldown(self):
        source = (STATIC / "ad-control-pages.js").read_text(encoding="utf-8")
        self.assertIn("按业务日汇总", source)
        self.assertIn("执行尝试（含重试）", source)
        self.assertIn("批次记录", source)
        self.assertIn("batch.action_id", source)
        self.assertIn("/targets", source)
        self.assertIn("当时状态：", source)
        self.assertIn("原因（按尝试计数）", source)

    def test_truncation_copy_distinguishes_source_limit_from_display_limit(self):
        source = (STATIC / "ad-control-pages.js").read_text(encoding="utf-8")
        self.assertIn("data.source_truncated", source)
        self.assertIn("data.has_more_groups", source)
        self.assertIn("data.has_more", source)
        self.assertNotIn("if (data.truncated)", source)


if __name__ == "__main__":
    unittest.main()
