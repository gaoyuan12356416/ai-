"""Offline contracts: no live SQL, network, credentials or publisher writes."""
import json
import os
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from unittest.mock import MagicMock, Mock, patch

from features.fb_video_publish.links import BASE_URL, build_attribution_url, publish_link, short_url
from features.fb_video_publish.sources import BrokerReadOnlyMySQL, GatedReadOnlyMySQL, SourceError, SourceRepository
from features.fb_video_publish.templates import TemplateError, render_preview, render_template, validate_template


def context(**changes):
    result = dict(page_id="123456", page_name="剧场 A", material_id="42", material_name="素材 A",
                  content_id="内容42", language="zh-tw", drama_name="剧名 雨后", tag="爱情",
                  sub_user_id="803", task_id="a" * 32)
    return dict(result, **changes)


def raw(mid="42", **changes):
    return dict(dict(material_id=mid, content_id="content-" + mid, material_name="Video " + mid,
                     material_tag="", language="en", video_duration=32,
                     media_url="http://advertising-1306474899.cos.ap-hongkong.myqcloud.com/" + mid + ".mp4",
                     thumbnail_url="", uploader_id="789"), **changes)


class FakeMySQL:
    schema = "kunlunads_dev"
    blacklist_schema = "ads_setting"

    def __init__(self, batches=None):
        self.calls, self.batches, self.last_rows = [], list(batches or []), []
        self.bad_desc, self.bad_name = False, False

    def select(self, sql, params):
        self.calls.append((sql, list(params)))
        if "ads_custom_source" in sql:
            self.last_rows = self.batches.pop(0)
            return self.last_rows
        if "ads_drama_resource" in sql:
            rows = [dict(content_id=r["content_id"], language=r["language"], drama_description="Description")
                    for r in self.last_rows]
            if self.bad_desc and rows:
                rows.append(dict(rows[0], drama_description="Conflicting description"))
            return rows
        rows = [dict(content_id=r["content_id"], language=r["language"], drama_name="Drama")
                for r in self.last_rows]
        if self.bad_name and rows:
            rows.append(dict(rows[0], drama_name="Conflicting drama"))
        return rows


class FakePages:
    def __init__(self):
        self.calls = []
        self.legacy = []
        self.rows = [SimpleNamespace(page_id="123456", page_name="DramaWave EN", language="English",
                                     group_ids=("1", "2"), eligible_token_count=1),
                     SimpleNamespace(page_id="123457", page_name="DramaWave CN", language="zh-tw",
                                     group_ids=("1",), eligible_token_count=0)]

    def list_groups(self, **scope):
        self.calls.append(("groups", scope))
        return [SimpleNamespace(group_id="1", app_id="1479", product="Dramawave"),
                SimpleNamespace(group_id="2", app_id="1479", product="Dramawave"),
                SimpleNamespace(group_id="9", app_id="999", product="Other")]

    def list_pages(self, ids, **scope):
        self.calls.append(("pages", ids, scope))
        return self.rows

    def legacy_conflicts(self, ids):
        self.calls.append(("legacy", ids))
        return self.legacy


def source(batches=None):
    mysql, pages = FakeMySQL(batches), FakePages()
    return SourceRepository(pages, SimpleNamespace(mysql=mysql),
                            now_fn=lambda: datetime(2026, 10, 20, tzinfo=timezone.utc)), mysql, pages


