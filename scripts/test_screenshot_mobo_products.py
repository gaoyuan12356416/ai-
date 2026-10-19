"""Offline acceptance tests: no credentials, network, generation, or uploads."""

import argparse
import ast
import contextlib
import datetime as dt
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest import mock
import uuid


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from features import screenshot_products as products

spec = importlib.util.spec_from_file_location("auto_cover", ROOT / "scripts/auto_submit_cover_synthesis.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
APP_TREE = ast.parse((ROOT / "app.py").read_text(encoding="utf-8-sig"))


def app_functions(*names, **namespace):
    """Load only pure call sites; importing app.py would initialize services."""
    nodes = [node for node in APP_TREE.body if isinstance(node, ast.FunctionDef) and node.name in names]
    if len(nodes) != len(names):
        raise AssertionError("requested application function was not found")
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(ROOT / "app.py"), "exec"), namespace)
    return namespace


def arguments(profile="dramawave", **overrides):
    with mock.patch.dict(runner.os.environ, {}, clear=True):
        result = runner.build_parser().parse_args(["--date", "2026-10-18", "--submit-delay", "0"])
    result = runner.profile_args(result, next(p for p in products.AUTO_PROFILES if p["name"] == profile))
    for name, value in overrides.items():
        setattr(result, name, value)
    return result


class SourceDatabase:
    """Execute generated SELECTs with only MySQL/SQLite syntax adaptation."""

    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.create_function("IF", 3, lambda condition, yes, no: yes if condition else no)
        self.conn.executescript("""
            CREATE TABLE ads_custom_source_insight (
                product TEXT, dt TEXT, platform TEXT, data_source_id TEXT,
                spend REAL, af_installs INT, af_revenue0 REAL, revenue REAL);
            CREATE TABLE ads_drama_resource (
                app_id TEXT, content_id TEXT, name TEXT, cover TEXT, app TEXT,
                country TEXT, language TEXT, type INT, sub_number INT,
                sub_url TEXT, updated_at TEXT, id INT);
        """)
        self.queries = []

    def resource(self, app_id, content_id, cover="https://example.invalid/cover.jpg", name="Synthetic drama",
                 language="en", updated_at="2026-10-18 12:00:00", resource_id=1, episode_url=""):
        self.conn.execute("INSERT INTO ads_drama_resource VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                          (app_id, content_id, name, cover, "resource-app", "us", language, 2,
                           1 if episode_url else 0, episode_url, updated_at, resource_id))

    def metric(self, product, content_id, spend, roas=0, date="2026-10-18", platform="0"):
        self.conn.execute("INSERT INTO ads_custom_source_insight VALUES (?,?,?,?,?,?,?,?)",
                          (product, date, platform, content_id, spend, 10, spend * roas / 100, 0))

    def select(self, sql):
        self.queries.append(sql)
        sql = re.sub(r"`[^`]+`\.`([^`]+)`", r"`\1`", sql)
        sql = re.sub(r"\s+FORCE INDEX \(psdd\)", "", sql)
        # MySQL resolves this original DramaWave HAVING identifier to the SELECT alias.
        sql = sql.replace("HAVING spend >=", "HAVING ROUND(SUM(spend), 2) >=")
        return self.conn.execute(sql).fetchall()

    def cli_select(self, sql, timeout, fieldnames):
        return [dict(zip(fieldnames, [str(value) if value is not None else "NULL" for value in row]))
                for row in self.select(sql)]


