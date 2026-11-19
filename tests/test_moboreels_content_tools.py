import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from features.moboreels_library.content_tools import ContentTools
from features.moboreels_library.promotions import PromotionError
from features.moboreels_library.navigation import ensure_navigation


class Provider:
    def __init__(self):
        self.calls = []
        self.data = []
    def _post(self, endpoint, body):
        self.calls.append((endpoint, body))
        data = self.data(body) if callable(self.data) else self.data
        return SimpleNamespace(status_code=200, json=lambda: {"code": 200, "data": copy.deepcopy(data)})


class ContentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = SimpleNamespace(data_dir=Path(self.tmp.name), institution_user_id="inst")
        self.provider = Provider()
        self.drama = {"story_id": "42", "story_name": "Test", "lang_id": 23}
        self.catalog = SimpleNamespace(detail=lambda story: self.drama,
                                       db=SimpleNamespace(recommendation_resource_ids=lambda ids: {i: 'XEY'+i for i in ids}))
        self.clock = datetime(2026, 11, 13, 2, tzinfo=timezone.utc)
        self.tools = ContentTools(self.settings, self.catalog, self.provider, lambda: self.clock)

    def test_recommendation_mapping_deduplicates_platforms_and_omits_conflicts(self):
        from features.moboreels_library.db import Database
        db=object.__new__(Database);calls=[]
        db._read=lambda sql:(calls.append(sql) or [dict(story_id='1',resource_id='XEY468')]*2+
            [dict(story_id='2',resource_id='A'),dict(story_id='2',resource_id='B')])
        self.assertEqual(db.recommendation_resource_ids(['1','2','3']),{'1':'XEY468'})
        self.assertIn('app_id=2373',calls[0]);self.assertIn('series_code',calls[0])

    def test_episodes_use_catalog_language_and_expire_before_hour(self):
        self.provider.data = [{"epis_no": 2, "epis_url": "https://cdn.example/2.mp4"}, {"epis_no": 1, "epis_url": "https://cdn.example/1.mp4"}]
        data = self.tools.episodes("42")
        self.assertEqual([r["epis_no"] for r in data["list"]], [1, 2])
        self.assertEqual(self.provider.calls[0][1], {"story_id": "42", "project_type": 2, "lang_id": 23})
        data["list"].clear()
        self.assertEqual(len(self.tools.episodes("42")["list"]), 2)
        self.assertEqual(len(self.provider.calls), 1)
        self.clock += timedelta(seconds=3541)
        self.tools.episodes("42")
        self.assertEqual(len(self.provider.calls), 2)
        self.tools.episodes("42", refresh=True)
        self.assertEqual(len(self.provider.calls), 3)

    def test_resource_ids_are_mapped_without_story_id_fallback(self):
        self.recommendations(3)
        self.catalog.db.recommendation_resource_ids = lambda ids: {'0': 'XEY468', '1': 'XEY468'}
        result = self.tools.recommendations({})
        self.assertEqual([r['resource_id'] for r in result['list']], ['XEY468', 'XEY468', ''])
        self.assertEqual([r['story_id'] for r in result['list']], ['0', '1', '2'])

    def test_resource_mapping_failure_keeps_recommendations_visible(self):
        from features.moboreels_library.db import DatabaseError
        self.recommendations(1)
        def fail(ids): raise DatabaseError('unavailable')
        self.catalog.db.recommendation_resource_ids = fail
        result = self.tools.recommendations({})
        self.assertEqual(result['total'], 1)
        self.assertEqual(result['list'][0]['resource_id'], '')
        self.assertTrue(result['resource_mapping_error'])

    def test_episode_missing_language_or_story_does_not_call_upstream(self):
        self.drama["lang_id"] = -1
        with self.assertRaises(PromotionError): self.tools.episodes("42")
        self.drama = None
        with self.assertRaises(PromotionError): self.tools.episodes("42")
        self.assertFalse(self.provider.calls)

    def test_episode_bad_urls_and_duplicate_numbers_are_not_cached(self):
        for data in [[{"epis_no": 1, "epis_url": "javascript:alert(1)"}],
                     [{"epis_no": 1, "epis_url": "https://x/1"}] * 2,
                     [{"epis_no": 1.5, "epis_url": "https://x/1"}]]:
            self.provider.data = data
            with self.assertRaises(PromotionError): self.tools.episodes("42")
        self.assertFalse(self.tools.episodes_cache)

    def recommendations(self, total=101):
        rows = [{"story_id": str(i), "story_name": "剧目"+str(i), "lang_id": 23 if i%2 else 3,
                 "recommend_grade": "SSS" if i%2 else "A", "tags": "love", "recommend_time": "2026-11-13"} for i in range(total)]
        self.provider.data = lambda body: {"total": total, "list": rows[(body["page_index"]-1)*100:body["page_index"]*100]}

    def test_full_recommendations_filters_summary_and_saved_cache(self):
        self.recommendations()
        data = self.tools.recommendations({"query_date": "2026-11-13", "lang_id": "23"})
        self.assertEqual(data["source_total"], 101)
        self.assertEqual(data["total"], 50)
        self.assertEqual(data["summary"]["grades"], {"A": 51, "SSS": 50})
        self.assertEqual(data["list"][0]["language_name"], "未知语言（23）")
        self.assertEqual(len(self.provider.calls), 2)
        again = ContentTools(self.settings, self.catalog, self.provider, lambda: self.clock)
        self.assertEqual(again.recommendations({"q": "剧目100"})["total"], 1)
        self.assertEqual(len(self.provider.calls), 2)

    def test_bad_pagination_keeps_previous_snapshot(self):
        self.recommendations(1)
        self.tools.recommendations({})
        path = next(self.settings.data_dir.rglob('*.json'))
        before = path.read_bytes()
        for data in [{"total": 2, "list": [{"story_id": "1"}]},
                     {"total": 2, "list": [{"story_id": "1"}, {"story_id": "1"}]},
                     {"total": 20001, "list": []}]:
            self.provider.data = data
            with self.assertRaises(PromotionError): self.tools.recommendations({"refresh": "1"})
            self.assertEqual(path.read_bytes(), before)

    def test_recommendation_dates_and_business_validation(self):
        for q in [{"query_date": "2026-11-14"}, {"query_date": "bad"}, {"project_type": "3"}, {"page_size": "999"}]:
            with self.assertRaises(ValueError): self.tools.recommendations(q)
        self.assertFalse(self.provider.calls)

    def test_navigation_additive_preserves_disabled_existing_item(self):
        old = [{"key": "partner_libraries", "items": [{"key": "moboreelsLibrary", "enabled": False}]}]
        result = ensure_navigation(old)
        self.assertEqual(len(result[0]["items"]), 3)
        self.assertFalse(result[0]["items"][0]["enabled"])
        self.assertEqual(result, ensure_navigation(result))
        self.assertEqual(len(old[0]["items"]), 1)
