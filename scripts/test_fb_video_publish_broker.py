"""Independent integration checks. All network, SQL and service calls are mocked."""
import ast
import io
import json
import os
import sys
import unittest
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from features.fb_auto_posts.repositories import MaterialRepository, PagePoolRepository
from features.fb_video_publish import client, gateway, query_broker
from features.fb_video_publish.runtime_source import GatedReadOnlyMySQL
from features.fb_video_publish.sources import SourceRepository
from patch_fb_video_publish_integration import patch_app

TOKEN = "fixture_internal_token_" + "x" * 40
ACTOR = {"tenant_key": "fixture", "user_id": "owner", "is_admin": False,
         "business_owner_id": "42", "sub_user_id": "42"}


def payload(sql="SELECT s.id FROM `kunlunads_dev`.ads_custom_source s WHERE s.id=%s",
            params=None):
    return {"sql": sql, "params": ["123"] if params is None else params,
            "schema": "kunlunads_dev", "blacklist_schema": "ads_setting"}


class Handler:
    def __init__(self, body=None, remote="127.0.0.1"):
        raw = json.dumps(payload() if body is None else body).encode()
        self.headers = Message()
        self.headers["Content-Length"] = str(len(raw))
        self.headers["Content-Type"] = "application/json"
        self.headers["Authorization"] = "Bearer " + TOKEN
        self.client_address = (remote, 12345)
        self.rfile = io.BytesIO(raw)
        self.close_connection = False
        self.responses = []


class CapturingMySQL:
    schema, blacklist_schema = "kunlunads_dev", "ads_setting"

    def __init__(self):
        self.queries = []

    def select(self, sql, params):
        query_broker.validate_query(payload(sql, list(params)))
        GatedReadOnlyMySQL._bind(sql, params)
        self.queries.append((sql, params))
        if "CAST(s.id AS CHAR) material_id" in sql:
            return [{"material_id": "123", "material_name": "Test material", "content_id": "content-123",
                     "language": "en", "video_duration": "30",
                     "media_url": "https://advertising-1306474899.cos.ap-hongkong.myqcloud.com/test.mp4"}]
        if "drama_name" in sql and "ads_drama_info" in sql:
            return [{"content_id": "content-123", "language": "en", "drama_name": "Test drama"}]
        if "drama_description" in sql and "ads_drama_resource" in sql:
            return [{"content_id": "content-123", "language": "en", "drama_description": "Test synopsis"}]
        return []