class ProductAndCoverTests(unittest.TestCase):
    def setUp(self):
        self.db = SourceDatabase()
        self.addCleanup(self.db.conn.close)

    def resolve(self, app_id, content_id):
        return products.resolve_cover_resource(app_id, content_id, self.db.select,
                                               "test", "ads_drama_resource", lambda value: value.strip())

    def test_four_products_and_upload_packages(self):
        self.assertEqual([p["app_id"] for p in products.list_products()], ["1479", "979", "3360", "2373"])
        for item in products.list_products():
            self.assertEqual(set(item), {"app_id", "app", "country", "language", "label"})
        expected = {"1479": "com.dramawave.app", "979": "com.freereels.app",
                    "3360": "com.changdu.mobovideo", "2373": "com.changdu.moboshort"}
        namespace = app_functions("app_package_for_app_id")
        for app_id, package in expected.items():
            self.assertEqual(namespace["app_package_for_app_id"](app_id), package)
        self.assertEqual(namespace["app_package_for_app_id"]("unknown"), "unknown")

    def test_metadata_cover_without_episodes_and_product_isolation(self):
        self.db.resource("3360", "shared", name="Reels", language="pt")
        self.db.resource("2373", "shared", name="Short", language="de")
        for app_id, name, language in [("3360", "Reels", "pt"), ("2373", "Short", "de")]:
            result = self.resolve(app_id, "shared")
            self.assertEqual((result["app_id"], result["drama_name"], result["language"]), (app_id, name, language))
            self.assertEqual(result["total_episodes"], 0)
        self.db.resource("1479", "only-other-product")
        with self.assertRaises(ValueError):
            self.resolve("3360", "only-other-product")

    def test_latest_nonblank_resource_uses_matching_metadata_and_id_tiebreak(self):
        self.db.resource("3360", "drama", name="Old", updated_at="2026-10-17", resource_id=50)
        self.db.resource("3360", "drama", name="Latest lower ID", language="es", resource_id=60)
        self.db.resource("3360", "drama", cover="https://example.invalid/latest.jpg", name="Latest", language="fr", resource_id=61)
        self.db.resource("3360", "drama", cover="   ", name="Blank", updated_at="2026-10-19", resource_id=70)
        result = self.resolve("3360", "drama")
        self.assertEqual((result["drama_name"], result["language"], result["cover_source_url"]),
                         ("Latest", "fr", "https://example.invalid/latest.jpg"))

    def test_cover_query_failure_and_wrong_product_are_not_hidden(self):
        query = mock.Mock(side_effect=RuntimeError("synthetic database unavailable"))
        with self.assertRaises(RuntimeError):
            products.resolve_cover_resource("3360", "x", query, "test", "resource", str)
        query = mock.Mock(return_value=[("2373", "Wrong", "https://example.invalid/x", "app", "", "")])
        with self.assertRaisesRegex(ValueError, "product mismatch"):
            products.resolve_cover_resource("3360", "x", query, "test", "resource", str)

    def test_new_validator_does_not_fetch_episodes(self):
        self.db.resource("3360", "cover-only")
        episodes = mock.Mock(side_effect=AssertionError("must not fetch episodes"))
        namespace = app_functions("validate_screenshot_request", run_mysql=self.db.select,
                                  DB_NAME="test", SOURCE_TABLE="ads_drama_resource",
                                  normalize_cover_source_url=lambda value: value.strip(), fetch_drama_episodes=episodes)
        self.assertEqual(namespace["validate_screenshot_request"]("3360", "cover-only")["app_id"], "3360")
        episodes.assert_not_called()

    def test_original_products_keep_episode_validator(self):
        for app_id in ("1479", "979"):
            sample = {"app_id": app_id, "app": "original", "country": "us", "language": "en",
                      "drama_name": "Original", "cover_url": " https://example.invalid/original.jpg "}
            episodes = mock.Mock(return_value=[sample])
            namespace = app_functions("validate_screenshot_request", fetch_drama_episodes=episodes,
                                      normalize_cover_source_url=lambda value: value.strip(),
                                      product_name_for_app_id=lambda value, fallback: fallback)
            result = namespace["validate_screenshot_request"](app_id, "original")
            episodes.assert_called_once_with("original", app_id=app_id)
            self.assertEqual(result["total_episodes"], 1)

    def test_product_route_enforces_cover_permission_and_page_uses_route(self):
        handler = next(n for n in APP_TREE.body if isinstance(n, ast.ClassDef) and n.name == "DramaMaterialHandler")
        get_method = next(n for n in handler.body if isinstance(n, ast.FunctionDef) and n.name == "do_GET")
        route = next(n for n in ast.walk(get_method) if isinstance(n, ast.If)
                     and isinstance(n.test, ast.Compare)
                     and any(isinstance(c, ast.Constant) and c.value == "/api/drama-screenshot-material/products"
                             for c in n.test.comparators))
        function = ast.parse("def isolated_route(self, parsed):\n    pass\n").body[0]
        function.body = [route]
        response = mock.Mock()
        namespace = {"json_response": response}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])), "route", "exec"), namespace)
        target = types.SimpleNamespace(path="/api/drama-screenshot-material/products")
        denied = mock.Mock()
        denied._require_module.return_value = False
        namespace["isolated_route"](denied, target)
        denied._require_module.assert_called_once_with("cover_synthesis")
        response.assert_not_called()
        allowed = mock.Mock()
        allowed._require_module.return_value = True
        namespace["isolated_route"](allowed, target)
        response.assert_called_once_with(allowed, 200, {"items": products.list_products()})
        page = (ROOT / "static/screenshots.html").read_text(encoding="utf-8")
        self.assertIn('api("/api/drama-screenshot-material/products")', page)
        self.assertNotIn('api("/api/drama-material/products")', page)

    def test_manual_single_and_batch_keep_product_and_metadata(self):
        validation = {"app_id": "3360", "app": "moboreels", "country": "br", "language": "pt",
                      "drama_name": "Synthetic", "cover_source_url": "https://example.invalid/source.jpg"}
        validate = mock.Mock(return_value=validation)
        saved = mock.Mock()
        dispatch = mock.Mock()
        namespace = app_functions("submit_screenshot_job", "submit_screenshot_job_batch",
                                  uuid=uuid, validate_screenshot_request=validate,
                                  ensure_no_duplicate_screenshot_job=mock.Mock(),
                                  clear_screenshot_job_deleted_marker=mock.Mock(), upsert_screenshot_job_record=saved,
                                  run_screenshot_job_async=dispatch, now_text=lambda: "2026-10-19 12:00:00")
        result = namespace["submit_screenshot_job_batch"]({"app_id": "3360", "content_ids": ["a", "b", "a"]})
        self.assertEqual(result["accepted_count"], 2)
        self.assertEqual(saved.call_count, 2)
        self.assertEqual(dispatch.call_count, 2)
        for call in saved.call_args_list:
            job = call.args[0]
            self.assertEqual((job["app_id"], job["language"], job["cover_source_url"]),
                             ("3360", "pt", validation["cover_source_url"]))
            for key in ("square_1x1_url", "landscape_1_91x1_url", "portrait_4x5_url"):
                self.assertIn(key, job)
        validation["app_id"] = "2373"
        validation["app"] = "moboshort"
        result = namespace["submit_screenshot_job"]({"app_id": "2373", "content_id": "short-drama"})
        self.assertEqual((result["app_id"], result["content_id"]), ("2373", "short-drama"))
        self.assertEqual(saved.call_args.args[0]["app_id"], "2373")

    def test_callback_routes_three_sizes_and_reuses_material_ids(self):
        post = mock.Mock(return_value=types.SimpleNamespace(status_code=200, text='{"code":0,"data":[]}'))
        specs = [{"key": "square_1x1", "field": "square_1x1_url", "ratio": "1:1"},
                 {"key": "landscape_1_91x1", "field": "landscape_1_91x1_url", "ratio": "1.91:1"},
                 {"key": "portrait_4x5", "field": "portrait_4x5_url", "ratio": "4:5"}]
        namespace = app_functions("app_package_for_app_id", "screenshot_material_id", "screenshot_asset_name",
                                  "post_screenshot_ai_source_callback", AI_SOURCE_CALLBACK_ENABLED=True,
                                  AI_SOURCE_CALLBACK_URL="https://example.invalid/callback",
                                  AI_SOURCE_CALLBACK_TOKEN="synthetic-token", AI_SOURCE_CALLBACK_TIMEOUT=3,
                                  get_admin_user_callback_username=lambda value: "synthetic-user",
                                  callback_time_text=lambda value: "2026-10-19 12:00:00", SCREENSHOT_SPECS=specs,
                                  requests=types.SimpleNamespace(post=post), json=json)
        for app_id, package in [("3360", "com.changdu.mobovideo"), ("2373", "com.changdu.moboshort")]:
            job = {"app_id": app_id, "content_id": "synthetic-drama", "job_id": "synthetic-job-" + app_id,
                   **{spec["field"]: "https://example.invalid/" + spec["key"] + ".jpg" for spec in specs}}
            namespace["post_screenshot_ai_source_callback"](job)
            first = post.call_args.kwargs["json"]
            namespace["post_screenshot_ai_source_callback"](job)
            second = post.call_args.kwargs["json"]
            self.assertEqual(len(first), 3)
            self.assertEqual({item["app_package"] for item in first}, {package})
            self.assertEqual({item["content_id"] for item in first}, {"synthetic-drama"})
            self.assertEqual({item["category"] for item in first}, {"1:1", "1.91:1", "4:5"})
            self.assertEqual([item["material_id"] for item in first], [item["material_id"] for item in second])

        job_before = dict(job)
        for status, body in [(200, '{"code":-1,"msg":"unsupported product"}'),
                             (200, '{"data":[]}'), (200, '[]'), (200, 'invalid'),
                             (200, '{"code":true}'), (403, '{"code":0}'), (302, '{"code":0}')]:
            post.return_value = types.SimpleNamespace(status_code=status, text=body)
            with self.subTest(status=status, body=body), self.assertRaises(RuntimeError):
                namespace["post_screenshot_ai_source_callback"](job)
            self.assertEqual(job, job_before, 'failed upload must preserve image URLs and material identity')
        for field in ('AI_SOURCE_CALLBACK_ENABLED', 'AI_SOURCE_CALLBACK_TOKEN'):
            with mock.patch.dict(namespace, {field: False}), self.assertRaises(RuntimeError):
                namespace["post_screenshot_ai_source_callback"](job)
        with self.assertRaisesRegex(RuntimeError, 'all three'):
            namespace["post_screenshot_ai_source_callback"]({**job, 'square_1x1_url': ''})


class DeployCompositionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("cover_deploy", ROOT / "ops/screenshot-mobo-products-20261019/deploy.py")
        cls.deploy = importlib.util.module_from_spec(spec)
        with mock.patch.dict(sys.modules, {"fcntl": types.SimpleNamespace()}):
            spec.loader.exec_module(cls.deploy)

    def live_source(self):
        return '''def app_package_for_app_id(app_id):
    return "legacy"

def validate_screenshot_request(app_id, content_id):
    return {"legacy": True}

def post_screenshot_ai_source_callback(job):
    return {"legacy": True}

def production_only_function():
    return "must survive"

class ProductionOnlyHandler:
    def do_GET(self):
        if parsed.path == "/api/drama-material/products":
            return "legacy video products"
'''

    def test_patch_preserves_live_only_code_and_refuses_repeat_or_drift(self):
        live = self.live_source()
        repo = (ROOT / "app.py").read_text(encoding="utf-8-sig")
        expected = {name: self.deploy.ast_sha(node) for name, node in self.deploy.function_nodes(live).items()
                    if name in self.deploy.FUNCTIONS}
        result = self.deploy.compose_app(live, repo, expected)
        self.assertIn('return "must survive"', result)
        self.assertIn('return "legacy video products"', result)
        self.assertEqual(result.count(self.deploy.URL), 1)
        for name in self.deploy.FUNCTIONS:
            self.assertEqual(self.deploy.ast_sha(self.deploy.function_nodes(result)[name]),
                             self.deploy.ast_sha(self.deploy.function_nodes(repo)[name]))
        with self.assertRaisesRegex(RuntimeError, "drift"):
            self.deploy.compose_app(result, repo, expected)
        with self.assertRaisesRegex(RuntimeError, "drift"):
            self.deploy.compose_app(live.replace('return "legacy"', 'return "changed-live"'), repo, expected)

    def test_fingerprint_handles_binary_constants_and_optional_python_fields(self):
        node = ast.parse("def sample():\n    return (b'bytes', 2j, ...)\n").body[0]
        expected = self.deploy.ast_sha(node)
        self.assertEqual(len(expected), 64)
        node.type_params = []
        self.assertEqual(self.deploy.ast_sha(node), expected)
        del node.type_params
        self.assertEqual(self.deploy.ast_sha(node), expected)
        other = ast.parse("def sample():\n    return (b'changed', 2j, ...)\n").body[0]
        self.assertNotEqual(self.deploy.ast_sha(other), expected)


