import copy
from datetime import datetime
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from features.moboreels_library.catalog import (
    BUSINESS_COLUMNS, Catalog, equal_business, language_name, merge_drama,
    normalize_drama,
)


def upstream(**changes):
    row = {
        "story_id": "007", "story_name": "潮汐", "is_show": 1,
        "c_name": "都市", "channel_text": "女频", "introduce": "简介",
        "img_src": "https://example.test/cover.jpg", "lang_id": 3,
        "is_full_text": "已完结", "baiduLink": "网盘分享原文",
        "baiduLinkCode": "a123", "publish_time": "2026-11-12 13:03:07",
        "local_type": 2, "local_type_text": "译制剧", "audio_type": 1,
        "audio_type_text": "原声剧", "holder_type": 1,
        "holder_text": "自制剧", "payepisfrom": 9,
    }
    row.update(changes)
    return row


class CatalogMergeTest(unittest.TestCase):
    def test_normalize_retains_original_json_and_source_time(self):
        raw = upstream(future_field={"notes": ["一", "二"]})
        result = normalize_drama(raw)
        self.assertEqual(tuple(result), BUSINESS_COLUMNS)
        self.assertEqual(len(result), 20)
        self.assertEqual(result["story_id"], "007")
        self.assertEqual(result["source_publish_time"], raw["publish_time"])
        self.assertEqual(result["baidu_link"], raw["baiduLink"])
        self.assertEqual(result["raw_json"], raw)
        raw["future_field"]["notes"].append("三")
        self.assertNotEqual(result["raw_json"], raw)

    def test_delisting_preserves_valid_material_and_description_only(self):
        before = normalize_drama(upstream())
        raw = upstream(is_show=0, story_name="-", introduce=None,
                       img_src="", baiduLink="-", baiduLinkCode="",
                       lang_id=-1, local_type=0, audio_type=0,
                       holder_type=0, payepisfrom=-1, c_name="新分类")
        merged = merge_drama(before, raw)
        for field in ["story_name", "introduce", "img_src", "baidu_link",
                      "baidu_link_code", "lang_id", "local_type",
                      "audio_type", "holder_type", "payepisfrom"]:
            self.assertEqual(merged[field], before[field], field)
        self.assertEqual(merged["c_name"], "新分类")
        self.assertEqual(merged["is_show"], 0)
        self.assertEqual(merged["raw_json"], raw)
        self.assertEqual(before["is_show"], 1)

    def test_active_record_empty_values_replace_previous_information(self):
        before = normalize_drama(upstream())
        raw = upstream(story_name="", introduce=None, baiduLink="",
                       lang_id=-1, payepisfrom=0)
        merged = merge_drama(before, raw)
        self.assertEqual(merged["story_name"], "")
        self.assertIsNone(merged["introduce"])
        self.assertEqual(merged["baidu_link"], "")
        self.assertEqual(merged["lang_id"], -1)
        self.assertEqual(merged["payepisfrom"], 0)

    def test_new_hidden_row_keeps_unknown_placeholders(self):
        raw = upstream(is_show=0, story_name="-", lang_id=-1)
        merged = merge_drama(None, raw)
        self.assertEqual(merged["story_name"], "-")
        self.assertEqual(merged["lang_id"], -1)

    def test_unknown_language_code_is_preserved_and_labelled(self):
        result = normalize_drama(upstream(lang_id=23))
        self.assertEqual(result["lang_id"], 23)
        self.assertEqual(language_name(23), "未知语言（23）")
        self.assertEqual(language_name(-1), "未知语言")
        self.assertEqual(language_name("3"), "英语")

    def test_json_format_and_metadata_do_not_change_business_equality(self):
        first = normalize_drama(upstream())
        second = copy.deepcopy(first)
        second["raw_json"] = json.dumps(first["raw_json"], sort_keys=True)
        second["last_seen_at"] = "2026-11-12T00:00:00Z"
        self.assertTrue(equal_business(first, second))
        second["source_publish_time"] = "2026-11-13 00:00:00"
        self.assertFalse(equal_business(first, second))
        second = copy.deepcopy(first)
        second["raw_json"]["new_field"] = True
        self.assertFalse(equal_business(first, second))

    def test_rejects_invalid_identity_status_and_oversize_values(self):
        cases = [{"story_id": ""}, {"story_id": "x" * 65},
                 {"story_id": True}, {"is_show": 2}, {"is_show": None},
                 {"c_name": "中" * 256}, {"lang_id": 32768},
                 {"payepisfrom": 2147483648}, {"lang_id": 3.5},
                 {"introduce": {"invalid": "type"}},
                 {"story_name": "中" * 21846}]
        for changes in cases:
            with self.subTest(changes=list(changes)):
                with self.assertRaises(ValueError):
                    normalize_drama(upstream(**changes))

    def test_required_only_payload_is_normalized_without_fake_information(self):
        result = normalize_drama({"story_id": 17, "is_show": "0"})
        self.assertEqual(result["story_id"], "17")
        self.assertEqual(result["story_name"], "")
        self.assertIsNone(result["lang_id"])
        self.assertEqual(result["is_show"], 0)

    def test_different_identity_cannot_be_merged(self):
        with self.assertRaises(ValueError):
            merge_drama(normalize_drama(upstream()), upstream(story_id="008", is_show=0))

    def test_presentation_does_not_mutate_database_results(self):
        class FakeDatabase:
            row = normalize_drama(upstream(lang_id=23))
            def list_dramas(self, params):
                self.params = params
                return {"list": [self.row], "total": 1, "page_index": 1,
                        "page_size": 50, "filters": {"languages": [23]}}
            def get_drama(self, story_id):
                return self.row if story_id == "007" else None
        db = FakeDatabase()
        catalog = Catalog(None, db)
        result = catalog.list({"is_show": "1"})
        self.assertEqual(result["list"][0]["language_name"], "未知语言（23）")
        self.assertNotIn("language_name", db.row)
        self.assertEqual(catalog.detail("007")["language_name"], "未知语言（23）")
        self.assertIsNone(catalog.detail("absent"))

    def test_presentation_normalizes_internal_times_but_not_source_time(self):
        row = normalize_drama(upstream())
        row.update(first_seen_at=datetime(2026, 11, 12, 10, 0, 0, 123000),
                   last_seen_at="2026-11-12 10:20:00.456")
        result = Catalog._present(row)
        self.assertEqual(result["first_seen_at"], "2026-11-12T10:00:00.123Z")
        self.assertEqual(result["last_seen_at"], "2026-11-12T10:20:00.456Z")
        self.assertEqual(result["source_publish_time"], "2026-11-12 13:03:07")
        self.assertIsInstance(row["first_seen_at"], datetime)
        row["last_seen_at"] = "2026-11-12T18:20:00+08:00"
        self.assertEqual(Catalog._present(row)["last_seen_at"], "2026-11-12T10:20:00.000Z")


if __name__ == "__main__":
    unittest.main()