class QueryValidationTests(unittest.TestCase):
    def test_actual_page_pool_sql_templates(self):
        db = CapturingMySQL()
        pages = PagePoolRepository(db)
        for is_admin in (False, True):
            pages.list_groups(is_admin=is_admin, owner_user_id="42")
            pages.list_pages(["1", "2"], is_admin=is_admin, owner_user_id="42")
        pages.legacy_conflicts(["1", "2"])
        pages.eligible_credentials("123456789")
        self.assertEqual(len(db.queries), 6)

    def test_actual_material_source_sql_and_language_aliases(self):
        db = CapturingMySQL()
        source = SourceRepository(PagePoolRepository(db), MaterialRepository(db))
        for filters in ({}, {"search": "test", "language": "en", "uploader_id": "42"}):
            result = source.search_materials(filters)
            self.assertEqual(result["items"][0]["drama_name"], "Test drama")
        result = source.get_materials(["123"], need_desc=True)
        self.assertEqual(result["123"]["drama_description"], "Test synopsis")
        self.assertEqual(len(db.queries), 9)
        self.assertTrue(any("REPLACE(" in sql for sql, _ in db.queries))
        self.assertTrue(any("MAX_EXECUTION_TIME" in sql for sql, _ in db.queries))

    def test_nonselect_multistatement_and_unapproved_sources_rejected(self):
        for sql in (
            "DELETE FROM `kunlunads_dev`.ads_custom_source",
            "SELECT id FROM `kunlunads_dev`.ads_custom_source; SELECT 1",
            "SELECT id FROM `mysql`.user",
            "SELECT id FROM ads_custom_source",
            "SELECT id FROM `kunlunads_dev`.ads_custom_source UNION SELECT 1",
            "SELECT SLEEP(1) FROM `kunlunads_dev`.ads_custom_source",
            "SELECT LOAD_FILE('/tmp/fixture') FROM `kunlunads_dev`.ads_custom_source",
            "SELECT id INTO OUTFILE '/tmp/fixture' FROM `kunlunads_dev`.ads_custom_source",
            "SELECT id FROM `kunlunads_dev`.ads_custom_source -- comment",
        ):
            with self.subTest(sql=sql), self.assertRaises(ValueError):
                query_broker.validate_query(payload(sql, []))

    def test_comma_and_comment_join_cannot_bypass_table_allowlist(self):
        for sql in (
            "SELECT a.id FROM `kunlunads_dev`.ads_custom_source a, mysql.user u",
            "SELECT a.id FROM `kunlunads_dev`.ads_custom_source a JOIN/**/mysql.user u ON 1=1",
            "SELECT a.id FROM `kunlunads_dev`.ads_custom_source a /*! JOIN mysql.user u ON 1=1 */",
        ):
            with self.subTest(sql=sql), self.assertRaises(ValueError):
                query_broker.validate_query(payload(sql, []))

    def test_schema_and_payload_shape_fail_closed(self):
        for body in ({}, {**payload(), "schema": "other"}, {**payload(), "password": "fixture"},
                     {**payload(), "params": {}}, {**payload(), "params": [0] * 2001}):
            with self.subTest(shape=list(body)), self.assertRaises(ValueError):
                query_broker.validate_query(body)

    def test_nested_sources_reserved_column_names_and_table_subqueries(self):
        for sql in (
            "SELECT a.id FROM `kunlunads_dev`.ads_custom_source a WHERE EXISTS (SELECT 1 FROM `mysql`.user u)",
            "SELECT a.id FROM `kunlunads_dev`.ads_custom_source a JOIN `kunlunads_dev`.ads_drama_info b ON a.order=b.id, mysql.user u",
            "SELECT a.id FROM `kunlunads_dev`.ads_custom_source a WHERE a.id IN (TABLE mysql.user)",
            "SELECT `dangerous_udf`(a.id) FROM `kunlunads_dev`.ads_custom_source a",
            "SELECT mysql.`foo`(a.id) FROM `kunlunads_dev`.ads_custom_source a",
            "SELECT `mysql`.`foo`(a.id) FROM `kunlunads_dev`.ads_custom_source a",
        ):
            with self.subTest(sql=sql), self.assertRaises(ValueError):
                query_broker.validate_query(payload(sql, []))
        query_broker.validate_query(payload(
            "SELECT a.id FROM `kunlunads_dev`.ads_custom_source a WHERE EXISTS "
            "(SELECT 1 FROM `kunlunads_dev`.ads_drama_info d WHERE d.content_id=a.data_source_id)", []))

    def test_binding_is_nonrecursive_and_utf8_hex_encoded(self):
        bound = GatedReadOnlyMySQL._bind("SELECT id FROM `kunlunads_dev`.ads_custom_source WHERE name=%s",
                                       ["a'; DROP TABLE x; -- 剧"])
        self.assertNotIn("DROP TABLE", bound)
        self.assertIn("_utf8mb4 0x", bound)
        with self.assertRaises(Exception):
            GatedReadOnlyMySQL._bind("SELECT id FROM `kunlunads_dev`.ads_custom_source WHERE id=%s", [])


