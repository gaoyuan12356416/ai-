import unittest
from datetime import datetime, timezone
from decimal import Decimal

from features.fb_auto_posts.repositories import MaterialRepository, PagePoolRepository, RepositoryError
from features.fb_auto_posts.metrics import MetricTotals, MetricWindow
from features.fb_auto_posts.validation import normalize_template_payload
from scripts.test_fb_auto_validation import payload


class FakeMySQL:
    schema = "kunlunads_dev"
    blacklist_schema = "ads_setting"
    def __init__(self): self.calls = []
    def select(self, sql, params):
        self.calls.append((sql, tuple(params)))
        if "GROUP BY g.id" in sql:
            return [{"group_id": "6", "owner_user_id": "248", "group_type": 0, "group_name": "DW Post", "app_id": "1479", "product": "Dramawave", "total_pages": 3, "publishable_pages": 2}]
        if "ads_facebook_page_group_ins" in sql:
            return [
                {"group_id": "6", "owner_user_id": "248", "page_id": "10001", "timezone": "UTC", "language": "english", "page_name": "FreeReels", "eligible_token_count": 2},
                {"group_id": "18", "owner_user_id": "248", "page_id": "10001", "timezone": "UTC", "language": "english", "page_name": "FreeReels", "eligible_token_count": 2},
            ]
        if "page_access_token" in sql:
            return [
                {"credential_id": "1", "page_id": "10001", "fb_user_id": "7", "page_access_token": "secret-A"},
                {"credential_id": "2", "page_id": "10001", "fb_user_id": "8", "page_access_token": "secret-A"},
                {"credential_id": "3", "page_id": "10001", "fb_user_id": "8", "page_access_token": "secret-B"},
            ]
        return []


