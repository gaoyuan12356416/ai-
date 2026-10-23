"""Offline gateway, trusted-source and DTO integration against real core SQLite."""
from __future__ import annotations

import copy
from contextlib import closing
from datetime import datetime, timedelta, timezone
from email.message import Message
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from features.x_posts.service import XPostError
from features.x_video_publish.errors import PublishingError
from features.x_video_publish import gateway, identity_broker, transport
from features.x_video_publish.runtime_source import GatedReadOnlyMySQL
from features.x_video_publish.service import PublishingService, batch_dto
from features.x_video_publish.sources import DEFAULT_PRODUCT, SourceRepository, safe_url
from features.x_video_publish.scheduler import PoolScheduler


ACTOR = {"tenant_key": "fixture-tenant", "user_id": "owner-a", "name": "Fixture owner", "role": "user"}


class FakeSource:
    def __init__(self):
        self.calls = []
        self.materials = {str(n): {"material_id": str(n), "material_name": "Video " + str(n),
            "drama_name": "Fixture drama", "drama_description": "Fixture description", "language": "en",
            "duration_seconds": 45, "preview_url": "https://media.example.invalid/" + str(n) + ".mp4"} for n in range(101, 110)}

    def resolve(self, mids):
        self.calls.append(list(mids))
        return {"items": {mid: copy.deepcopy(self.materials[mid]) for mid in mids if mid in self.materials},
                "errors": {mid: {"code": "material_not_found", "message": "Fixture material absent"} for mid in mids if mid not in self.materials}}

    def listing(self, query):
        return {"items": list(self.materials.values()), "next_cursor": "", "empty_reason": ""}


class FakeOAuth:
    def __init__(self, db_path):
        self.POST_DB_PATH, self.INTERNAL_TOKEN = str(db_path), "offline-fixture-token-no-network-0000"
        self.scope_calls = []
        self.rows = {str(n): {"id": n, "x_user_id": "900000000000000000" + str(n),
            "owner_tenant_key": ACTOR["tenant_key"], "owner_user_id": "owner-a" if n < 3 else "owner-b",
            "username": "fixture" + str(n), "display_name": "Fixture " + str(n),
            "drama_language": "en", "status": "active", "publish_approved": True, "publish_eligible": True,
            "profile_image_url": "https://images.example.invalid/avatar.png"} for n in (1, 2, 3)}

    def require_actor_subject(self, actor):
        if not actor.get("tenant_key") or not actor.get("user_id"):
            raise PublishingError("invalid_actor", "Missing actor", 403)
        return dict(actor)

    def list_accounts(self, actor, scope):
        self.scope_calls.append(scope)
        assert scope in {"mine", "platform"}
        return {"items": [copy.deepcopy(row) for row in self.rows.values()
            if scope == "platform" or (row["owner_tenant_key"], row["owner_user_id"]) == (actor["tenant_key"], actor["user_id"])]}

    def find_scoped_account_row(self, aid, actor, scope):
        self.scope_calls.append(scope)
        assert scope in {"mine", "platform"}
        row = self.rows.get(str(aid))
        if not row or scope != "platform" and (row["owner_tenant_key"], row["owner_user_id"]) != (actor["tenant_key"], actor["user_id"]):
            raise PublishingError("account_not_found", "Not owned", 404)
        return copy.deepcopy(row)

    @staticmethod
    def row_to_item(row):
        return dict(row)


class FakeHandler:
    def __init__(self, method="POST", body=None, headers=None):
        raw = json.dumps(body if body is not None else {}).encode()
        self.command, self.rfile, self.headers = method, io.BytesIO(raw), Message()
        defaults = {"Content-Type": "application/json", "Content-Length": str(len(raw)),
                    "Origin": "https://ai.example.invalid", "Host": "ai.example.invalid", "Sec-Fetch-Site": "same-origin"}
        defaults.update(headers or {})
        for key, value in defaults.items():
            if value is not None:
                self.headers[key] = value
        self.close_connection, self.responses, self.client_address = False, [], ("127.0.0.1", 12345)

    def _cookies(self):
        return {"session": "fixture-session"}

    def _require_same_origin_json(self):
        source = urlsplit(self.headers.get("Origin") or self.headers.get("Referer") or "")
        valid = source.netloc == self.headers.get("Host") and source.scheme == "https"
        if not valid:
            self.responses.append((403, {"error": "same_origin_required"}))
        return valid


class GatewayServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.oauth, self.source = FakeOAuth(root / "posts.sqlite"), FakeSource()
        self.service = PublishingService(self.oauth, source=self.source, identity=lambda t, u: dict(ACTOR), enabled=True)
        self.job_path = root / "jobs.sqlite"
        with closing(sqlite3.connect(self.job_path)) as conn, conn:
            conn.execute("CREATE TABLE drama_admin_user(tenant_key TEXT,user_id TEXT,name TEXT,role TEXT,status TEXT,permissions_json TEXT)")
            conn.execute("INSERT INTO drama_admin_user VALUES(?,?,?,?,?,?)", (ACTOR["tenant_key"], ACTOR["user_id"], ACTOR["name"], "user", "active", '["x_accounts"]'))
        self.nav = {"xVideoPublish": True, "xVideoPublishRecords": True}
        self.session = {**ACTOR, "auth_type": "feishu"}
        self.app = {"JOB_DB_PATH": str(self.job_path), "SESSION_COOKIE_NAME": "session",
            "load_session": lambda token: self.session,
            "normalize_user_permissions": lambda raw, role: json.loads(raw),
            "has_module_permission": lambda user, module: module in user["permissions"],
            "load_navigation_config": lambda: {},
            "navigation_item_access": lambda user, key, config: {"allowed": self.nav.get(key, False)},
            "json_response": lambda handler, status, value, **kw: handler.responses.append((status, value))}

    def tearDown(self):
        self.temp.cleanup()

    def dispatch(self, method, suffix, body=None, headers=None):
        handler = FakeHandler(method, body, headers)
        def trusted_sidecar(m, path, actor, payload, query):
            try:
                return self.service.dispatch(m, path, actor, payload, query)
            except XPostError as exc:
                raise PublishingError(exc.code, str(exc), exc.status) from None
        with patch.object(gateway, "sidecar", trusted_sidecar):
            gateway.dispatch(handler, urlsplit(gateway.PREFIX + suffix), self.app)
        return handler.responses[-1], handler

    def manual(self, schedules=None):
        return {"material_ids": ["101", "102"], "account_ids": ["1", "2"], "pairs": [
            {"material_id": "101", "account_id": "1", "caption_template": "First {name}"},
            {"material_id": "101", "account_id": "2", "caption_template": "Second {name}"},
            {"material_id": "102", "account_id": "1", "caption_template": "Third {name}"}],
            "material_schedules": schedules or {}, "processing_mode": "original"}

    def test_trusted_session_owns_settings_even_with_forged_actor(self):
        result, _ = self.dispatch("POST", "/accounts/1/settings", {"expected_version": 0, "caption_template": "Owned {name}",
            "actor": {"user_id": "owner-b"}, "user_id": "owner-b", "owner_tenant_key": "other", "is_admin": True})
        self.assertEqual(result[0], 200)
        self.assertEqual(self.service.store.get_settings(ACTOR, "1")["default_copy"], "Owned {name}")
        other = {**ACTOR, "user_id": "owner-b"}
        self.assertEqual(self.service.store.settings(other)["items"], [])
        denied, _ = self.dispatch("POST", "/accounts/3/settings", {"expected_version": 0, "caption_template": "Other"})
        self.assertEqual(denied[0], 200)
        self.assertTrue(all(scope == "platform" for scope in self.oauth.scope_calls))

    def test_permission_is_read_fresh_not_from_cookie(self):
        self.assertEqual(self.dispatch("GET", "/accounts")[0][0], 200)
        with closing(sqlite3.connect(self.job_path)) as conn, conn:
            conn.execute("UPDATE drama_admin_user SET permissions_json='[]'")
        denied, _ = self.dispatch("GET", "/accounts")
        self.assertEqual(denied[0], 403)
        self.assertEqual(denied[1]["code"], "identity_permission_denied")

    def test_inactive_identity_and_api_tokens_cannot_access(self):
        self.session["auth_type"] = "api_token"
        self.assertEqual(self.dispatch("GET", "/accounts")[0][0], 401)
        self.session["auth_type"] = "feishu"
        with closing(sqlite3.connect(self.job_path)) as conn, conn:
            conn.execute("UPDATE drama_admin_user SET status='disabled'")
        self.assertEqual(self.dispatch("GET", "/accounts")[0][0], 403)

    def test_platform_permission_does_not_require_separate_publish_navigation(self):
        self.nav["xVideoPublish"] = False
        self.assertEqual(self.dispatch("GET", "/batches")[0][0], 200)
        self.assertEqual(self.dispatch("POST", "/preflight", self.manual())[0][0], 200)

    def test_csrf_missing_origin_cross_site_and_wrong_host_are_rejected(self):
        for headers in ({"Origin": None}, {"Sec-Fetch-Site": "cross-site"}, {"Origin": "https://evil.example.invalid"}, {"Origin": "null"}):
            response, _ = self.dispatch("POST", "/draft", {"draft": {}}, headers)
            self.assertEqual(response[0], 403)
        response, _ = self.dispatch("POST", "/draft", {"draft": {}}, {"Origin": None, "Referer": "https://ai.example.invalid/x-video-publish.html"})
        self.assertEqual(response[0], 200)

    def test_body_framing_transfer_encoding_and_duplicate_lengths_fail(self):
        for mutate in (lambda h: h.headers.add_header("Content-Length", "2"), lambda h: h.headers.add_header("Transfer-Encoding", "chunked")):
            handler = FakeHandler(body={})
            mutate(handler)
            with self.assertRaises(PublishingError) as caught:
                gateway.read_json(handler)
            self.assertEqual(caught.exception.status, 413)
            self.assertEqual(handler.rfile.tell(), 0)

    def test_non_json_nan_and_short_body_fail(self):
        handler = FakeHandler(body={}, headers={"Content-Type": "text/plain"})
        with self.assertRaises(PublishingError):
            gateway.read_json(handler)
        for raw in (b'{"x": NaN}', b'[]', b'{'):
            handler = FakeHandler()
            handler.rfile = io.BytesIO(raw)
            handler.headers.replace_header("Content-Length", str(len(raw)))
            with self.assertRaises(PublishingError):
                gateway.read_json(handler)

    def test_route_and_query_are_exact(self):
        for method, path in (("POST", "/accounts/1/pool/tick"), ("DELETE", "/batches"), ("GET", "/batches/1/extra")):
            with self.assertRaises(PublishingError):
                gateway.route(method, gateway.PREFIX + path)
        for path in ("/materials?q=a&q=b", "/materials?sql=SELECT", "/accounts?owner_user_id=other"):
            self.assertEqual(self.dispatch("GET", path)[0][0], 400)
        self.assertEqual(self.dispatch("GET", "/materials?product=Dramawave")[0][0], 200)
        self.assertEqual(self.dispatch("GET", "/materials?product=Dramawave&product=Other")[0][0], 400)
        self.assertEqual(self.dispatch("GET", "/accounts?product=Dramawave")[0][0], 400)

    def test_manual_three_pairs_commit_idempotently_with_no_platform_write(self):
        response, _ = self.dispatch("POST", "/preflight", self.manual())
        self.assertEqual(response[0], 200)
        preview = response[1]
        self.assertEqual(len(preview["items"]), 3)
        request = {"preview_id": preview["preview_id"], "idempotency_key": "fixture-three-pairs", "allow_repeat": False}
        response, _ = self.dispatch("POST", "/batches", request)
        self.assertEqual(response[0], 200)
        batch = response[1]["batch"]
        self.assertEqual(batch["total_tasks"], 3)
        self.assertEqual({row["status"] for row in batch["items"]}, {"queued"})
        repeat, _ = self.dispatch("POST", "/batches", request)
        self.assertEqual(repeat[1]["batch_id"], batch["batch_id"])
        operation, _ = self.dispatch("GET", "/operations/fixture-three-pairs")
        self.assertEqual(operation[1]["batch_id"], batch["batch_id"])
        with self.service.store._connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM x_video_publish_batch").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM x_post_queue").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM x_post_manual_material_reservation").fetchone()[0], 2)

    def test_manual_invalid_inputs_fail_but_other_accounts_are_allowed(self):
        for change in (lambda p: p["pairs"].pop(), lambda p: p["pairs"][0].update(caption_template="")):
            payload = self.manual()
            change(payload)
            self.assertEqual(self.dispatch("POST", "/preflight", payload)[0][0], 400)
        payload = self.manual()
        payload["account_ids"].append("3")
        payload["pairs"][0]["account_id"] = "3"
        response, _ = self.dispatch("POST", "/preflight", payload)
        self.assertEqual(len(response[1]["skipped"]), 0)
        self.assertEqual(self.dispatch("POST", "/batches", {"preview_id": response[1]["preview_id"], "idempotency_key": "fixture-invalid-manual"})[0][0], 200)

    def test_excel_invalid_processing_and_ids_are_row_errors(self):
        account = self.oauth.rows["1"]["x_user_id"]
        response, _ = self.dispatch("POST", "/excel/preview", {"rows": [
            {"row_number": 2, "x_user_id": account, "material_id": "101", "processing_mode": "original"},
            {"row_number": 3, "x_user_id": account, "material_id": "102", "processing_mode": "unsupported"},
            {"row_number": 4, "x_user_id": int(account), "material_id": "103"},
            {"row_number": 5, "x_user_id": account, "material_id": "1e3"},
            {"row_number": 6, "x_user_id": account, "material_id": "101"}]})
        self.assertEqual(response[0], 200)
        self.assertEqual([r["row_number"] for r in response[1]["items"]], [2])
        self.assertEqual([r["row_number"] for r in response[1]["skipped"]], [3, 4, 5, 6])
        committed, _ = self.dispatch("POST", "/batches", {"preview_id": response[1]["preview_id"], "idempotency_key": "fixture-excel-subset"})
        self.assertEqual(committed[1]["batch"]["total_tasks"], 1)

    def test_excel_all_invalid_and_over_limit_do_not_create_batch(self):
        account = self.oauth.rows["1"]["x_user_id"]
        row = {"x_user_id": account, "material_id": "999"}
        self.assertEqual(self.dispatch("POST", "/excel/preview", {"rows": [row] * 201})[0][0], 400)
        response, _ = self.dispatch("POST", "/excel/preview", {"rows": [row]})
        self.assertEqual(response[1]["items"], [])
        denied, _ = self.dispatch("POST", "/batches", {"preview_id": response[1]["preview_id"], "idempotency_key": "fixture-invalid-excel"})
        self.assertEqual(denied[0], 400)
        self.assertEqual(self.service.store.list_batches(ACTOR)["total"], 0)

    def test_expired_schedule_is_checked_again_at_commit(self):
        future = (datetime.now(timezone.utc) + timedelta(minutes=3)).replace(second=0, microsecond=0)
        response, _ = self.dispatch("POST", "/preflight", self.manual({"101": future.strftime("%Y-%m-%dT%H:%M:00Z")}))
        self.assertEqual(response[0], 200)
        from features.x_video_publish import core
        original_now = core._now
        with patch.object(core, "_now", lambda value=None: future + timedelta(seconds=1) if value is None else original_now(value)):
            denied, _ = self.dispatch("POST", "/batches", {"preview_id": response[1]["preview_id"], "idempotency_key": "fixture-expired-schedule"})
        self.assertEqual(denied[0], 400)
        self.assertEqual(self.service.store.list_batches(ACTOR)["total"], 0)

    def test_changed_account_owner_does_not_revoke_operator_access(self):
        preview = self.dispatch("POST", "/preflight", self.manual())[0][1]
        self.oauth.rows["1"]["owner_user_id"] = "owner-b"
        response, _ = self.dispatch("POST", "/batches", {"preview_id": preview["preview_id"], "idempotency_key": "fixture-owner-drift"})
        self.assertEqual(response[0], 200)
        self.assertEqual(self.service.store.list_batches(ACTOR)["total"], 1)

    def test_account_and_batch_dto_contains_no_owner_or_credentials(self):
        self.oauth.rows["1"].update(access_token="fixture-private", token_store_key="fixture-path")
        response, _ = self.dispatch("GET", "/accounts")
        text = json.dumps(response[1])
        self.assertNotIn("fixture-private", text)
        self.assertNotIn("token_store_key", text)
        self.assertNotIn("owner_user_id", text)
        self.assertEqual({r["account_id"] for r in response[1]["items"]}, {"1", "2", "3"})
        self.assertEqual(batch_dto({"id": 1, "source": "manual", "created_at": "x", "status": "stopped", "tasks": []})["status"], "stopped")

    def test_internal_broker_requires_loopback_single_bearer_no_forwarding(self):
        from features.x_accounts import client
        for extra, remote in (({"X-Forwarded-For": "127.0.0.1"}, False), ({}, True), ({}, False)):
            handler = FakeHandler(body={"tenant_key": ACTOR["tenant_key"], "user_id": ACTOR["user_id"]}, headers=extra)
            handler.headers["Authorization"] = "Bearer " + "x" * 40
            if not extra and not remote:
                handler.headers["Authorization"] = "Bearer " + "x" * 40
            if remote:
                handler.client_address = ("192.0.2.5", 12345)
            with patch.object(client, "_INTERNAL_TOKEN", "x" * 40):
                identity_broker.dispatch(handler, self.app, "identity")
            self.assertEqual(handler.responses[-1][0], 403)

    def test_internal_identity_positive_path_uses_current_database_owner(self):
        from features.x_accounts import client
        handler=FakeHandler(body={"tenant_key":ACTOR["tenant_key"],"user_id":ACTOR["user_id"]})
        handler.headers["Authorization"]="Bearer "+"x"*40
        with patch.object(client,"_INTERNAL_TOKEN","x"*40):
            identity_broker.dispatch(handler,self.app,"identity")
        self.assertEqual(handler.responses[-1][0],200)
        self.assertEqual(handler.responses[-1][1]["actor"]["user_id"],ACTOR["user_id"])

    def test_pool_service_dto_operations_and_disabled_pause(self):
        response,_=self.dispatch("POST","/accounts/1/pool/settings",{"expected_version":0,"times":["09:00"],"processing_mode":"original"})
        self.assertEqual(response[0],200)
        version=response[1]["pool"]["version"]
        proof=self.dispatch("POST","/accounts/1/pool/items/preview",{"expected_version":version,"material_ids":["101","bad"]})[0][1]
        response,_=self.dispatch("POST","/accounts/1/pool/items",{"expected_version":version,"preview_id":proof["preview_id"],"idempotency_key":"fixture-pool-admission"})
        self.assertEqual(response[0],200)
        self.assertEqual(response[1]["items"][0]["material_id"],"101")
        recovered,_=self.dispatch("GET","/accounts/1/pool/operations/fixture-pool-admission")
        self.assertTrue(recovered[1]["idempotent"])
        self.assertEqual(self.dispatch("GET","/accounts/1/pool/operations/missing-operation")[0][0],404)
        self.service.enabled=False
        response,_=self.dispatch("POST","/accounts/1/pool/pause",{"expected_version":response[1]["pool"]["version"]})
        self.assertEqual(response[0],200)
        self.assertFalse(response[1]["pool"]["enabled"])

    def test_scheduler_classifies_broker_and_oauth_callback_errors(self):
        for kind in ("identity","source","account"):
            with self.subTest(kind=kind):
                # Each subcase owns a fresh DB to preserve all prior evidence.
                oauth=FakeOAuth(Path(self.temp.name)/(kind+".sqlite"))
                source=FakeSource()
                service=PublishingService(oauth,source=source,identity=lambda t,u:dict(ACTOR),enabled=True)
                pool=service._pool()
                clock=[datetime(2030,1,1,0,0,tzinfo=timezone.utc)]
                pool.now_fn=lambda:clock[0]
                version=pool.settings(ACTOR,"1",{"expected_version":0,"times":["08:01"],"processing_mode":"original"})["pool"]["version"]
                proof=pool.preview(ACTOR,"1",{"expected_version":version,"material_ids":["101"]})
                added=pool.add(ACTOR,"1",{"expected_version":version,"preview_id":proof["preview_id"],"idempotency_key":"fixture-callback-error"})
                pool.toggle(ACTOR,"1",{"expected_version":added["pool"]["version"]},True)
                scheduler=PoolScheduler(pool)
                scheduler.start("fixture-worker")
                def failure(*args):
                    raise PublishingError("identity_permission_denied" if kind=="identity" else "source_busy" if kind=="source" else "x_account_not_found","Fixture denied",403 if kind=="identity" else 503 if kind=="source" else 404)
                if kind=="identity":service.identity=failure
                elif kind=="source":source.resolve=failure
                else:oauth.find_scoped_account_row=failure
                clock[0]+=timedelta(minutes=1,seconds=1)
                result=scheduler.tick("fixture-worker")
                self.assertEqual(result["results"][0]["status"],"skipped" if kind=="source" else "blocked")
                with pool.connection() as conn:
                    self.assertEqual(conn.execute("SELECT status FROM x_video_publish_pool_item").fetchone()[0],"pending")
                    self.assertEqual(conn.execute("SELECT status FROM x_video_publish_pool").fetchone()[0],"enabled" if kind=="source" else "paused")
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM x_video_publish_task").fetchone()[0],0)