class MacroTests(unittest.TestCase):
    def test_strict_braces_and_supported_tokens(self):
        for text in ("", " ", "{title}", "{{name}}", "{name", "name}", "{ name }", "{{name}", "{url}}"):
            with self.subTest(text=text), self.assertRaises(TemplateError):
                validate_template(text)
        self.assertEqual(validate_template("  {name}\n{desc}\n{url}  "), "{name}\n{desc}\n{url}")

    def test_source_values_are_expanded_once(self):
        result = render_template("{name} {desc} {url}", {"drama_name": "{url}", "drama_description": "{unknown}"},
                                 short_url(42))
        self.assertEqual(result, "{url} {unknown} https://gy.g2flow.com/s2l/facebook/42.html")

    def test_missing_values_and_wrong_short_namespace(self):
        for template in ("{name}", "{desc}", "{url}"):
            with self.subTest(template=template), self.assertRaises(TemplateError):
                render_template(template, {}, "")
        with self.assertRaises(TemplateError):
            render_template("{url}", {}, "https://gy.g2flow.com/s2l/youtube/1.html")
        self.assertEqual(render_template("Plain text", {}), "Plain text")

    def test_utf8_limit_uses_final_real_short_link(self):
        url = short_url(9223372036854775807)
        accepted = "x" * (5000 - len(url.encode())) + "{url}"
        self.assertEqual(len(render_template(accepted, {}, url).encode()), 5000)
        with self.assertRaises(TemplateError):
            render_template("中" + accepted, {}, url)
        with self.assertRaises(TemplateError):
            validate_template("x" * 16001)

    def test_controls_and_unpaired_unicode(self):
        for value in ("Hi\x00", "Hi\ud800"):
            with self.assertRaises(TemplateError):
                validate_template(value)

    def test_preview_uses_placeholder_without_relaxing_real_urls(self):
        self.assertEqual(render_preview("Link {url}", {}), "Link [提交后生成独立短链]")
        with self.assertRaises(TemplateError):
            render_template("Link {url}", {}, "[提交后生成独立短链]")
        with self.assertRaises(TemplateError):
            render_preview("x" * 4950 + "{url}", {})


class LinkTests(unittest.TestCase):
    def test_exact_2310_eight_fields_utf8_and_creation_seconds(self):
        url = build_attribution_url(context(), 123, "2026-10-20T08:00:00+08:00")
        self.assertEqual(url.split("?")[0], BASE_URL)
        self.assertIn("%E5%89%A7", url)
        self.assertIn("%20", url)
        self.assertNotIn("+", url)
        self.assertEqual(len(parse_qs(urlsplit(url).query)), 8)
        query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        stamp = int(datetime(2026, 10, 20, tzinfo=timezone.utc).timestamp())
        self.assertEqual(query["c"], f"yingliang_post_CLV_VL_facebook_123456*{stamp}nonezh-tw*剧名 雨后*爱情*123")
        self.assertEqual(query["af_ad"], "素材 A_contentid[内容42]")
        self.assertEqual(query["af_channel"], "803")
        self.assertEqual(query["af_c_id"], "a" * 32)
        self.assertEqual(query["af_ad_id"], "42")
        self.assertEqual(query["af_adset_id"], "123456")
        self.assertEqual(query["af_dp"], "内容42")

    def test_invalid_mapping_identity_time_and_campaign_separator(self):
        for change in (dict(sub_user_id="0"), dict(sub_user_id="803,804"), dict(page_id="abc"),
                       dict(task_id="1"), dict(drama_name="Drama*Other"), dict(tag="tag*bad")):
            with self.subTest(change=change), self.assertRaises(TemplateError):
                build_attribution_url(context(**change), 1, "2026-10-20T00:00:00Z")
        for timestamp in ("2026-10-20", "invalid", "2026-10-20T00:00:00"):
            with self.assertRaises(TemplateError):
                build_attribution_url(context(), 1, timestamp)

    def test_independent_pair_identity_and_none_tag(self):
        first = build_attribution_url(context(tag=""), 1, "2026-10-20T00:00:00Z")
        second = build_attribution_url(context(page_id="9999", task_id="b" * 32), 2, "2026-10-20T00:00:00Z")
        self.assertIn("*none*1", first)
        self.assertNotEqual(first, second)
        self.assertNotEqual(short_url(1), short_url(2))

    def test_immutable_publish_readback_and_retry(self):
        first = build_attribution_url(context(), 123, "2026-10-20T00:00:00Z")
        with tempfile.TemporaryDirectory() as root:
            result = publish_link(root, 123, first)
            self.assertFalse(result["reused"])
            self.assertTrue(publish_link(root, 123, first)["reused"])
            self.assertEqual(result["short_url"], short_url(123))
            data = Path(root, "123.html").read_bytes()
            self.assertIn(b"fbclid", data)
            altered = build_attribution_url(context(page_name="Changed"), 123, "2026-10-20T00:00:00Z")
            with self.assertRaises(Exception) as raised:
                publish_link(root, 123, altered)
            self.assertEqual(raised.exception.code, "short_link_immutable_conflict")
            self.assertEqual(Path(root, "123.html").read_bytes(), data)

    def test_malformed_long_link_cannot_create_file(self):
        original = build_attribution_url(context(), 123, "2026-10-20T00:00:00Z")
        with tempfile.TemporaryDirectory() as root:
            for url, record in ((original.replace("/2310/", "/2284/"), 123), (original, 124),
                                (original + "&af_dp=other", 123), (original + "#other", 123)):
                with self.assertRaises(TemplateError):
                    publish_link(root, record, url)
            self.assertEqual(list(Path(root).iterdir()), [])

    def test_concurrent_identical_link_reuses_one_file(self):
        url = build_attribution_url(context(), 123, "2026-10-20T00:00:00Z")
        with tempfile.TemporaryDirectory() as root:
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda _: publish_link(root, 123, url), range(8)))
            self.assertEqual(sum(not result["reused"] for result in results), 1)
            self.assertEqual(len({result["sha256"] for result in results}), 1)
            self.assertEqual([item.name for item in Path(root).iterdir()], ["123.html"])


