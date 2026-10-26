"""Offline group-pool source policy tests; every SQL query uses the real broker validator."""
from __future__ import annotations

import copy
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from features.fb_video_publish.group_sources import GroupPoolSource, POLICY_VERSION, normalize_language
from features.fb_video_publish.query_broker import validate_query
from features.fb_video_publish.runtime_source import GatedReadOnlyMySQL
from features.fb_video_publish.sources import SourceError, SourceRepository

NOW = datetime(2026, 10, 26, 6, tzinfo=timezone.utc)
ACTOR = {"tenant_key": "fixture-tenant", "user_id": "fixture-user", "is_admin": False,
         "business_owner_id": "42", "sub_user_id": "42"}


def material(mid="1", **kwargs):
    return {"material_id": mid, "material_type": 2, "product": "Dramawave", "data_source": "6",
            "is_delete": 0, "language": "en", "content_id": "C" + mid, "material_name": "Video " + mid,
            "material_tag": "Romance", "media_url": "https://socialkit-cdn.yingliang.tech/" + mid + ".mp4",
            "thumbnail_url": "https://socialkit-cdn.yingliang.tech/" + mid + ".jpg",
            "video_duration": 30, "uploader_id": "42", **kwargs}


def resource(mid="1", **kwargs):
    return {"content_id": "C" + mid, "app_id": "1479", "series_code": "S" + mid,
            "language": "en", "drama_name": "Drama " + mid, "drama_description": "Synopsis " + mid, **kwargs}


def release(mid="1", **kwargs):
    return {"content_id": "C" + mid, "app_id": "1479", "language": "en", "release_status": 1,
            "deploy_time": int(NOW.timestamp()) - 60, **kwargs}


def group(gid="1", **kwargs):
    return {"group_id": gid, "owner_user_id": "42", "group_type": 0, "group_name": "Group " + gid,
            "app_id": "1479", "product": "Dramawave", "is_delete": 0, **kwargs}


def member(pid="11", gid="1", language="en", **kwargs):
    return {"group_id": gid, "page_id": pid, "language": language, "timezone": "Asia/Shanghai",
            "deleted_at": 0, **kwargs}


def page(pid="11", **kwargs):
    return {"page_id": pid, "page_name": "Page " + pid, "language": "es", "eligible_token_count": 1,
            "owner_user_id": "42", "access_token": "fixture-private-value", **kwargs}


class Catalog:
    schema, blacklist_schema = "kunlunads_dev", "ads_setting"

    def __init__(self):
        self.calls = []
        self.materials = {"1": material()}
        self.resources, self.releases = [resource()], [release()]
        self.groups, self.members, self.pages = [group()], [member()], [page()]

    def source(self):
        source = SourceRepository(None, SimpleNamespace(mysql=self), now_fn=lambda: NOW)
        return GroupPoolSource(source)

    def select(self, sql, params):
        validate_query({"sql": sql, "params": list(params), "schema": self.schema,
                        "blacklist_schema": self.blacklist_schema})
        GatedReadOnlyMySQL._bind(sql, params)
        self.calls.append((sql, params))
        if "GROUP BY p.page_id" in sql:
            scoped = "f.user_id=%s" in sql
            ids = params[3:] if scoped else params
            return copy.deepcopy([item for item in self.pages if item["page_id"] in ids
                                  and (not scoped or item["owner_user_id"] == params[0])])
        if "ads_facebook_page_group g" in sql:
            owner = params[-1] if "g.user_id=%s" in sql else None
            ids = params[:-1] if owner else params
            groups = [g for g in self.groups if g["is_delete"] == 0 and g["group_type"] in (0, 1)
                      and g["app_id"] == "1479" and g["product"].lower() == "dramawave"
                      and (not owner or g["owner_user_id"] == owner)
                      and ("g.id IN (" not in sql or g["group_id"] in ids)]
            gids = {g["group_id"] for g in groups}
            if "COUNT(DISTINCT i.page_id)" in sql:
                return [{**g, "total_pages": len({m["page_id"] for m in self.members
                                                  if m["group_id"] == g["group_id"] and not m["deleted_at"]})}
                        for g in groups]
            return copy.deepcopy([m for m in self.members if m["group_id"] in gids and not m["deleted_at"]])
        if "ads_custom_source" in sql:
            return copy.deepcopy([self.materials[mid] for mid in params[:-1] if mid in self.materials])
        if "ads_drama_resource" in sql:
            return copy.deepcopy([r for r in self.resources if r["content_id"] in params and r["app_id"] == "1479"])
        if "ads_drama_info" in sql:
            return copy.deepcopy([r for r in self.releases if r["content_id"] in params and r["app_id"] == "1479"])
        raise AssertionError("Unexpected source query")