class RepositoryTests(unittest.TestCase):
    def test_group_query_uses_real_user_column_and_returns_counts(self):
        mysql = FakeMySQL(); groups = PagePoolRepository(mysql).list_groups(is_admin=False, owner_user_id="248")
        self.assertEqual((groups[0].name, groups[0].app_id, groups[0].publishable_pages), ("DW Post", "1479", 2))
        self.assertIn("g.user_id", mysql.calls[0][0]); self.assertNotIn("g.owner_user_id", mysql.calls[0][0])
        self.assertIn("g.type IN (0,1)", mysql.calls[0][0])
        self.assertIn("p.status<>1", mysql.calls[0][0]); self.assertNotIn("p.status=0", mysql.calls[0][0])

    def test_overlapping_membership_becomes_one_page_with_lineage(self):
        mysql = FakeMySQL(); pages = PagePoolRepository(mysql).list_pages(["6", "18"], is_admin=True, owner_user_id="")
        self.assertEqual(len(pages), 1); self.assertEqual(pages[0].group_ids, ("6", "18"))
        self.assertEqual(pages[0].page_name, "FreeReels")
        self.assertIn("g.type IN (0,1)", mysql.calls[0][0])
        self.assertIn("MAX(TRIM(pn.page_name))", mysql.calls[0][0])
        self.assertIn("pn.status<>1", mysql.calls[0][0]); self.assertIn("p.status<>1", mysql.calls[0][0])
        self.assertNotIn("pn.status=0", mysql.calls[0][0]); self.assertNotIn("p.status=0", mysql.calls[0][0])

    def test_credentials_dedupe_by_token_in_memory(self):
        mysql = FakeMySQL(); credentials = PagePoolRepository(mysql).eligible_credentials("10001")
        self.assertEqual([item.credential_id for item in credentials], ["1", "3"])
        self.assertNotIn("secret", repr(credentials))
        self.assertIn("status<>1", mysql.calls[0][0]); self.assertNotIn("status=0", mysql.calls[0][0])

    def test_legacy_conflict_detects_same_page_across_different_groups(self):
        class MySQL(FakeMySQL):
            def select(self,sql,params):
                self.calls.append((sql,tuple(params)))
                return [{"queue_id":"9","queue_name":"legacy","group_id":"18","selected_group_id":"6","overlap_page_id":"10001","execute_switch":"1"}]
        mysql=MySQL(); conflicts=PagePoolRepository(mysql).legacy_conflicts(["6"])
        self.assertEqual(conflicts[0]["group_id"],"18"); self.assertEqual(conflicts[0]["overlap_page_id"],"10001")
        self.assertIn("si.page_id=li.page_id",mysql.calls[0][0]); self.assertIn("q.execute_switch=1",mysql.calls[0][0])
        self.assertIn("ORDER BY queue_id,overlap_page_id", mysql.calls[0][0])
        self.assertNotIn("ORDER BY q.id,li.page_id", mysql.calls[0][0])

    def test_material_query_pushes_filters_and_uses_drama_then_material_sort_before_limit(self):
        class MaterialMySQL:
            schema = "kunlunads_dev"; blacklist_schema = "ads_setting"
            def __init__(self): self.calls=[]
            def select(self, sql, params):
                self.calls.append((sql, tuple(params)))
                return [
                    {"material_id":"1","content_id":"d1","media_url":"https://cdn.example/1.mp4","material_name":"one","drama_name":"low","language":"english","video_duration":30,"resource_type_v2":"1"},
                    {"material_id":"6000","content_id":"d2","media_url":"https://cdn.example/2.mp4","material_name":"top","drama_name":"high","language":"english","video_duration":30,"resource_type_v2":"1"},
                ]
        class Metrics:
            def load_metric_window(self, **_kwargs):
                return MetricWindow((11,), ("2026-08-10",), {"d1":MetricTotals(Decimal("50"),Decimal("5")),"d2":MetricTotals(Decimal("100"),Decimal("5"))}, {("d1","1"):MetricTotals(Decimal("1"),Decimal("1")),("d2","6000"):MetricTotals(Decimal("10"),Decimal("1"))})
        raw=payload(); raw["drama_rule"].update({"sort_by":"spend","sort_direction":"desc","spend_min":10,"resource_type_v2":["1"]}); raw["material_rule"].update({"sort_by":"spend","sort_direction":"asc","spend_max":20})
        config=normalize_template_payload(raw); config.update({"app_id":"1479","product":"Dramawave","metric_product":"Dramawave","metric_platform":0})
        mysql=MaterialMySQL(); candidates=MaterialRepository(mysql,Metrics(),now_fn=lambda:datetime(2026,8,17,tzinfo=timezone.utc)).candidates(config)
        sql=mysql.calls[0][0]
        self.assertEqual([item.material_id for item in candidates],["6000","1"])
        self.assertNotIn("ads_custom_source_insight",sql); self.assertNotIn("mi.spend",sql)
        self.assertLess(sql.index("s.video_duration>=%s"),sql.index("LIMIT %s")); self.assertIn("AND s.id>%s",sql); self.assertIn("ORDER BY s.id LIMIT %s",sql)
        self.assertIn("ads_custom_source s FORCE INDEX(PRIMARY)",sql)
        self.assertIn("EXISTS (SELECT 1",sql); self.assertIn("ads_drama_info d FORCE INDEX(ac)",sql)
        self.assertNotIn("JOIN `kunlunads_dev`.ads_drama_info d ON",sql)
        self.assertEqual(mysql.calls[0][1][2],"en")

    def test_catalog_keyset_pagination_keeps_metric_top_after_5000(self):
        class Store:
            def load_metric_window(self,**_kwargs):
                return MetricWindow((1,),("2026-08-16",),{"top":MetricTotals(Decimal("9999"),Decimal("1"))},{("top","6001"):MetricTotals(Decimal("9999"),Decimal("1"))})
        class MySQL:
            schema="kunlunads_dev"; blacklist_schema="ads_setting"
            def __init__(self): self.calls=0; self.source_calls=0
            def select(self,sql,params):
                self.calls+=1
                if "JOIN (SELECT d0.content_id" in sql:
                    return [{"content_id":str(item),"drama_name":"d","resource_type_v2":"1","series_code":"s"} for item in params[3:]]
                cursor=int(params[-2]); start=cursor+1
                self.source_calls+=1
                ids=list(range(start,start+1000)) if cursor<5000 else ([6001] if cursor==5000 else [])
                return [{"material_id":str(i),"content_id":"top" if i==6001 else "zero","media_url":f"https://cdn.example/{i}.mp4","material_name":"m","drama_name":"d","language":"english","video_duration":30,"resource_type_v2":"1"} for i in ids]
        raw=payload(); raw["drama_rule"].update({"sort_by":"spend","sort_direction":"desc"}); raw["material_rule"].update({"sort_by":"spend","sort_direction":"desc"}); config=normalize_template_payload(raw); config.update({"app_id":"1479","product":"Dramawave","metric_product":"Dramawave","metric_platform":0})
        mysql=MySQL(); result=MaterialRepository(mysql,Store(),now_fn=lambda:datetime(2026,8,17,tzinfo=timezone.utc)).candidate_snapshot(config)
        self.assertEqual(result.candidates[0].material_id,"6001"); self.assertEqual(mysql.source_calls,6); self.assertEqual(mysql.calls,12); self.assertLessEqual(len(result.candidates),5000)

    def test_metric_drama_prefilter_proves_exact_top_limit_without_primary_scan(self):
        class Store:
            def load_metric_window(self,**_kwargs):
                return MetricWindow(
                    (1,), ("2026-08-16",),
                    {"d1":MetricTotals(Decimal("10"),Decimal("1")),"d2":MetricTotals(Decimal("20"),Decimal("2"))},
                    {("d1","1"):MetricTotals(Decimal("1"),Decimal("1")),("d1","2"):MetricTotals(Decimal("2"),Decimal("1")),("d2","3"):MetricTotals(Decimal("3"),Decimal("1")),("d2","4"):MetricTotals(Decimal("4"),Decimal("1"))},
                )
        class MySQL:
            schema="kunlunads_dev"; blacklist_schema="ads_setting"
            def __init__(self): self.calls=[]
            def select(self,sql,params):
                self.calls.append((sql,tuple(params)))
                if "JOIN (SELECT d0.content_id" in sql:
                    return [{"content_id":item,"drama_name":item,"resource_type_v2":"1","series_code":item} for item in params[3:]]
                if "FORCE INDEX(idx_source_type_source_id)" in sql:
                    return [
                        {"material_id":"1","content_id":"d1","media_url":"https://cdn.example/1.mp4","material_name":"m1","language":"en","video_duration":30},
                        {"material_id":"2","content_id":"d1","media_url":"https://cdn.example/2.mp4","material_name":"m2","language":"en","video_duration":30},
                        {"material_id":"3","content_id":"d2","media_url":"https://cdn.example/3.mp4","material_name":"m3","language":"en","video_duration":30},
                        {"material_id":"4","content_id":"d2","media_url":"https://cdn.example/4.mp4","material_name":"m4","language":"en","video_duration":30},
                    ]
                if "FORCE INDEX(PRIMARY)" in sql: raise AssertionError("full catalog scan must be skipped after an exact top-N proof")
                return []
        raw=payload(); raw["drama_rule"].update({"sort_by":"spend","sort_direction":"desc"}); raw["material_rule"].update({"sort_by":"spend","sort_direction":"desc"})
        config=normalize_template_payload(raw); config.update({"app_id":"1479","product":"Dramawave","metric_product":"Dramawave","metric_platform":0})
        mysql=MySQL(); result=MaterialRepository(mysql,Store(),now_fn=lambda:datetime(2026,8,17,tzinfo=timezone.utc),metric_prefilter_min_content_ids=2,metric_prefilter_batch_size=2,candidate_limit=3).candidate_snapshot(config)
        self.assertEqual([item.material_id for item in result.candidates],["4","3","2"])
        self.assertTrue(any("FORCE INDEX(idx_source_type_source_id)" in sql for sql,_ in mysql.calls))
        self.assertFalse(any("FORCE INDEX(PRIMARY)" in sql for sql,_ in mysql.calls))

    def test_metric_drama_prefilter_falls_back_when_it_cannot_fill_limit(self):
        class Store:
            def load_metric_window(self,**_kwargs):
                return MetricWindow((1,),("2026-08-16",),{"d1":MetricTotals(Decimal("10"),Decimal("1")),"d2":MetricTotals(Decimal("20"),Decimal("2"))},{})
        class MySQL:
            schema="kunlunads_dev"; blacklist_schema="ads_setting"
            def __init__(self): self.calls=[]
            def select(self,sql,params):
                self.calls.append((sql,tuple(params)))
                if "JOIN (SELECT d0.content_id" in sql:
                    return [{"content_id":item,"drama_name":item,"resource_type_v2":"1","series_code":item} for item in params[3:]]
                if "FORCE INDEX(idx_source_type_source_id)" in sql:
                    return [{"material_id":"9","content_id":"d2","media_url":"https://cdn.example/9.mp4","material_name":"m9","language":"en","video_duration":30}]
                if "FORCE INDEX(PRIMARY)" in sql:
                    if int(params[-2]) > 0: return []
                    return [
                        {"material_id":"1","content_id":"zero","media_url":"https://cdn.example/1.mp4","material_name":"m1","language":"en","video_duration":30},
                        {"material_id":"2","content_id":"d1","media_url":"https://cdn.example/2.mp4","material_name":"m2","language":"en","video_duration":30},
                        {"material_id":"9","content_id":"d2","media_url":"https://cdn.example/9.mp4","material_name":"m9","language":"en","video_duration":30},
                    ]
                return []
        raw=payload(); raw["drama_rule"].update({"sort_by":"spend","sort_direction":"desc"})
        config=normalize_template_payload(raw); config.update({"app_id":"1479","product":"Dramawave","metric_product":"Dramawave","metric_platform":0})
        mysql=MySQL(); result=MaterialRepository(mysql,Store(),now_fn=lambda:datetime(2026,8,17,tzinfo=timezone.utc),metric_prefilter_min_content_ids=2,metric_prefilter_batch_size=2,candidate_limit=3).candidate_snapshot(config)
        self.assertEqual([item.material_id for item in result.candidates],["9","2","1"])
        self.assertTrue(any("FORCE INDEX(idx_source_type_source_id)" in sql for sql,_ in mysql.calls))
        self.assertTrue(any("FORCE INDEX(PRIMARY)" in sql for sql,_ in mysql.calls))

    def test_ascending_spend_keeps_complete_primary_scan(self):
        class Store:
            def load_metric_window(self,**_kwargs):
                return MetricWindow((1,),("2026-08-16",),{"d1":MetricTotals(Decimal("10"),Decimal("1")),"d2":MetricTotals(Decimal("20"),Decimal("2"))},{})
        class MySQL:
            schema="kunlunads_dev"; blacklist_schema="ads_setting"
            def __init__(self): self.calls=[]
            def select(self,sql,params):
                self.calls.append((sql,tuple(params)))
                if "JOIN (SELECT d0.content_id" in sql: return [{"content_id":"zero","drama_name":"zero","resource_type_v2":"1","series_code":"zero"}]
                if "FORCE INDEX(PRIMARY)" in sql: return [{"material_id":"1","content_id":"zero","media_url":"https://cdn.example/1.mp4","material_name":"m1","language":"en","video_duration":30}]
                return []
        raw=payload(); raw["drama_rule"].update({"sort_by":"spend","sort_direction":"asc"})
        config=normalize_template_payload(raw); config.update({"app_id":"1479","product":"Dramawave","metric_product":"Dramawave","metric_platform":0})
        mysql=MySQL(); MaterialRepository(mysql,Store(),now_fn=lambda:datetime(2026,8,17,tzinfo=timezone.utc),metric_prefilter_min_content_ids=2,candidate_limit=1).candidate_snapshot(config)
        self.assertFalse(any("FORCE INDEX(idx_source_type_source_id)" in sql for sql,_ in mysql.calls))
        self.assertTrue(any("FORCE INDEX(PRIMARY)" in sql for sql,_ in mysql.calls))

    def test_catalog_scan_has_bounded_overall_deadline(self):
        class Store:
            def load_metric_window(self,**_kwargs): return MetricWindow((1,),('2026-08-16',),{}, {})
        class MySQL:
            schema="kunlunads_dev"; blacklist_schema="ads_setting"
            def select(self,sql,params):
                if "JOIN (SELECT d0.content_id" in sql: return [{"content_id":"d","drama_name":"d","resource_type_v2":"1","series_code":"s"}]
                return [{"material_id":str(i),"content_id":"d","media_url":f"https://cdn.example/{i}.mp4","material_name":"m","language":"en","video_duration":30} for i in range(1,1001)]
        ticks=iter((0,0,61))
        repository=MaterialRepository(MySQL(),Store(),now_fn=lambda:datetime(2026,8,17,tzinfo=timezone.utc),monotonic_fn=lambda:next(ticks),catalog_deadline_seconds=60)
        config=normalize_template_payload(payload()); config.update({"app_id":"1479","product":"Dramawave","metric_product":"Dramawave","metric_platform":0})
        with self.assertRaises(RepositoryError) as caught: repository.candidate_snapshot(config)
        self.assertEqual(caught.exception.code,"fb_auto_catalog_scan_timeout")

    def test_description_macro_uses_batched_same_language_resource_and_freezes_tag(self):
        class Store:
            def load_metric_window(self,**_kwargs):
                return MetricWindow((1,),("2026-08-16",),{"d1":MetricTotals(Decimal("1"),Decimal("1"))},{("d1","1"):MetricTotals(Decimal("1"),Decimal("1"))})
        class MySQL:
            schema="kunlunads_dev"; blacklist_schema="ads_setting"
            def __init__(self, ambiguous=False): self.calls=[]; self.ambiguous=ambiguous
            def select(self,sql,params):
                self.calls.append((sql,tuple(params)))
                if "ads_drama_resource" in sql:
                    return [{"content_id":"d1","drama_description":"  A  short\n drama  description  ","description_count":2 if self.ambiguous else 1}]
                if "JOIN (SELECT d0.content_id" in sql:
                    return [{"content_id":"d1","drama_name":"Drama","resource_type_v2":"1","series_code":"s1"}]
                return [{"material_id":"1","content_id":"d1","media_url":"https://cdn.example/1.mp4","material_name":"Material","material_tag":"hook","language":"en","video_duration":30}]
        raw=payload(); raw["message_template"]="{{desc}}"; config=normalize_template_payload(raw); config.update({"app_id":"1479","product":"Dramawave","metric_product":"Dramawave","metric_platform":0})
        mysql=MySQL(); candidates=MaterialRepository(mysql,Store(),now_fn=lambda:datetime(2026,8,17,tzinfo=timezone.utc)).candidates(config)
        self.assertEqual((candidates[0].drama_description,candidates[0].material_tag),("A short drama description","hook"))
        resource_call=next(item for item in mysql.calls if "ads_drama_resource" in item[0])
        self.assertIn("r.app_id=%s AND r.type=2 AND LOWER(TRIM(r.language))=%s",resource_call[0])
        self.assertIn("COUNT(DISTINCT BINARY TRIM(r.`desc`))",resource_call[0]); self.assertIn("GROUP BY r.content_id",resource_call[0])
        self.assertEqual(resource_call[1][:2],("1479","en")); self.assertEqual(len(resource_call[1]),3)
        self.assertEqual(MaterialRepository(MySQL(True),Store(),now_fn=lambda:datetime(2026,8,17,tzinfo=timezone.utc)).candidates(config),[])


if __name__ == "__main__": unittest.main()