class FakeMySQL:
    schema = "fixture_schema"
    def __init__(self, count=3):
        self.calls, self.resource_count, self.deploy_count = [], 1, 1
        self.exact_titles = []
        self.sources = [{"material_id": str(101 + i), "product": DEFAULT_PRODUCT, "material_type": 2,
            "material_is_deleted": 0, "material_url": "https://media.example.invalid/" + str(i) + ".mp4",
            "material_name": "Fixture video", "material_language": "en", "content_id": "fixture-content",
            "source_tag_name": "fixture", "video_duration": 45, "thumbnail_url": "https://images.example.invalid/1.png"} for i in range(count)]

    def source_rows(self, sql, params):
        if "WHERE s.id IN" in sql:
            return [dict(r) for r in self.sources if r["material_id"] in params]
        rows = copy.deepcopy(self.sources)
        if 's.product=%s' in sql:
            product = params[sql.split('s.product=%s')[0].count('%s')]
            rows = [r for r in rows if r['product'] == product]
        if 's.id=%s' in sql:
            mid = params[sql.split('s.id=%s')[0].count('%s')]
            rows = [r for r in rows if r['material_id'] == mid]
        if 's.id<%s' in sql:
            cursor = params[sql.split('s.id<%s')[0].count('%s')]
            rows = [r for r in rows if int(r['material_id']) < int(cursor)]
        if 's.data_source_id IN (' in sql:
            before, after = sql.split('s.data_source_id IN (', 1)
            start = before.count('%s')
            count = after.split(')', 1)[0].count('%s')
            contents = params[start:start+count]
            rows = [r for r in rows if r['content_id'] in contents]
        return rows

    def resource_rows(self, sql):
        return [{"content_id": "fixture-content", "series_code": "fixture-series", "language": "en",
                     "drama_name": "Fixture drama", "drama_labels": "Romance", "drama_description": "Fixture description"} for _ in range(self.resource_count)]

    def deployment_rows(self):
        return [{"content_id": "fixture-content", "app_id": 1479, "app": "fixture-app", "language": "en", "deploy_time": 0} for _ in range(self.deploy_count)]

    def select(self, sql, params):
        self.calls.append((sql, list(params)))
        if sql.startswith('SELECT bundle.row_kind'):
            source_sql = sql.split('UNION ALL')[0]
            rows = self.source_rows(source_sql, params[:source_sql.count('%s')])
            records = [('material', row) for row in rows]
            if any(r.get('content_id') for r in rows):
                records += [('drama', row) for row in self.resource_rows(sql)]
                records += [('deploy', row) for row in self.deployment_rows()]
            return [{'row_kind': kind, 'row_json': json.dumps(row)} for kind, row in records]
        if 'SELECT DISTINCT r.content_id' in sql:
            return copy.deepcopy(self.exact_titles)
        if "FROM `fixture_schema`.ads_custom_source" in sql:
            return self.source_rows(sql, params)
        if "FROM `fixture_schema`.ads_drama_resource" in sql:
            return self.resource_rows(sql)
        if "FROM `fixture_schema`.ads_drama_info" in sql:
            return self.deployment_rows()
        raise AssertionError("Unexpected source query")