class PreservedAcceptanceTests(unittest.TestCase):
    def test_receiving_records_use_external_material_id_without_image_type_filter(self):
        source = (ROOT / 'ops/screenshot-mobo-products-20261019/acceptance.py').read_text(encoding='utf-8')
        nodes = [n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef)
                 and n.name in ('material_ids_for_jobs', 'material_rows')]
        process = mock.Mock(return_value=types.SimpleNamespace(returncode=0,
                            stdout='1\n700\tmoboreels\t123\thttps://example.invalid/a.jpg\tasset\t2\t102\n'))
        namespace = {'SIZES': {'square_1x1_url': (1200, 1200)}, 'hashlib': hashlib,
                     'runpy': types.SimpleNamespace(run_path=lambda path: {'load_env_file': lambda path: None,
                                                                         'mysql_escape': lambda value: "'" + value + "'"}),
                     'LIVE': Path('/synthetic'), 'os': types.SimpleNamespace(environ={'DRAMA_DB_USER': 'synthetic-user'}),
                     'subprocess': types.SimpleNamespace(run=process, PIPE=-1)}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), 'receiving-readback', 'exec'), namespace)
        ids = namespace['material_ids_for_jobs']([{'job_id': 'synthetic-job'}])
        self.assertEqual(ids, [str(int(hashlib.sha1(b'synthetic-job:square_1x1').hexdigest()[:15], 16))])
        rows = namespace['material_rows'](['67767322'], ids)
        sql = process.call_args.args[0][-1]
        formal, receiving = sql.split(' UNION ALL ')
        self.assertIn("data_source_id IN ('67767322')", formal)
        self.assertIn('data_source=6 AND type=1', formal)
        self.assertIn("data_source_id IN ('" + ids[0] + "')", receiving)
        self.assertIn('data_source=102', receiving)
        self.assertNotIn('type=1', receiving)
        self.assertEqual((rows[0]['data_source'], rows[0]['type']), ('102', '2'))

    def test_resume_requires_exact_failed_canary_and_all_original_image_bytes(self):
        source = (ROOT / 'ops/screenshot-mobo-products-20261019/resume_acceptance.py').read_text(encoding='utf-8')
        node = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == 'validate_preserved')
        fields = ('square_1x1_url', 'landscape_1_91x1_url', 'portrait_4x5_url')
        namespace = {'JOBS': {'synthetic-job': '3360'}, 'acceptance': types.SimpleNamespace(SIZES=dict.fromkeys(fields)),
                     'json': json, 'Path': Path, 'hashlib': hashlib}
        exec(compile(ast.Module(body=[node], type_ignores=[]), 'resume-guard', 'exec'), namespace)
        check = namespace['validate_preserved']
        with tempfile.TemporaryDirectory() as directory:
            job = {'job_id': 'synthetic-job', 'app_id': '3360', 'content_id': '67767322', 'status': 'failed',
                   'error_message': 'app_package not supported'}
            assets, images = {}, []
            for field in fields:
                path = Path(directory) / field
                path.write_bytes(field.encode())
                job[field] = 'https://example.invalid/' + field
                assets[field.removesuffix('_url')] = {'public_output_path': str(path)}
                images.append({'job_id': job['job_id'], 'field': field, 'url': job[field],
                               'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
            job['assets_json'] = json.dumps(assets)
            check(job, images)
            for patch in ({'status': 'done'}, {'app_id': '2373'}, {'error_message': 'network timeout'},
                          {'square_1x1_url': 'changed-url'}):
                with self.subTest(patch=patch), self.assertRaises(AssertionError):
                    check({**job, **patch}, images)
            (Path(directory) / fields[0]).write_bytes(b'changed')
            with self.assertRaisesRegex(AssertionError, 'do not regenerate'):
                check(job, images)
            (Path(directory) / fields[0]).unlink()
            with self.assertRaisesRegex(AssertionError, 'do not regenerate'):
                check(job, images)


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.db = SourceDatabase()
        self.addCleanup(self.db.conn.close)

    def candidates(self, profile):
        with mock.patch.object(runner, "run_mysql", side_effect=self.db.cli_select):
            return runner.fetch_candidates(arguments(profile))

    def test_dramawave_retains_both_inclusive_boundaries_and_episode_requirement(self):
        for content_id, spend, roas, episode in [("exact", 1000, 45, True), ("low-spend", 999.99, 99, True),
                                               ("low-roas", 5000, 44.99, True), ("high", 1001, 45.01, True),
                                               ("no-episode", 4000, 50, False)]:
            self.db.metric("dramawave", content_id, spend, roas)
            self.db.resource("1479", content_id, episode_url="video.mp4" if episode else "")
        self.assertEqual([r["content_id"] for r in self.candidates("dramawave")], ["high", "exact"])

    def test_both_new_products_use_strict_unrounded_spend_and_ignore_roas(self):
        for profile, source, app_id in [("moboreels", "moboreels", "3360"), ("moboshort", "MoboShort", "2373")]:
            for content_id, spend, roas in [("exact", 1000, 100), ("low", 999.99, 100),
                                           ("just-above", 1000.001, 0), ("above", 1000.01, 44.99), ("high", 4000, 0)]:
                self.db.metric(source, content_id, spend, roas)
                self.db.resource(app_id, content_id)
            with self.subTest(profile=profile):
                self.assertEqual([r["content_id"] for r in self.candidates(profile)], ["high", "above", "just-above"])

    def test_date_platform_aggregation_order_and_local_cover_filter(self):
        for content_id, spend, date, platform in [("aggregate", 1600, "2026-10-18", "0"),
                                                ("aggregate", 1601, "2026-10-18", "0"),
                                                ("wrong-date", 5000, "2026-10-17", "0"),
                                                ("wrong-platform", 5000, "2026-10-18", "1"),
                                                ("foreign-cover", 5000, "2026-10-18", "0")]:
            self.db.metric("moboreels", content_id, spend, date=date, platform=platform)
            self.db.resource("2373" if content_id == "foreign-cover" else "3360", content_id)
        result = self.candidates("moboreels")
        self.assertEqual([r["content_id"] for r in result], ["aggregate"])
        self.assertEqual(float(result[0]["spend"]), 3201)

    def test_profile_rules_override_legacy_environment_without_mutating_parser_arguments(self):
        with mock.patch.dict(runner.os.environ, {"AUTO_COVER_MIN_ROAS": "90", "AUTO_COVER_MIN_SPEND": "9999"}):
            original = runner.build_parser().parse_args([])
        for profile in products.AUTO_PROFILES:
            selected = runner.profile_args(original, profile)
            expected = (1000, 45, ">=") if profile["name"] == "dramawave" else (1000, None, ">")
            self.assertEqual((selected.min_spend, selected.min_roas, selected.spend_operator), expected)
        self.assertEqual((original.min_spend, original.min_roas), (9999, 90))


class LedgerAndDispatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "jobs.sqlite3"
        with contextlib.closing(sqlite3.connect(self.path)) as conn:
            conn.execute("CREATE TABLE drama_screenshot_job (app_id TEXT, content_id TEXT, job_id TEXT, status TEXT, progress INT, progress_detail TEXT)")
            conn.executemany("INSERT INTO drama_screenshot_job VALUES (?,?,?,?,?,?)",
                             [("3360", state, "job-" + state, state, 10, "synthetic") for state in ("success", "running", "failed")]
                             + [("1479", "shared", "other-product", "success", 100, "synthetic")])
            conn.commit()

    def test_all_statuses_skip_but_other_product_does_not_and_database_is_unchanged(self):
        before = self.path.read_bytes()
        args = arguments("moboreels", db_path=str(self.path))
        candidates = [{"content_id": x} for x in ("success", "running", "failed", "shared")]
        with mock.patch.object(runner, "fetch_candidates", return_value=candidates), \
             mock.patch.object(runner, "api_token", return_value="synthetic-token"), \
             mock.patch.object(runner, "submit_job", return_value=(202, {"job_id": "accepted"})) as submit:
            result = runner.run_product(args)
        self.assertEqual(result["skipped_existing_count"], 3)
        self.assertEqual(result["submitted_count"], 1)
        self.assertEqual(submit.call_args.args[2:4], ("3360", "shared"))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(runner.existing_jobs(str(self.path), "2373", ["success", "shared"]), {})

    def test_large_candidate_list_is_chunked_without_losing_existing_rows(self):
        self.assertEqual(set(runner.existing_jobs(str(self.path), "3360", [str(i) for i in range(1000)] + ["failed"])), {"failed"})

    def test_missing_corrupt_and_wrong_schema_ledger_stop_submission(self):
        paths = [Path(self.temp.name) / "absent.sqlite3", Path(self.temp.name) / "corrupt.sqlite3",
                 Path(self.temp.name) / "no-table.sqlite3"]
        paths[1].write_bytes(b"not-a-sqlite-database")
        with contextlib.closing(sqlite3.connect(paths[2])):
            pass
        for path in paths:
            with self.subTest(path=path.name), mock.patch.object(runner, "fetch_candidates", return_value=[{"content_id": "new"}]), \
                 mock.patch.object(runner, "submit_job") as submit:
                with self.assertRaises((RuntimeError, sqlite3.Error)):
                    runner.run_product(arguments("moboreels", db_path=str(path)))
                submit.assert_not_called()
        self.assertFalse(paths[0].exists())

    def test_all_products_continue_after_query_failure_and_return_nonzero(self):
        seen = []
        def run_one(args):
            seen.append(args.target_app_id)
            if args.target_app_id == "3360":
                raise RuntimeError("synthetic query failure")
            return {"target_app_id": args.target_app_id, "failed_count": 0}
        with mock.patch.object(runner, "load_env_file"), mock.patch.object(runner, "run_product", side_effect=run_one), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            status = runner.main(["--all-products", "--date", "2026-10-18"])
        self.assertEqual(seen, ["1479", "3360", "2373"])
        self.assertEqual(status, 1)
        result = json.loads(output.getvalue())
        self.assertEqual(result["products"][1]["failed_count"], 1)

    def test_zero_candidates_and_dry_run_never_submit(self):
        for candidates in ([], [{"content_id": "new"}]):
            with mock.patch.object(runner, "fetch_candidates", return_value=candidates), \
                 mock.patch.object(runner, "submit_job") as submit:
                result = runner.run_product(arguments("moboreels", db_path=str(self.path), dry_run=True))
                self.assertEqual(result["candidate_count"], len(candidates))
                self.assertEqual(result["submitted_count"], len(candidates))
                submit.assert_not_called()

    def test_uncertain_post_is_not_replayed_and_stops_current_product(self):
        with mock.patch.object(runner, "fetch_candidates", return_value=[{"content_id": "a"}, {"content_id": "b"}]), \
             mock.patch.object(runner, "api_token", return_value="synthetic-token"), \
             mock.patch.object(runner, "submit_job", side_effect=TimeoutError("synthetic timeout")) as submit:
            result = runner.run_product(arguments("moboreels", db_path=str(self.path)))
        self.assertEqual(submit.call_count, 1)
        self.assertTrue(result["failed"][0]["unknown_outcome"])

    def test_default_date_is_previous_beijing_natural_day(self):
        class FrozenDatetime(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                self.assertEqual(tz.utcoffset(None), dt.timedelta(hours=8))
                return cls(2026, 10, 19, 0, 30, tzinfo=tz)
        with mock.patch.object(runner.dt, "datetime", FrozenDatetime), mock.patch.object(runner, "load_env_file"), \
             mock.patch.object(runner, "run_selected_products", return_value=[{"failed_count": 0}]) as run, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(runner.main(["--dry-run"]), 0)
        self.assertEqual(run.call_args.args[0].date, "2026-10-18")

    def test_mysql_queries_are_readonly_and_use_beijing_session(self):
        process = types.SimpleNamespace(returncode=0, stdout="", stderr="")
        with mock.patch.object(runner.subprocess, "run", return_value=process) as run:
            self.assertEqual(runner.run_mysql("SELECT 1", 30, ["one"]), [])
        self.assertIn("SET SESSION TRANSACTION READ ONLY; SET time_zone = '+08:00'; SELECT 1", run.call_args.args[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