class BrokerBoundaryTests(unittest.TestCase):
    def dispatch(self, handler, *, env_token=TOKEN, side_effect=None):
        app = {"MYSQL_USER": "fixture-user", "MYSQL_PASSWORD": "fixture-password",
               "json_response": lambda h, status, body, **kw: h.responses.append((status, body, kw))}
        with patch.dict(os.environ, {"FB_AUTO_POST_INTERNAL_TOKEN": env_token}), \
                patch.object(query_broker, "GatedReadOnlyMySQL") as mysql:
            mysql.return_value.select.return_value = [{"id": "123"}]
            mysql.return_value.select.side_effect = side_effect
            query_broker.dispatch(handler, app)
        return mysql

    def test_exact_bearer_loopback_and_no_forwarded_call_required(self):
        for remote, bearer, forwarded in (
            ("192.0.2.1", "Bearer " + TOKEN, ""),
            ("127.0.0.1", "Bearer wrong", ""),
            ("127.0.0.1", "Bearer " + TOKEN, "198.51.100.1"),
        ):
            handler = Handler(remote=remote)
            handler.headers.replace_header("Authorization", bearer)
            if forwarded:
                handler.headers["X-Forwarded-For"] = forwarded
            mysql = self.dispatch(handler)
            self.assertEqual(handler.responses[0][0], 403)
            self.assertEqual(handler.rfile.tell(), 0)
            mysql.assert_not_called()
        handler = Handler()
        mysql = self.dispatch(handler, env_token="")
        self.assertEqual(handler.responses[0][0], 403)
        mysql.assert_not_called()

    def test_framing_rejected_before_body_read(self):
        for mutate in (
            lambda h: h.headers.add_header("Content-Length", "2"),
            lambda h: h.headers.add_header("Transfer-Encoding", "chunked"),
            lambda h: h.headers.replace_header("Content-Length", "262145"),
            lambda h: h.headers.replace_header("Content-Length", "x"),
            lambda h: h.headers.replace_header("Content-Type", "text/plain"),
        ):
            handler = Handler()
            mutate(handler)
            mysql = self.dispatch(handler)
            self.assertEqual(handler.responses[0][0], 400)
            self.assertEqual(handler.rfile.tell(), 0)
            mysql.assert_not_called()

    def test_authorized_select_returns_only_query_result(self):
        for remote in ("127.0.0.1", "::1"):
            handler = Handler(remote=remote)
            mysql = self.dispatch(handler)
            self.assertEqual(handler.responses[0][:2], (200, {"ok": True, "items": [{"id": "123"}]}))
            self.assertTrue(handler.responses[0][2]["no_store"])
            mysql.return_value.select.assert_called_once()
            self.assertNotIn("fixture-password", json.dumps(handler.responses))

    def test_sql_and_exception_details_do_not_leak(self):
        handler = Handler(payload("SELECT id FROM `mysql`.user", []))
        mysql = self.dispatch(handler)
        self.assertEqual(handler.responses[0][0], 400)
        mysql.assert_not_called()
        handler = Handler()
        self.dispatch(handler, side_effect=RuntimeError("fixture-password secret sql"))
        self.assertEqual(handler.responses[0][0], 503)
        self.assertNotIn("fixture-password", json.dumps(handler.responses))


class Response:
    def __init__(self, status=200, body=None, chunks=None):
        self.status_code = status
        self.content = json.dumps(body if body is not None else {"ok": True}).encode()
        self.chunks = chunks

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def iter_content(self, _):
        return iter(self.chunks if self.chunks is not None else [self.content])