class SourceBoundaryTests(unittest.TestCase):
    def test_complete_drama_title_uses_content_index_and_empty_language_does_not_fallback(self):
        mysql = FakeMySQL()
        mysql.exact_titles = [{'content_id': 'fixture-content'}]
        result = SourceRepository(mysql).listing({'q': 'Fixture drama', 'language': 'ja'})
        self.assertEqual(result['items'], [])
        self.assertEqual(len(mysql.calls), 4)
        title_sql, title_params = mysql.calls[0]
        self.assertIn('FORCE INDEX(name)', title_sql)
        self.assertIn('FORCE INDEX(scoo)', title_sql)
        self.assertIn('i.series_code=title_series.series_code', title_sql)
        self.assertIn("TRIM(r.series_code) NOT IN ('','0')", title_sql)
        self.assertIn('i.app_id=1479', title_sql)
        self.assertEqual(title_params, ['Fixture drama', 'Fixture drama'])
        sql, params = mysql.calls[1]
        self.assertIn('STRAIGHT_JOIN', sql)
        self.assertIn('s.data_source_id IN (%s)', sql)
        self.assertIn('s.product=%s', sql)
        self.assertIn('s.data_source <=> source_kinds.data_source', sql)
        self.assertNotIn('LOCATE', sql)
        self.assertIn('fixture-content', params)
        self.assertIn('ja', params)
        mysql.calls.clear()
        result = SourceRepository(mysql).listing({'q': 'Fixture drama', 'language': 'en'})
        self.assertEqual(len(result['items']), 3)
        mysql.exact_titles *= 101
        with self.assertRaises(PublishingError) as caught:
            SourceRepository(mysql).listing({'q': 'Fixture drama'})
        self.assertEqual(caught.exception.code, 'source_mapping_too_large')

    def test_title_returns_translations_but_keeps_language_product_and_cursor_filters(self):
        class TranslatedMySQL(FakeMySQL):
            def resource_rows(self, sql):
                return [{"content_id": r['content_id'], "series_code": "same-series",
                         "language": r['material_language'], "drama_name": "Localized title " + r['material_language'],
                         "drama_labels": "Romance", "drama_description": "Description " + r['material_language']}
                        for r in self.sources]

            def deployment_rows(self):
                return [{"content_id": r['content_id'], "app_id": 1479, "app": "fixture-app",
                         "language": r['material_language'], "deploy_time": 0} for r in self.sources]

        mysql = TranslatedMySQL(count=5)
        for row, language in zip(mysql.sources, ['en', 'ko', 'ja', 'zh-TW', 'en']):
            row.update(content_id='content-' + row['material_id'], material_language=language)
        mysql.exact_titles = [{'content_id': r['content_id']} for r in mysql.sources[:4]]
        source = SourceRepository(mysql)
        query = {'q': 'Original English title'}
        self.assertEqual([r['material_id'] for r in source.listing(query)['items']], ['101','102','103','104'])
        self.assertEqual([r['material_id'] for r in source.listing({**query,'language':'ja'})['items']], ['103'])
        self.assertEqual([r['material_id'] for r in source.listing({**query,'language':'ko'})['items']], ['102'])
        self.assertEqual(source.listing({**query,'language':'de'})['items'], [])
        self.assertEqual([r['material_id'] for r in source.listing({**query,'cursor':'103'})['items']], ['101','102'])
        mysql.sources[1]['product'] = 'Dramawave'
        self.assertEqual([r['material_id'] for r in source.listing(query)['items']], ['101','103','104'])
        self.assertEqual([r['material_id'] for r in source.listing({**query,'product':'Dramawave'})['items']], ['102'])

    def test_catalog_products_and_cross_product_exact_ids(self):
        mysql = FakeMySQL()
        mysql.sources[1]['product'] = 'Dramawave'
        mysql.sources[2]['product'] = 'Other'
        source = SourceRepository(mysql)
        self.assertEqual([r['material_id'] for r in source.listing({})['items']], ['101'])
        self.assertEqual([r['material_id'] for r in source.listing({'product': 'Dramawave', 'q': 'Fixture'})['items']], ['102'])
        self.assertEqual([r['material_id'] for r in source.listing({'product': 'Dramawave', 'q': '103'})['items']], ['103'])
        self.assertEqual([r['material_id'] for r in source.listing({'q': '102'})['items']], ['102'])
        self.assertEqual(set(source.resolve(['101', '102', '103'])['items']), {'101', '102', '103'})
        for sql, params in mysql.calls:
            if 'ads_custom_source s' in sql:
                self.assertNotIn('FORCE INDEX(PRIMARY)', sql)
                if 's.id=%s' in sql or 's.id IN' in sql:
                    self.assertNotIn('s.product=%s', sql)
        with self.assertRaises(PublishingError) as caught:
            source.listing({'product': 'Other'})
        self.assertEqual(caught.exception.code, 'invalid_query')

    def test_product_scope_ignores_foreign_description_but_rejects_real_conflicts(self):
        class MixedProducts(FakeMySQL):
            conflict = False
            def resource_rows(self, sql):
                rows = super().resource_rows(sql)
                foreign = {**rows[0], "drama_description": "Other product copy"}
                # Simulate SQL scope, not a permissive fallback to the first row.
                if self.conflict or "r.app_id=1479" not in sql:
                    rows.append(foreign)
                return rows
        mysql = MixedProducts()
        result = SourceRepository(mysql).resolve(["101"])
        self.assertIn("101", result["items"])
        mysql.conflict = True
        result = SourceRepository(mysql).resolve(["101"])
        self.assertEqual(result["items"], {})
        self.assertEqual(result["errors"]["101"]["code"], "drama_mapping_ambiguous")
        self.assertIn("Dramawave", result["errors"]["101"]["message"])

    def test_language_prefilter_still_requires_resolved_drama_language(self):
        mysql = FakeMySQL()
        result = SourceRepository(mysql).listing({"language": "ja", "limit": 2})
        self.assertEqual(result["items"], [])  # authoritative fixture language is en
        self.assertEqual(result["next_cursor"], "102")
        sql, _ = mysql.calls[0]
        self.assertIn("ads_drama_info i FORCE INDEX(ac)", sql)
        self.assertIn("i.app_id=1479", sql)
        resource_sql = mysql.calls[1][0]
        self.assertIn("r.app_id=1479", resource_sql)
        self.assertIn("FORCE INDEX(content_id)", resource_sql)

    def test_exact_reads_use_one_query_and_browse_keeps_bounded_queries(self):
        mysql = FakeMySQL()
        source = SourceRepository(mysql)
        result = source.resolve(["101", "102", "103"])
        self.assertEqual(len(result["items"]), 3)
        self.assertEqual(len(mysql.calls), 1)
        self.assertTrue(all("LIMIT" in sql for sql, _ in mysql.calls))
        sql, params = mysql.calls[0]
        self.assertEqual(sql.count('UNION ALL'), 2)
        self.assertEqual(sql.count('LIMIT 4001'), 2)
        self.assertEqual(sql.count('SELECT DISTINCT TRIM(s.data_source_id)'), 2)
        self.assertEqual(params, ['101','102','103']*3)
        self.assertIn('r.app_id=1479',sql)
        self.assertIn('i.app_id=1479',sql)
        mysql.calls.clear()
        self.assertEqual(len(source.listing({'q':'101'})['items']),1)
        self.assertEqual(len(mysql.calls),1)
        mysql.calls.clear()
        self.assertEqual(source.listing({'q':'101','cursor':'100'})['items'],[])
        self.assertEqual(len(mysql.calls),1)
        mysql.calls.clear()
        listing = source.listing({"limit": "2"})
        self.assertEqual(len(listing["items"]), 2)
        self.assertEqual(listing["next_cursor"], "102")
        self.assertEqual(len(mysql.calls), 3)

    def test_single_query_keeps_unicode_nulls_and_rejects_bad_bundle(self):
        mysql = FakeMySQL()
        mysql.sources[0].update(material_name='日本語视频😀', thumbnail_url=None)
        result = SourceRepository(mysql).listing({'q':'101'})
        self.assertEqual(result['items'][0]['material_name'],'日本語视频😀')
        self.assertEqual(result['items'][0]['thumbnail_url'],'')
        for record in ({'row_kind':'unknown','row_json':'{}'}, {'row_kind':'material','row_json':'[]'},
                       {'row_kind':'material','row_json':'private invalid JSON'}):
            with patch.object(mysql,'select',return_value=[record]), self.assertRaises(PublishingError) as error:
                SourceRepository(mysql).resolve(['101'])
            self.assertEqual(error.exception.code,'source_result_invalid')
            self.assertNotIn('private',str(error.exception))

    def test_mapping_limit_overflow_fails_closed_before_selector(self):
        for which in ("resource_count", "deploy_count"):
            mysql = FakeMySQL()
            setattr(mysql, which, 4001)
            with self.assertRaises(PublishingError) as caught:
                SourceRepository(mysql).resolve(["101"])
            self.assertEqual(caught.exception.code, "source_mapping_too_large")
            self.assertLessEqual(len(mysql.calls), 3)

    def test_exact_limit_is_complete_and_still_validated(self):
        mysql = FakeMySQL()
        mysql.resource_count = 4000
        result = SourceRepository(mysql).resolve(["101"])
        self.assertIn("101", result["items"])
        self.assertTrue(any("LIMIT 4001" in sql for sql, _ in mysql.calls))

    def test_query_is_parameterized_and_numeric_search_is_exact(self):
        mysql = FakeMySQL(0)
        source = SourceRepository(mysql)
        source.listing({"q": "9000000000000000001", "limit": "50", "language": "jp"})
        sql, params = mysql.calls[0]
        self.assertIn("s.id=%s", sql)
        self.assertIn("9000000000000000001", params)
        self.assertNotIn("9000000000000000001", sql)
        self.assertIn("ja", params)
        self.assertIn("jp", params)
        for bad in ({"cursor": "1 OR 1=1"}, {"q": "x" * 81}, {"language": "x' OR 1=1"}):
            with self.assertRaises(PublishingError):
                source.listing(bad)
        with self.assertRaises(PublishingError):
            source.resolve(["1e3"])

    def test_browsing_missing_duration_never_probes_or_downloads_media(self):
        mysql = FakeMySQL()
        mysql.sources[0]["video_duration"] = 0
        with patch("features.x_posts.selector.probe_source_duration", side_effect=AssertionError("No media download allowed")), patch(
                "features.x_video_publish.sources.probe_source_metadata_duration", side_effect=AssertionError("No bulk probing")):
            result = SourceRepository(mysql).listing({})
        self.assertNotIn("101", [r['material_id'] for r in result['items']])

    def test_exact_id_and_preflight_recover_real_duration_with_https_and_same_gates(self):
        mysql = FakeMySQL()
        mysql.sources[0].update(video_duration=0, material_url='http://media.example.invalid/0.mp4')
        with patch("features.x_posts.selector.probe_source_duration", side_effect=AssertionError("No full download allowed")), patch(
                "features.x_video_publish.sources.probe_source_metadata_duration", return_value=1792.934) as probe:
            source = SourceRepository(mysql)
            result = source.listing({'q':'101','product':'Dramawave'})
            self.assertEqual(result['items'][0]['duration_seconds'],1792.934)
            self.assertEqual(result['items'][0]['preview_url'],'https://media.example.invalid/0.mp4')
            self.assertEqual(source.resolve(['101'])['items']['101']['duration_seconds'],1792.934)
            self.assertEqual(probe.call_args.args[0], 'https://media.example.invalid/0.mp4')
            self.assertIn('deadline',probe.call_args.kwargs)
            probe.return_value=14401
            result=source.resolve(['101'])
            self.assertEqual(result['errors']['101']['code'],'material_duration_exceeds_limit')
        self.assertEqual(mysql.sources[0]['video_duration'],0)

    def test_exact_probe_failure_is_local_redacted_and_batch_has_one_deadline(self):
        mysql=FakeMySQL()
        for row in mysql.sources[:2]:row['video_duration']=0
        with patch("features.x_posts.selector.probe_source_duration", side_effect=AssertionError("No full download allowed")), patch(
                "features.x_video_publish.sources.probe_source_metadata_duration", side_effect=ValueError('private signed URL')) as probe:
            result=SourceRepository(mysql).resolve(['101','102','103'])
        self.assertEqual(set(result['items']),{'103'})
        self.assertEqual(result['errors']['101']['code'],'material_duration_missing')
        self.assertNotIn('private',str(result))
        self.assertEqual(len({c.kwargs['deadline'] for c in probe.call_args_list}),1)

    def test_safe_url_and_sql_binding_reject_unsafe_values(self):
        for url in ("javascript:alert(1)", "http://example.invalid/x", "https://user:pass@example.invalid/x", "https://example.invalid:444/x"):
            self.assertEqual(safe_url(url), "")
        text = "quote' ; SELECT secret -- \\ 中文"
        bound = GatedReadOnlyMySQL._bind("SELECT value FROM fixture WHERE name=%s", [text])
        self.assertNotIn(text, bound)
        self.assertIn(text.encode().hex(), bound)
        for sql in ("UPDATE fixture SET x=1", "SELECT 1; SELECT 2", "SELECT secret INTO OUTFILE 'x'"):
            with self.assertRaises(PublishingError):
                GatedReadOnlyMySQL._bind(sql, [])

    def test_mysql_requires_verified_read_only_result(self):
        def output(flag):
            return ('<resultset><row><field name="replica_read_only">' + flag + '</field></row></resultset>'
                    '<resultset><row><field name="material_id">9000000000000000001</field></row></resultset>').encode()
        self.assertEqual(GatedReadOnlyMySQL._decode(output("1"))[0]["material_id"], "9000000000000000001")
        with self.assertRaises(PublishingError):
            GatedReadOnlyMySQL._decode(output("0"))

    def test_loopback_transport_rejects_redirect_and_preserves_unknown_acceptance(self):
        self.assertIsNone(transport.NoRedirect().redirect_request(None, None, None, None, None, None))
        with self.assertRaises(PublishingError):
            transport.request("https://outside.example.invalid/api", "x" * 40, {})
        for status, accepted in ((409, False), (503, None)):
            error = urllib.error.HTTPError("http://127.0.0.1:1234/fixture", status, "fixture", {}, io.BytesIO(b'{"code":"fixture_error","message":"Fixture failure"}'))
            with patch.object(transport.OPENER, "open", side_effect=error):
                with self.assertRaises(PublishingError) as caught:
                    transport.request("http://127.0.0.1:1234/fixture", "x" * 40, {})
            self.assertIs(caught.exception.accepted, accepted)

    def test_mysql_uses_fifo_wrapper_and_never_bypass_or_cli_password(self):
        from features.x_video_publish import runtime_source
        class Process:
            returncode=0
            def communicate(self,timeout=None):
                return (b'<resultset><row><field name="replica_read_only">1</field></row></resultset><resultset/>',b'')
            def poll(self):return 0
        adapter=GatedReadOnlyMySQL({"X_VIDEO_PUBLISH_MYSQL_USER":"fixture_user","X_VIDEO_PUBLISH_MYSQL_PASSWORD":"fixture-private-password"})
        with patch.dict(os.environ,{"SQL_GATE_BYPASS":"1"}),patch.object(runtime_source.subprocess,"Popen",return_value=Process()) as start:
            self.assertEqual(adapter.select("SELECT material_id FROM fixture WHERE id=%s",["101"]),[])
        args,kwargs=start.call_args
        self.assertEqual(args[0][0],"/usr/bin/mysql")
        self.assertNotIn("fixture-private-password",repr(args[0]))
        self.assertNotIn("SQL_GATE_BYPASS",kwargs["env"])
        self.assertIn("START TRANSACTION READ ONLY",args[0][-1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