class SourceTests(unittest.TestCase):
    actor = dict(tenant_key="tenant", user_id="feishu", business_owner_id="789", is_admin=False)

    def test_owner_scope_product_and_disabled_page_visibility(self):
        repo, _, pages = source()
        items = repo.list_pages(self.actor)
        self.assertEqual(pages.calls[1][1], ["1", "2"])
        self.assertEqual(pages.calls[1][2], {"is_admin": False, "owner_user_id": "789"})
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["language"], "en")
        self.assertTrue(items[1]["blocked_reason"])
        with self.assertRaises(SourceError):
            repo.get_pages(self.actor, ["123457"])
        with self.assertRaises(SourceError):
            repo.get_pages(self.actor, ["999999"])

    def test_legacy_overlap_does_not_block_manual_page_and_no_token_fields(self):
        repo, _, pages = source()
        pages.legacy = [{"overlap_page_id": "123456"}]
        item = repo.list_pages(self.actor, "EN")[0]
        self.assertFalse(item["legacy_conflict"])
        self.assertEqual(item["blocked_reason"], "")
        self.assertFalse(set(item) & {"token", "page_access_token", "access_token", "credentials"})
        self.assertEqual(repo.get_pages(self.actor, ["123456"])["123456"], item)
        self.assertFalse(any(call[0] == "legacy" for call in pages.calls))

    def test_manual_page_access_does_not_depend_on_legacy_queue_availability(self):
        repo, _, pages = source()
        pages.legacy_conflicts = Mock(side_effect=RuntimeError("legacy queue unavailable"))
        self.assertEqual(repo.get_pages(self.actor, ["123456"])["123456"]["blocked_reason"], "")
        with self.assertRaises(SourceError) as denied:
            repo.get_pages(self.actor, ["123457"])
        self.assertEqual(denied.exception.code, "page_unavailable")
        pages.legacy_conflicts.assert_not_called()

    def test_batch_fresh_checks_have_no_n_plus_one(self):
        rows = [raw(str(mid)) for mid in range(100, 0, -1)]
        repo, mysql, _ = source([rows])
        result = repo.get_materials([str(mid) for mid in range(1, 101)], need_desc=True)
        self.assertEqual(len(result), 100)
        self.assertEqual(list(result)[0], "1")
        self.assertEqual(len(mysql.calls), 3)
        self.assertTrue(result["1"]["media_url"].startswith("https://"))
        self.assertEqual(result["1"]["drama_description"], "Description")
        sql, params = mysql.calls[0]
        for gate in ("s.data_source=%s", "s.type=2", "s.is_delete=0", "d.release_status=1",
                     "d.deploy_time<=%s", "b.type=0", "b.type=1", "s.id IN"):
            self.assertIn(gate, sql)
        self.assertEqual(params[:3], ["6", "Dramawave", "1479"])

    def test_200_materials_are_split_into_two_bounded_batches(self):
        rows = [[raw(str(mid)) for mid in range(100, 0, -1)],
                [raw(str(mid)) for mid in range(200, 100, -1)]]
        repo, mysql, _ = source(rows)
        self.assertEqual(len(repo.get_materials([str(mid) for mid in range(1, 201)])), 200)
        self.assertEqual(len(mysql.calls), 6)
        with self.assertRaises(SourceError):
            repo.get_materials([str(mid) for mid in range(1, 202)])

    def test_unavailable_and_duplicate_source_fail_closed(self):
        for rows in ([], [raw("1"), raw("1")]):
            repo, _, _ = source([rows])
            with self.assertRaises(SourceError):
                repo.get_materials(["1"])
        repo, _, _ = source()
        with self.assertRaises(SourceError):
            repo.get_materials(["1", "1"])

    def test_media_validation_missing_duration_never_probes(self):
        bad = [dict(video_duration=0), dict(video_duration="nan"), dict(video_duration=3601),
               dict(media_url="https://example.com/test.mp4"),
               dict(media_url="http://127.0.0.1/test.mp4"),
               dict(media_url="https://advertising-1306474899.cos.ap-hongkong.myqcloud.com/a.mp4#bad")]
        for change in bad:
            repo, mysql, _ = source([[raw(**change)]])
            with self.subTest(change=change), self.assertRaises(SourceError):
                repo.get_materials(["42"])
            self.assertEqual(len(mysql.calls), 1)

    def test_approved_cdn_https_only(self):
        repo, _, _ = source([[raw(media_url="https://socialkit-cdn.yingliang.tech/videos/test.mp4")]])
        self.assertIn("socialkit-cdn.yingliang.tech", repo.get_materials(["42"])["42"]["media_url"])
        repo, _, _ = source([[raw(media_url="http://socialkit-cdn.yingliang.tech/videos/test.mp4")]])
        with self.assertRaises(SourceError):
            repo.get_materials(["42"])

    def test_keyset_filters_precede_limit_and_unsafe_rows_advance(self):
        repo, mysql, _ = source([[raw("5", media_url="https://example.com/bad.mp4"), raw("4"), raw("3")],
                                 [raw("3"), raw("2")]])
        first = repo.search_materials(dict(search="Video", language="English", uploader_id="789", limit=2))
        self.assertEqual([m["material_id"] for m in first["items"]], ["4"])
        self.assertTrue(first["has_more"])
        sql, params = mysql.calls[0]
        self.assertLess(sql.index("LOCATE"), sql.index("LIMIT"))
        self.assertIn("en", params)
        second = repo.search_materials(dict(search="Video", language="en", uploader_id="789",
                                            limit=2, cursor=first["next_cursor"]))
        self.assertEqual([m["material_id"] for m in second["items"]], ["3", "2"])
        self.assertFalse(second["has_more"])
        query, params = mysql.calls[3]
        self.assertIn("s.id<%s", query)
        self.assertIn("s.id<=%s", query)
        self.assertEqual(params[-3:-1], ["4", "5"])
        with self.assertRaises(SourceError):
            repo.search_materials(dict(search="different", cursor=first["next_cursor"]))

    def test_drama_title_search_matches_published_same_language_before_paging(self):
        repo, mysql, _ = source([[raw()]])
        repo.search_materials(dict(search="Only Drama Title", language="en", limit=1))
        sql, params = mysql.calls[0]
        self.assertIn("MAX_EXECUTION_TIME(8000)", sql)
        self.assertIn("ds.content_id=s.data_source_id", sql)
        self.assertIn("ds.app_id=%s AND ds.release_status=1", sql)
        self.assertIn("ds.deploy_time>0 AND ds.deploy_time<=%s", sql)
        self.assertIn("COALESCE(ds.language,''),", sql)
        self.assertIn("COALESCE(s.language,''),", sql)
        self.assertIn("LOWER(COALESCE(ds.name,''))", sql)
        self.assertLess(sql.index("LOWER(COALESCE(ds.name,''))"), sql.index("LIMIT"))
        self.assertEqual(params.count("Only Drama Title"), 2)

    def test_strict_metadata_conflict_and_optional_description(self):
        repo, mysql, _ = source([[raw()], [raw()]])
        mysql.bad_desc = True
        self.assertEqual(repo.get_materials(["42"])["42"]["description_status"], "ambiguous")
        with self.assertRaises(SourceError):
            repo.get_materials(["42"], need_desc=True)
        repo, mysql, _ = source([[raw()]])
        mysql.bad_name = True
        with self.assertRaises(SourceError):
            repo.get_materials(["42"])

    def test_invalid_filters_cursors_and_page_limits(self):
        repo, _, _ = source()
        for query in (dict(cursor="!"), dict(limit=101), dict(limit=True), dict(language="bad language"),
                      dict(uploader_id="x"), dict(search="x" * 201)):
            with self.subTest(query=query), self.assertRaises(SourceError):
                repo.search_materials(query)