class ClientBoundaryTests(unittest.TestCase):
    def call(self, response=None, *, base=None, error=None):
        env = {"FB_AUTO_POST_INTERNAL_TOKEN": TOKEN}
        if base:
            env["FB_AUTO_POST_ADMIN_SERVICE_URL"] = base
        with patch.object(client.requests, "Session") as create:
            session = create.return_value.__enter__.return_value
            session.request.return_value = response or Response()
            session.request.side_effect = error
            result = client.request("POST", client.PREFIX + "/batches", actor=ACTOR,
                                    payload={"operation_id": "fixture"}, environ=env)
            return result, session

    def test_client_only_fixed_loopback_no_proxy_or_redirect(self):
        result, session = self.call()
        self.assertEqual(result, {"ok": True})
        self.assertFalse(session.trust_env)
        args = session.request.call_args
        self.assertEqual(args.args[1], "http://127.0.0.1:18835/api/fb-video-publish/batches")
        self.assertFalse(args.kwargs["allow_redirects"])
        self.assertEqual(json.loads(args.kwargs["headers"]["X-FB-Video-Actor"]), ACTOR)
        with self.assertRaises(client.GatewayError) as raised:
            self.call(base="http://127.0.0.1:9999")
        self.assertFalse(raised.exception.accepted)

    def test_unknown_transport_invalid_json_and_redirect_do_not_claim_rejection(self):
        for response, error in (
            (None, client.requests.Timeout("fixture")),
            (Response(chunks=[b"not-json"]), None),
            (Response(302, {"message": "redirect"}), None),
            (Response(503, {"message": "unknown failure"}), None),
        ):
            with self.subTest(response=response), self.assertRaises(client.GatewayError) as raised:
                self.call(response, error=error)
            self.assertIsNone(raised.exception.accepted)

    def test_explicit_rejection_is_preserved_and_response_budget_separate(self):
        with self.assertRaises(client.GatewayError) as raised:
            self.call(Response(409, {"code": "config_changed", "message": "changed", "accepted": False}))
        self.assertIs(raised.exception.accepted, False)
        self.assertEqual(client.MAX_BYTES, 64 * 1024 * 1024)
        self.assertGreaterEqual(client.MAX_RESPONSE_BYTES, 16 * 1024 * 1024)
        result, _ = self.call(Response(body={"ok": True, "caption": "x" * (4 * 1024 * 1024 + 1)}))
        self.assertGreater(len(result["caption"]), 4 * 1024 * 1024)

    def test_oversized_and_sensitive_response_fail_unknown(self):
        for response in (
            Response(chunks=[b"x" * (client.MAX_RESPONSE_BYTES + 1)]),
            Response(body={"ok": True, "page_access_token": "fixture"}),
        ):
            with self.assertRaises(client.GatewayError) as raised:
                self.call(response)
            self.assertIsNone(raised.exception.accepted)

    def test_gateway_preserves_unknown_versus_explicit_rejection(self):
        for accepted in (False, None):
            handler = SimpleNamespace(command="GET", _cookies=lambda: {"session": "fixture"},
                                      close_connection=False, responses=[])
            app = {"SESSION_COOKIE_NAME": "session", "load_session": lambda _: ACTOR,
                   "has_module_permission": lambda *_: True,
                   "load_navigation_config": lambda: [],
                   "navigation_item_access": lambda *_: {"allowed": True},
                   "json_response": lambda h, status, body, **kw: h.responses.append((status, body))}
            with patch.object(gateway, "request", side_effect=client.GatewayError("fixture", "test", accepted=accepted)):
                gateway.dispatch(handler, urlparse(client.PREFIX + "/draft"), app)
            self.assertEqual("accepted" in handler.responses[0][1], accepted is False)


class MainIntegrationTests(unittest.TestCase):
    def test_ast_patch_dispatches_only_exact_internal_post_and_public_prefix(self):
        original = '''MODULE_PERMISSIONS = {
}
DEFAULT_USER_PERMISSIONS = {
}
def load_navigation_config():
    with open(NAVIGATION_CONFIG_PATH, "r", encoding="utf-8-sig") as handle:
        return filter_navigation(json.load(handle))
class DramaMaterialHandler:
    def do_GET(self):
        parsed = urlparse(self.path)
        allowed = {
            "/fb-post-ad-delete.js",
        }
        return "old-get"
    def do_POST(self):
        parsed = urlparse(self.path)
        return "old-post"
'''
        changed = patch_app(original)
        ast.parse(changed)
        self.assertEqual(patch_app(changed), changed)
        scope = {"urlparse": urlparse}
        exec(compile(changed, "<isolated-handler>", "exec"), scope)
        handler = scope["DramaMaterialHandler"]()
        with patch.object(query_broker, "dispatch", return_value="broker") as broker, \
                patch.object(gateway, "dispatch", return_value="gateway") as public:
            handler.path = query_broker.PATH
            self.assertEqual(handler.do_POST(), "broker")
            self.assertEqual(handler.do_GET(), "old-get")
            handler.path = query_broker.PATH + "/suffix"
            self.assertEqual(handler.do_POST(), "old-post")
            handler.path = client.PREFIX + "/pages"
            self.assertEqual(handler.do_POST(), "gateway")
            self.assertEqual(handler.do_GET(), "gateway")
            handler.path = client.PREFIX + "-other/pages"
            self.assertEqual(handler.do_GET(), "old-get")
            self.assertEqual(broker.call_count, 1)
            self.assertEqual(public.call_count, 2)
        self.assertIs(scope["DEFAULT_USER_PERMISSIONS"]["fb_video_publish"], False)

    def test_public_nginx_route_denies_internal_exact_endpoint(self):
        config = (ROOT / "deploy/nginx-fb-video-publish.conf").read_text()
        self.assertIn("location = /internal/fb-video-publish/query { return 404; }", config)
        self.assertIn("proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;", config)


if __name__ == "__main__":
    unittest.main()
