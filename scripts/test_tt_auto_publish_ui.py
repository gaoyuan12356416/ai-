#!/usr/bin/env python3
"""Static UI contracts for independent TT automatic publishing templates."""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"
PAGE_PATHS = {
    "templates": STATIC / "tt-auto-publish-templates.html",
    "template": STATIC / "tt-auto-publish-template.html",
    "runs": STATIC / "tt-auto-publish-runs.html",
}
SCRIPT_PATHS = {
    "common": STATIC / "tt-auto-publish-common.js",
    "templates": STATIC / "tt-auto-publish-templates.js",
    "template": STATIC / "tt-auto-publish-template.js",
    "runs": STATIC / "tt-auto-publish-runs.js",
}
PAGES = {name: path.read_text(encoding="utf-8") for name, path in PAGE_PATHS.items()}
SCRIPTS = {name: path.read_text(encoding="utf-8") for name, path in SCRIPT_PATHS.items()}
QUICK_NAV = (STATIC / "quick-nav.js").read_text(encoding="utf-8")
NAVIGATION = json.loads((STATIC / "navigation.json").read_text(encoding="utf-8"))


class IdParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids: set[str] = set()
        self.scripts: list[str] = []
        self.stylesheets: list[str] = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if values.get("id"):
            self.ids.add(values["id"])
        if tag == "script" and values.get("src"):
            self.scripts.append(values["src"])
        if tag == "link" and values.get("rel") == "stylesheet" and values.get("href"):
            self.stylesheets.append(values["href"])


def parse_page(source: str) -> IdParser:
    parser = IdParser()
    parser.feed(source)
    return parser