class LanguageTests(unittest.TestCase):
    def test_aliases_chinese_distinctions_and_no_missing_default(self):
        for raw, expected in ((" English ", "en"), ("japanese", "ja"), ("JP", "ja"),
                              ("ja", "ja"), ("jp_JP", "ja-jp"), ("pt_BR", "pt-br"), ("zh", "zh"),
                              ("ZH_tw", "zh-tw"), ("zh-CN", "zh-cn")):
            with self.subTest(raw=raw):
                self.assertEqual(expected, normalize_language(raw))
        for invalid in (None, "", " ", "zh+tw", "中文", True, 12, "123", "unknown", "und", "en\n-us"):
            with self.subTest(invalid=invalid):
                self.assertEqual("", normalize_language(invalid))

    def test_constructor_does_not_access_legacy_fake_sources(self):
        GroupPoolSource(object())


class GroupScopeTests(unittest.TestCase):
    def setUp(self):
        self.db = Catalog()
        self.source = self.db.source()

    def test_groups_filter_owner_product_type_and_deletion(self):
        self.db.groups += [group("2", owner_user_id="84"), group("3", app_id="999", product="Other"),
                           group("4", group_type=2), group("5", is_delete=1), group("6", group_type=1)]
        self.assertEqual(["1", "6"], [g["id"] for g in self.source.list_groups(ACTOR)])
        self.assertEqual(["1", "2", "6"], [g["id"] for g in self.source.list_groups({**ACTOR, "is_admin": True})])
        self.assertEqual(1, self.source.list_groups(ACTOR)[0]["total_pages"])

    def test_entire_scope_rejected_when_one_group_loses_access(self):
        self.db.groups.append(group("2", owner_user_id="84"))
        with self.assertRaises(SourceError) as raised:
            self.source.resolve_scope(ACTOR, ["1", "2"])
        self.assertEqual("group_not_found", raised.exception.code)
        self.assertEqual(403, raised.exception.status)
        self.assertEqual(1, len(self.db.calls))
        with self.assertRaises(SourceError) as raised:
            self.source.list_groups({**ACTOR, "business_owner_id": ""})
        self.assertEqual("fb_auto_owner_mapping_missing", raised.exception.code)

    def test_group_language_wins_without_any_local_page_configuration(self):
        scope = self.source.resolve_scope(ACTOR, ["1"])
        self.assertEqual("en", scope["pages"]["11"]["language"])
        self.assertEqual(["1"], scope["pages"]["11"]["group_ids"])
        self.assertEqual([], scope["excluded"])
        self.assertNotIn("access_token", repr(scope))
        self.assertFalse(any("fb_video_page" in sql for sql, _ in self.db.calls))
        page_query = next(sql for sql, _ in self.db.calls if "GROUP BY p.page_id" in sql)
        self.assertIn("ads_facebook_page_ins", page_query)
        self.assertIn("f.user_id=%s", page_query)

    def test_duplicate_pages_conflicting_missing_and_revoked_are_explained(self):
        self.db.groups.append(group("2"))
        self.db.members = [member("11", "1", "jp"), member("11", "2", "ja"),
                           member("12", "1", "en"), member("12", "2", "es"),
                           member("13", "1", ""), member("14", "1", "en"),
                           member("15", "1", "en", deleted_at=1), member("16", "1", "en"),
                           member("17", "1", "en"), member("17", "2", "bad language")]
        self.db.pages = [page(str(pid)) for pid in range(11, 18)]
        self.db.pages[3]["eligible_token_count"] = 0
        self.db.pages[5]["owner_user_id"] = "84"
        result = self.source.resolve_scope(ACTOR, ["1", "2"])
        self.assertEqual(["11"], list(result["pages"]))
        self.assertEqual("ja", result["pages"]["11"]["language"])
        self.assertEqual(["1", "2"], result["pages"]["11"]["group_ids"])
        self.assertEqual({"12": "page_language_conflict", "13": "page_language_missing",
                          "14": "page_unavailable", "16": "page_unavailable", "17": "page_language_missing"},
                         {row["page_id"]: row["code"] for row in result["excluded"]})

    def test_authorization_reads_are_batched_below_101_pages(self):
        self.db.members = [member(str(pid)) for pid in range(1, 202)]
        self.db.pages = [page(str(pid)) for pid in range(1, 202)]
        result = self.source.resolve_scope(ACTOR, ["1"])
        self.assertEqual(201, len(result["pages"]))
        calls = [(sql, params) for sql, params in self.db.calls if "GROUP BY p.page_id" in sql]
        self.assertEqual([100, 100, 1], [len(params) - 3 for _, params in calls])
        self.assertEqual(5, len(self.db.calls))

    def test_admin_access_does_not_require_owner_mapping(self):
        self.db.groups[0]["owner_user_id"] = "84"
        self.db.pages[0]["owner_user_id"] = "84"
        result = self.source.resolve_scope({**ACTOR, "business_owner_id": "", "is_admin": True}, ["1"])
        self.assertEqual(["11"], list(result["pages"]))