class GatedSQLTests(unittest.TestCase):
    env = {"FB_AUTO_MYSQL_USER": "test_reader", "FB_AUTO_MYSQL_PASSWORD": "test-password"}
    response = b"""<?xml version="1.0"?>
<resultset><row><field name="replica_read_only">1</field></row></resultset>
<?xml version="1.0"?>
<resultset xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<row><field name="id">42</field><field name="name">A &amp; B&#10;C</field>
<field name="missing" xsi:nil="true"/><field name="empty"></field></row></resultset>"""

    def test_gate_fixed_replica_readonly_timeout_and_secret_boundaries(self):
        process = Mock(returncode=0)
        process.communicate.return_value = (self.response, b"")
        process.poll.return_value = 0
        with patch.dict(os.environ, {"SQL_GATE_BYPASS": "1"}), patch(
                "features.fb_video_publish.runtime_source.subprocess.Popen", return_value=process) as launch:
            data = GatedReadOnlyMySQL(self.env).select("SELECT %s AS name LIMIT %s", ["雨后';\\x", 1])
        args, kwargs = launch.call_args
        command = args[0]
        self.assertEqual(command[:5], ["/usr/bin/mysql", "-h", "101.32.56.53", "-P", "63350"])
        self.assertNotIn("test-password", " ".join(command))
        self.assertNotIn("SQL_GATE_BYPASS", kwargs["env"])
        self.assertEqual(kwargs["env"]["MYSQL_PWD"], "test-password")
        self.assertIn("START TRANSACTION READ ONLY", command[-1])
        self.assertIn("MAX_EXECUTION_TIME=8000", command[-1])
        self.assertIn("LIMIT 1", command[-1])
        self.assertNotIn("雨后", command[-1])
        self.assertIn("_utf8mb4 0x", command[-1])
        self.assertNotIn("CONVERT(0x", command[-1])
        self.assertIn("--connect-timeout=3", command)
        process.communicate.assert_called_once_with(timeout=25)
        self.assertEqual(data, [{"id": "42", "name": "A & B\nC", "missing": None, "empty": ""}])

    def test_writable_endpoint_or_unsafe_sql_never_returns_data(self):
        with self.assertRaises(TemplateError):
            GatedReadOnlyMySQL._decode(self.response.replace(b">1</field>", b">0</field>"))
        for sql, params in (("UPDATE t SET a=1", []), ("SELECT 1;DELETE FROM t", []),
                            ("SELECT 1 INTO OUTFILE 'x'", []), ("SELECT %s", [])):
            with self.subTest(sql=sql), self.assertRaises(TemplateError):
                GatedReadOnlyMySQL._bind(sql, params)

    def test_timeout_is_sanitized_and_child_terminated(self):
        process = Mock(returncode=None)
        process.communicate.side_effect = [subprocess.TimeoutExpired(["secret-command"], 25), (b"", b"")]
        process.poll.return_value = None
        with patch("features.fb_video_publish.runtime_source.subprocess.Popen", return_value=process), patch(
                "features.fb_video_publish.runtime_source.os.name", "nt"):
            with self.assertRaises(TemplateError) as raised:
                GatedReadOnlyMySQL(self.env).select("SELECT 1", [])
        self.assertNotIn("secret", str(raised.exception))
        process.kill.assert_called_once()

    def test_sidecar_uses_only_fixed_authenticated_local_broker(self):
        response = MagicMock(status_code=200)
        response.__enter__.return_value = response
        response.iter_content.return_value = [b'{"ok":true,"items":[{"id":"42","empty":null}]}']
        session = MagicMock()
        session.__enter__.return_value = session
        session.post.return_value = response
        with patch("features.fb_video_publish.runtime_source.requests.Session", return_value=session):
            rows = BrokerReadOnlyMySQL({"FB_AUTO_POST_INTERNAL_TOKEN": "b" * 64}).select(
                "SELECT %s AS id", ["42"])
        self.assertEqual(rows, [{"id": "42", "empty": None}])
        self.assertFalse(session.trust_env)
        args, kwargs = session.post.call_args
        self.assertEqual(args[0], "http://127.0.0.1:8787/internal/fb-video-publish/query")
        self.assertEqual(kwargs["json"], {"sql": "SELECT %s AS id", "params": ["42"],
                                         "schema": "kunlunads_dev", "blacklist_schema": "ads_setting"})
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer " + "b" * 64)
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(kwargs["timeout"], (3, 30))

    def test_broker_error_body_is_never_exposed(self):
        response = MagicMock(status_code=503)
        response.__enter__.return_value = response
        response.iter_content.return_value = [b'{"ok":false,"message":"secret-output"}']
        session = MagicMock()
        session.__enter__.return_value = session
        session.post.return_value = response
        with patch("features.fb_video_publish.runtime_source.requests.Session", return_value=session):
            with self.assertRaises(TemplateError) as raised:
                BrokerReadOnlyMySQL({"FB_AUTO_POST_INTERNAL_TOKEN": "b" * 64}).select("SELECT 1", [])
        self.assertNotIn("secret-output", str(raised.exception))

    def test_broker_accepts_actual_page_and_material_repository_queries(self):
        from features.fb_auto_posts.repositories import PagePoolRepository
        from features.fb_video_publish.query_broker import validate_query
        repo, mysql, _ = source([[raw()]])
        repo.search_materials({"search": "Drama", "language": "en", "limit": 1})
        captured = list(mysql.calls)
        recorder = SimpleNamespace(schema="kunlunads_dev", blacklist_schema="ads_setting")
        recorder.select = lambda sql, params: captured.append((sql, list(params))) or []
        pages = PagePoolRepository(recorder)
        pages.list_groups(is_admin=False, owner_user_id="803")
        pages.list_pages(["1"], is_admin=False, owner_user_id="803")
        pages.legacy_conflicts(["1"])
        pages.eligible_credentials("123456")
        self.assertEqual(len(captured), 7)
        for sql, params in captured:
            with self.subTest(sql=sql[:80]):
                self.assertEqual(validate_query({"sql": sql, "params": params,
                    "schema": "kunlunads_dev", "blacklist_schema": "ads_setting"}), (sql, params))


if __name__ == "__main__":
    unittest.main()