class TtAutoPublishUiTest(unittest.TestCase):
    def test_javascript_files_parse(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        for path in [*SCRIPT_PATHS.values(), STATIC / "quick-nav.js"]:
            with self.subTest(path=path.name):
                result = subprocess.run(
                    [node, "--check", str(path)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_pages_reuse_shared_shell_and_tt_permission(self):
        active_keys = {
            "templates": 'activeKey: "ttAutoPublishTemplates"',
            "template": 'activeKey: "ttAutoPublishTemplates"',
            "runs": 'activeKey: "ttAutoPublishRuns"',
        }
        for name, source in PAGES.items():
            with self.subTest(page=name):
                parser = parse_page(source)
                self.assertIn("/ui-topbar.css", parser.stylesheets)
                self.assertIn("/tt-auto-publish.css", parser.stylesheets)
                self.assertIn("/ui-topbar.js", parser.scripts)
                self.assertIn("/quick-nav.js", parser.scripts)
                self.assertIn("/tt-auto-publish-common.js", parser.scripts)
                self.assertIn("quickNav", parser.ids)
                self.assertIn("userCard", parser.ids)
                self.assertIn("authButton", parser.ids)
                self.assertIn("loginGate", parser.ids)
                self.assertIn("permissionGate", parser.ids)
                self.assertIn("pageRoot", parser.ids)
                self.assertIn("tt_posts", source)
                self.assertIn(active_keys[name], SCRIPTS[name])
        self.assertIn('api("/api/ui/topbar")', SCRIPTS["common"])
        self.assertIn("user.permissions.tt_posts", SCRIPTS["common"])
        self.assertIn("window.QuickNav.render", SCRIPTS["common"])
        self.assertIn("window.UiTopbar.render", SCRIPTS["common"])
        self.assertIn("window.UiTopbar.handleAuthAction", SCRIPTS["common"])

    def test_navigation_registers_both_independent_pages(self):
        group = next(item for item in NAVIGATION if item.get("key") == "tiktok_platform")
        items = {item["key"]: item for item in group["items"]}
        expected = {
            "ttAutoPublishTemplates": "/tt-auto-publish-templates.html",
            "ttAutoPublishRuns": "/tt-publish-logs.html",
        }
        for key, href in expected.items():
            with self.subTest(key=key):
                self.assertEqual(items[key]["href"], href)
                self.assertEqual(items[key]["module"], "tt_posts")
                self.assertTrue(items[key]["enabled"])
                self.assertIn(f'key: "{key}"', QUICK_NAV)
                self.assertIn(f'{key}: "{href}"', QUICK_NAV)
        self.assertIn("ttAutoPublishTemplatesExists", QUICK_NAV)
        self.assertIn("ttAutoPublishRunsExists", QUICK_NAV)

    def test_new_pages_use_only_the_new_api_namespace(self):
        combined = "\n".join(SCRIPTS.values())
        self.assertIn('const API_BASE = "/api/admin/tt-auto-publish"', SCRIPTS["common"])
        self.assertNotIn("/api/admin/tt-posts", combined)
        self.assertNotIn("/material-pool", combined)
        self.assertNotIn("/auto-config", combined)
        self.assertNotIn("/api/admin/tt-posts/run-now", combined)

    def test_untrusted_content_is_rendered_without_inner_html(self):
        for name, source in {**PAGES, **SCRIPTS}.items():
            with self.subTest(source=name):
                self.assertNotIn(".innerHTML", source)
                self.assertNotIn("document.write", source)
        self.assertIn("node.textContent", SCRIPTS["common"])
        self.assertIn("node.replaceChildren()", SCRIPTS["common"])

    def test_template_list_supports_required_actions_and_real_run_confirmation(self):
        page_ids = parse_page(PAGES["templates"]).ids
        for required in (
            "templateRows",
            "reloadTemplates",
            "filterQuery",
            "filterStatus",
            "prevPage",
            "nextPage",
            "confirmDialog",
        ):
            self.assertIn(required, page_ids)
        source = SCRIPTS["templates"]
        for action in ('"edit"', '"copy"', '"enable"', '"disable"', '"run"'):
            self.assertIn(action, source)
        self.assertIn('"run-now"', source)
        self.assertIn("body.confirmed = true", source)
        self.assertIn("expected_version: version", source)
        self.assertIn("确认真实执行", source)
        self.assertIn("视频准备完成后自动发布", source)

    def test_editor_contains_two_sequential_filter_layers(self):
        ids = parse_page(PAGES["template"]).ids
        required = {
            "accountList",
            "metricWindowDays",
            "platform",
            "dramaLaunchWindowDays",
            "cooldownDays",
            "dramaResourceTypes",
            "dramaRoasMin",
            "dramaRoasMax",
            "dramaSpendMin",
            "dramaSpendMax",
            "dramaSortBy",
            "dramaSortDirection",
            "materialDurationMin",
            "materialDurationMax",
            "materialRoasMin",
            "materialRoasMax",
            "materialSpendMin",
            "materialSpendMax",
            "materialSortBy",
            "materialSortDirection",
            "captionTemplate",
            "videoTemplate",
            "summaryVideoTemplate",
        }
        self.assertFalse(required - ids, required - ids)
        source = PAGES["template"]
        self.assertLess(source.index("先筛选剧"), source.index("再筛选素材"))
        script = SCRIPTS["template"]
        for field in (
            "metric_window_days",
            "drama_launch_window_days",
            "cooldown_days",
            "resource_type_v2",
            "duration_min_seconds",
            "duration_max_seconds",
            "drama_rule",
            "material_rule",
            "sort_by",
            "sort_direction",
        ):
            self.assertIn(field, script)
        self.assertIn("account_ids", script)
        self.assertIn("drama_language", script)
        self.assertIn("ads_setting.ads_facebook_post_blacklist", source)
        self.assertIn("历史素材永久排除", source)
        self.assertIn("{{content_id}}", source)
        self.assertIn("{desc}", source)
        self.assertIn("{url}", source)
        self.assertIn("{code}", source)
        self.assertIn("唯一四位剧情查询码", source)
        self.assertIn("剧 ID 宏为可选", source)
        self.assertNotIn("发布文案模板必须包含", script)

    def test_editor_selects_an_immutable_video_template_route(self):
        page = PAGES["template"]
        script = SCRIPTS["template"]
        self.assertIn('value="random_overlay"', page)
        self.assertIn('value="direct_outro"', page)
        self.assertIn("随机排重", page)
        self.assertIn("拼接结尾", page)
        self.assertIn('const DEFAULT_VIDEO_TEMPLATE = "random_overlay"', script)
        self.assertIn("video_template: videoTemplate", script)
        self.assertIn(
            'configValue("video_template", DEFAULT_VIDEO_TEMPLATE)', script
        )

    def test_resource_type_v2_is_optional_chinese_enum_multiselect(self):
        page = PAGES["template"]
        script = SCRIPTS["template"]
        ids = parse_page(page).ids
        for required in (
            "dramaResourceTypes",
            "dramaResourceTypeMenu",
            "dramaResourceTypeOptions",
            "clearDramaResourceTypes",
        ):
            self.assertIn(required, ids)
        self.assertIn("非必选；不选择时不限制短剧类型。", page)
        self.assertNotIn(
            'class="required" for="dramaResourceTypes"',
            page,
        )
        expected = {
            "0": "其他",
            "1": "翻译剧非首发",
            "2": "本土首发",
            "3": "本土对投",
            "4": "本土二轮采买",
            "5": "本土自制",
            "6": "翻译剧首发",
            "7": "首发本土动态漫",
            "8": "二轮本土动态漫",
            "9": "首发翻译动态漫",
            "10": "二轮翻译动态漫",
            "11": "翻译剧自制",
            "12": "漫剧自制",
            "13": "AI本土真人剧自制",
            "14": "AI本土真人剧首发",
            "15": "二轮本土AI真人剧",
            "16": "翻译AI真人剧首发",
            "17": "二轮翻译AI真人剧",
            "18": "AI本土解说剧自制",
            "19": "AI本土解说剧首发",
            "20": "AI本土解说剧二轮",
            "21": "AI翻译解说剧首发",
            "22": "AI翻译解说剧首发",
            "100": "小说",
        }
        for value, label in expected.items():
            with self.subTest(value=value):
                self.assertIn(
                    f'{{ value: "{value}", label: "{label}" }}',
                    script,
                )
        self.assertNotIn('{ value: "-1"', script)
        self.assertIn('let text = "不限类型"', script)
        self.assertIn("state.selectedResourceTypes.clear()", script)
        self.assertIn("resource_type_v2: parseResourceTypes()", script)

    def test_editor_has_fixed_and_random_schedules(self):
        ids = parse_page(PAGES["template"]).ids
        for required in (
            "scheduleModeFixed",
            "scheduleModeRandom",
            "publishTimes",
            "addPublishTime",
            "randomDailyCount",
        ):
            self.assertIn(required, ids)
        source = SCRIPTS["template"]
        self.assertIn('{ mode: "fixed", times }', source)
        self.assertIn('{ mode: "random", daily_count: dailyCount }', source)
        self.assertIn("dailyCount < 1 || dailyCount > 24", source)
        self.assertIn("Array.from(new Set(timeValues())).sort()", source)

    def test_editor_does_not_offer_interaction_or_disclosure_settings(self):
        combined = PAGES["template"] + SCRIPTS["template"]
        forbidden_fields = (
            "allow_comment",
            "allow_duet",
            "allow_stitch",
            "commercial_disclosure",
            "brand_organic_toggle",
            "brand_content_toggle",
            "privacy_level",
            "is_aigc",
        )
        for field in forbidden_fields:
            self.assertNotIn(field, combined)
        self.assertIn("不在这里覆盖评论、Duet、Stitch 或内容披露配置", PAGES["template"])

    def test_runs_page_lists_and_renders_account_task_details(self):
        ids = parse_page(PAGES["runs"]).ids
        for required in (
            "runRows",
            "runDetailDialog",
            "runDetailFacts",
            "runTaskRows",
            "runSnapshot",
            "runEvents",
            "filterTemplateId",
            "filterTriggerType",
            "filterStatus",
        ):
            self.assertIn(required, ids)
        source = SCRIPTS["runs"]
        self.assertIn('ui.readItems(payload, ["runs", "items"])', source)
        self.assertIn('ui.readItem(payload, ["run", "item"])', source)
        self.assertIn('ui.readItems(payload, ["tasks", "account_tasks"])', source)
        self.assertIn('ui.readItems(payload, ["events"])', source)
        self.assertIn("template_snapshot", source)
        self.assertIn("blacklist_snapshot", source)
        self.assertIn("publish_id", source)
        self.assertIn("needs_review", source)
        self.assertIn("item.account_display_name || item.account_username", source)

    def test_template_write_contract_matches_api_document(self):
        source = SCRIPTS["template"]
        self.assertIn('method: "POST"', source)
        self.assertIn("payload.expected_version = state.version", source)
        self.assertIn('ui.readItem(response, ["template", "item"])', source)
        self.assertIn('ui.readItems(payload, ["accounts", "items"])', source)
        self.assertIn("caption_template", source)
        self.assertIn("video_template", source)
        self.assertIn("platform: integerValue", source)

    def test_direct_outro_deploy_contract_is_isolated_from_random_overlay(self):
        cpu_env = (ROOT / "deploy" / "tt-auto-post.env.example").read_text(
            encoding="utf-8"
        )
        gpu_env = (
            ROOT / "deploy" / "tt-post-gpu-direct-outro.env.example"
        ).read_text(encoding="utf-8")
        gpu_unit = (
            ROOT / "deploy" / "tt-gpu-direct-outro.service"
        ).read_text(encoding="utf-8")
        tunnel = (
            ROOT / "deploy" / "tt-gpu-direct-outro-reverse-tunnel.service"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "TT_AUTO_POST_DIRECT_OUTRO_GPU_URL=http://127.0.0.1:18834",
            cpu_env,
        )
        self.assertIn("TT_POST_GPU_PORT=8832", gpu_env)
        self.assertIn("TT_POST_GPU_MEDIA_MODE=direct_outro", gpu_env)
        self.assertIn(
            "TT_POST_DEFAULT_SOURCE_TRIM_TAIL_SECONDS=4.333333", gpu_env
        )
        self.assertIn(
            "TT_POST_GPU_WORK_ROOT=/data/tt-post-publisher/direct-outro-work",
            gpu_env,
        )
        self.assertIn(
            "EnvironmentFile=/etc/tt-post-gpu-direct-outro.env", gpu_unit
        )
        self.assertIn(
            "ReadWritePaths=/data/tt-post-publisher/direct-outro-work",
            gpu_unit,
        )
        self.assertIn(
            "-R 127.0.0.1:18834:127.0.0.1:8832", tunnel
        )
        self.assertNotIn(":18830:127.0.0.1:8830", tunnel)

    def test_only_direct_outro_client_opts_into_the_new_loopback_port(self):
        service = (
            ROOT / "features" / "tt_auto_posts" / "service.py"
        ).read_text(encoding="utf-8")
        random_block = service.split("gpu = GPUClient(", 1)[1].split(
            "direct_outro_gpu = GPUClient(", 1
        )[0]
        direct_block = service.split("direct_outro_gpu = GPUClient(", 1)[1].split(
            "primary_trim =", 1
        )[0]
        self.assertNotIn("allowed_loopback_ports", random_block)
        self.assertIn("allowed_loopback_ports=(18834,)", direct_block)


if __name__ == "__main__":
    unittest.main()