class MaterialTests(unittest.TestCase):
    def setUp(self):
        self.db = Catalog()
        self.source = self.db.source()

    def resolve(self, **kwargs):
        return self.source.resolve_materials(["1"], **kwargs)

    def code(self, **kwargs):
        result = self.resolve(**kwargs)
        self.assertEqual([], result["items"])
        return result["errors"][0]["code"]

    def test_other_product_arbitrary_source_and_deleted_video_are_eligible(self):
        for product in ("OtherProduct", "", None):
            with self.subTest(product=product):
                self.db.materials["1"] = material(product=product, data_source="987654", is_delete=1,
                                                  language="source-language-is-ignored")
                item = self.resolve()["items"][0]
                self.assertEqual("en", item["language"])
                self.assertEqual(POLICY_VERSION, item["policy_version"])
        for sql, _ in self.db.calls:
            self.assertNotIn("blacklist", sql)
            self.assertNotIn("s.is_delete", sql)
            self.assertNotIn("s.product", sql)
            self.assertNotIn("s.data_source=", sql)
            self.assertNotIn("s.language", sql)

    def test_exact_ids_batched_100_and_output_keeps_requested_order(self):
        ids = [str(mid) for mid in range(201, 0, -1)]
        self.db.materials = {mid: material(mid) for mid in ids}
        self.db.resources = [resource(mid) for mid in ids]
        self.db.releases = [release(mid) for mid in ids]
        result = self.source.resolve_materials(ids)
        self.assertEqual(ids, [item["material_id"] for item in result["items"]])
        self.assertEqual([], result["errors"])
        self.assertEqual(9, len(self.db.calls))
        calls = [params for sql, params in self.db.calls if "ads_custom_source" in sql]
        self.assertEqual([100, 100, 1], [len(params) - 1 for params in calls])
        for sql, params in self.db.calls:
            self.assertIn("LIMIT", sql)
            self.assertIn("MAX_EXECUTION_TIME(8000)", sql)
            self.assertLessEqual(len(params), 101)

    def test_source_missing_nonvideo_duration_and_url_fail_per_item(self):
        self.db.materials = {
            "1": material(), "2": material("2", material_type=1),
            "3": material("3", video_duration="NaN"), "4": material("4", video_duration=0),
            "5": material("5", video_duration=3601), "6": material("6", media_url="https://evil.test/v.mp4"),
            "7": material("7", media_url="https://socialkit-cdn.yingliang.tech/v.webm"),
        }
        result = self.source.resolve_materials([str(mid) for mid in range(1, 9)])
        self.assertEqual(["1"], [r["material_id"] for r in result["items"]])
        self.assertEqual(["material_not_video", "material_duration_invalid", "material_duration_invalid",
                          "material_duration_invalid", "material_unavailable", "material_unavailable", "material_not_found"],
                         [r["code"] for r in result["errors"]])

    def test_duration_upper_boundary_and_approved_origin_canonicalization(self):
        self.db.materials["1"] = material(video_duration=3600,
                                          media_url="http://advertising-1306474899.cos.ap-hongkong.myqcloud.com/a.mp4")
        item = self.resolve()["items"][0]
        self.assertEqual(3600, item["duration_seconds"])
        self.assertTrue(item["media_url"].startswith("https://"))

    def test_mapping_missing_foreign_product_and_missing_language(self):
        self.db.resources = []
        self.assertEqual("drama_mapping_missing", self.code())
        self.db.resources = [resource(app_id="999")]
        self.assertEqual("drama_mapping_missing", self.code())
        self.db.resources = [resource(language="")]
        self.assertEqual("drama_language_missing", self.code())

    def test_duplicate_alias_mapping_is_one_identity_and_jp_release_matches(self):
        self.db.resources = [resource(language="ja"), resource(language="jp"), resource(language="Japanese")]
        self.db.releases = [release(language="jp")]
        self.assertEqual("ja", self.resolve()["items"][0]["language"])

    def test_ambiguous_language_series_name_or_description_is_rejected(self):
        cases = [("language", "es", "drama_language_ambiguous"),
                 ("series_code", "OTHER", "drama_mapping_ambiguous"),
                 ("drama_name", "Other Name", "drama_mapping_ambiguous"),
                 ("drama_description", "Other description", "drama_mapping_ambiguous")]
        for key, value, expected in cases:
            with self.subTest(field=key):
                self.db.resources = [resource(), resource(**{key: value})]
                self.assertEqual(expected, self.code())
        self.db.resources = [resource(language="zh"), resource(language="zh-tw")]
        self.assertEqual("drama_language_ambiguous", self.code())

    def test_description_is_optional_only_when_template_does_not_require_it(self):
        self.db.resources = [resource(drama_description="")]
        self.assertEqual("material_metadata_invalid", self.code())
        item = self.source.get_materials(["1"], need_desc=False)["1"]
        self.assertEqual("", item["drama_description"])
        self.assertEqual("missing", item["description_status"])

    def test_invalid_attribution_fields_rejected_before_freezing(self):
        for field, bad in (("material_name", ""), ("material_name", "bad\x00name"),
                           ("material_tag", "bad*tag"), ("content_id", "")):
            with self.subTest(field=field):
                self.db.materials["1"] = material(**{field: bad})
                self.assertEqual("material_metadata_invalid", self.code())
        self.db.materials["1"] = material()
        self.db.resources = [resource(drama_name="bad*name")]
        self.assertEqual("material_metadata_invalid", self.code())

    def test_release_language_status_missing_future_and_all_platform_times(self):
        for rows, expected in (([], "drama_not_released"), ([release(language="es")], "drama_not_released"),
                               ([release(release_status=0)], "drama_not_released"),
                               ([release(deploy_time=0)], "drama_not_released"),
                               ([release(deploy_time="bad")], "drama_deploy_time_invalid"),
                               ([release(), release(deploy_time=int(NOW.timestamp()) + 1)], "drama_not_yet_deliverable")):
            with self.subTest(expected=expected, rows=rows):
                self.db.releases = rows
                self.assertEqual(expected, self.code())
        self.db.releases = [release(deploy_time=int(NOW.timestamp()))]
        self.assertEqual([], self.resolve()["errors"])

    def test_strict_adapter_fails_if_any_id_missing_and_invalid_inputs(self):
        with self.assertRaises(SourceError) as raised:
            self.source.get_materials(["1", "2"])
        self.assertEqual("material_not_found", raised.exception.code)
        for ids in (["1", "1"], [True], ["0"], ["1" * 20]):
            with self.subTest(ids=ids), self.assertRaises(SourceError):
                self.source.resolve_materials(ids)
        self.assertEqual({"items": [], "errors": []}, self.source.resolve_materials([]))

    def test_database_errors_are_global_and_do_not_leak_sql_or_secrets(self):
        self.db.select = Mock(side_effect=RuntimeError("SELECT secret-token password=fixture-private-value"))
        with self.assertRaises(SourceError) as raised:
            self.resolve()
        self.assertEqual(503, raised.exception.status)
        for sensitive in ("SELECT", "secret-token", "password", "fixture-private-value"):
            self.assertNotIn(sensitive, str(raised.exception))

    def test_duplicate_or_unrequested_source_identity_fails_globally(self):
        for rows in ([material(), material()], [material("2")]):
            self.db.select = Mock(return_value=rows)
            with self.subTest(rows=rows), self.assertRaises(SourceError) as raised:
                self.resolve()
            self.assertEqual(503, raised.exception.status)


if __name__ == "__main__":
    unittest.main()
