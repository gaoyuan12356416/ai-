import gzip
import json
from pathlib import Path
import pickle
import tempfile
import time
import unittest
from unittest.mock import patch
from contextlib import nullcontext

from features.fb_page_assignment.selector import _profile
from features.fb_page_assignment_v2.catalog import (
    SelectionError, Task, numbers, page_states, permissions, dramas_for_pools, task_ref)
from features.fb_page_assignment_v2.catalog_transport import pull_catalog, pull_publishers, source_file
from features.fb_page_assignment_v2.selection import Selector, decide, eligible_pages
from features.fb_page_assignment_v2.snapshot import Catalog, canon, install, sha, topics


def page(theme="revenge", audience="female", share=800):
    return dict(status="ready", audiences=[dict(key=audience, share_bp=10000,
                children=[dict(key=theme, share_bp=share)])])


def data():
    return dict(format=1, version="20261126T030637063913Z", pages={"2": page(), "3": page()},
                warm={"1479":{"A":{"en":["revenge"]}}}, directions={"1479":{"A":["female", ["en"]]}},
                themes={"avenged":"revenge"}, aliases={"test":"avenged"})


def make_snapshot(root, value):
    raw = canon(value)
    packed = gzip.compress(raw, mtime=0)
    head = dict(format=1, version=value["version"], sha256=sha(raw),
                packed_sha256=sha(packed), page_count=len(value["pages"]))
    (root / "current.json").write_bytes(canon(head))
    (root / (head["sha256"] + ".json.gz")).write_bytes(packed)
    return head


class Maps:
    def __init__(self, mappings):
        self.tables = mappings
        self.touched = []
    def query(self, sql, *args, **kwargs):
        self.touched.append(sql)
        return next((rows for table,rows in self.tables.items() if table in sql), [])
    def many(self, sql, ids, **kwargs):
        return self.query(sql) if set(ids) else []


class CatalogTests(unittest.TestCase):
    def test_dependency_log_identifies_failed_step_without_driver_message(self):
        from unittest.mock import Mock
        catalog = Mock()
        catalog.current.return_value = ({"sha256":"test"}, data())

        def connection(**kwargs):
            raise RuntimeError("private driver detail")

        selector = Selector(catalog, connection)
        req = dict(external_task_id="cross:10:qa", request_id="http-trace", page_group_id=24, drama_ids=["A"])
        with patch("features.fb_page_assignment_v2.selection.log") as log:
            error, = selector.select_batch([req], time.monotonic()+1)
        self.assertEqual((error.status, error.code), (503, "selection_dependency_unavailable"))
        event, = log.call_args.args
        self.assertEqual(event, "v2_selection_dependency_failure")
        self.assertEqual(log.call_args.kwargs["step"], "open_read_connection")
        self.assertEqual(log.call_args.kwargs["request_ids"], ["http-trace"])
        self.assertNotIn("private driver detail", str(log.call_args))

    def test_only_positive_language_then_blank_with_local_load_counter(self):
        profiles = {k: _profile(v) for k,v in {
            "2": page(), "3":page(), "4":page(), "5":page("other"),
            "6":page(), "7":page(), "8":page()}.items()}
        source = [[pid,lang,status,0,1,0,0,""] for pid,lang,status in (
            ("2","en",0), ("3","en",0), ("4","",0), ("5","fr",0), ("6","jp",0),
            ("7","en",1), ("9","en",0), ("8","pt-br",0))]
        lanes = eligible_pages(source, profiles)
        drama = lambda lang: {"audience":"female","language":lang,"themes":["revenge"]}
        ds = {"a":drama("en"), "b":drama("fr"), "c":drama(""), "d":{**drama("en"),"themes":[]},
              "e":{"error":"drama_source_ambiguous"}}
        cache = {}
        ids = ["a","a","b","c","d","e"]
        result = decide(ids, ds, lanes, cache)
        self.assertEqual([(v["page_id"],v["selection_stage"]) for v in result],
                         [("2","language_profile"),("3","language_profile"),("4","fallback_profile"),
                          ("4","fallback_profile"),(None,"none"),(None,"none")])
        self.assertEqual(result, decide(ids, ds, lanes, cache))
        self.assertEqual(result[4]["error_code"], "drama_profile_missing")
        self.assertEqual(result[5]["error_code"], "drama_source_ambiguous")

    def test_canary_and_multi_pool_disambiguation_by_final_native_child(self):
        refs = {Task("cross",10), Task("cross",11), Task("native",99)}
        reader = Maps({
            "ads_cross_template_queue": [
                dict(id=10,supper_id=5023,user_id=248,app_id=1479,source_id='{"333":"metadata"}'),
                dict(id=11,supper_id=5000,user_id=248,app_id=1479,source_id='[444]')],
            "ads_template_make_queue": [dict(id=99,user_id=248,template_make_id=123,ad_account="22",target_ids="444")],
            "idx_sqid": [dict(super_queue_id=p,publish_queue_id=n,template_id=t,page_group_id=g)
                         for p,n,t,g in ((10,98,201,24),(11,99,123,48),(11,100,124,24))],
            "idx_pqid": [dict(super_queue_id=11,publish_queue_id=99,template_id=123,page_group_id=48),
                         dict(super_queue_id=10,publish_queue_id=99,template_id=999,page_group_id=62)],
            "ads_auto_publishs_queue": [dict(user_id=248,app_id=1479,template_ids='["5023"]')]})
        grants, materials = permissions(reader, refs)
        self.assertEqual(grants, {Task("cross",10):{62},Task("cross",11):set(), Task("native",99):{48}})
        self.assertEqual(materials[Task("cross",10)], {333})
        self.assertEqual(task_ref("3053734"), task_ref("cross:3053734:stable-part"))
        with self.assertRaises(SelectionError): task_ref("missing")
        self.assertEqual(pickle.loads(pickle.dumps(SelectionError(403,"pool"))).status, 403)

    def test_native_multi_account_permission_is_intersection(self):
        reader = Maps({"ads_template_make_queue":[dict(id=2,ad_account="10,11",target_ids="10",template_make_id=8)],
            "ads_accounts_setting":[dict(id=10,account_id="act_a"),dict(id=11,account_id="act_b")],
            "ads_alternative_accounts":[dict(account_id="act_a",page_group_id=1,page_group_id2=24),
                                        dict(account_id="act_b",page_group_id=24,page_group_id2=2)]})
        grants, _ = permissions(reader, {Task("native",2)})
        self.assertEqual(grants[Task("native",2)], {24})
        self.assertEqual(numbers("12,14"), {12,14})

    def test_direct_drama_ids_use_pool_product_and_exact_identity(self):
        reader = Maps({"ads_drama_info":[
            dict(content_id="Ab",app_id=1479,language="en"),
            dict(content_id="Ab",app_id=3302,language="jp"),
            dict(content_id="Ab",app_id=3302,language="ja"),
            dict(content_id="old",app_id=1479,language="en"),
            dict(content_id="XX",app_id=1479,language="en"),
            dict(content_id="many",app_id=1479,language="en"),
            dict(content_id="many",app_id=1479,language="ar")]})
        facts=dramas_for_pools(reader,{24:1479,48:3302},
                              {24:{"Ab","old","absent","xx","many"},48:{"Ab","old"}})
        self.assertEqual(facts[24,"Ab"],{"app":1479,"language":"en","id":"Ab"})
        self.assertEqual(facts[48,"Ab"],{"app":3302,"language":"ja","id":"Ab"})
        self.assertEqual(facts[48,"old"]["error"],"drama_not_found")
        self.assertEqual(facts[24,"absent"]["error"],"drama_not_found")
        self.assertEqual(facts[24,"xx"]["error"],"drama_not_found")
        self.assertEqual(facts[24,"many"]["error"],"drama_source_ambiguous")
        self.assertEqual(len(reader.touched),1)
        self.assertIn("ads_drama_info",reader.touched[0])

    def test_merged_state_excludes_removed_unauthorized_duplicate_language(self):
        base=dict(group_id=24,page_id="2",language="en",source_id=1,status=0,forbidden_status=0,
                  is_permission=1,active_ad_count=0,fan_ccount=1,updated_at="2026-11-11")
        reader=Maps({"LEFT JOIN":[base, {**base,"language":"pt"}, {**base,"page_id":"3","source_id":5},
                                  {**base,"page_id":"3","source_id":6,"is_permission":0}],
                     "ads_facebook_page_group":[dict(id=24,app_id=1479),dict(id=25,app_id=9999)]})
        states=page_states(reader,{24,25})
        self.assertEqual(set(states),{24})
        self.assertFalse(eligible_pages(states[24],{"2":_profile(page()),"3":_profile(page())}))
        self.assertIn("i.deleted_at=0",reader.touched[-1])

    def test_snapshot_pull_repair_atomic_and_previous_version_retained(self):
        with tempfile.TemporaryDirectory() as temp:
            source, dest = Path(temp)/"s",Path(temp)/"d"
            source.mkdir()
            h1 = make_snapshot(source,data())
            calls=[]
            def fetch(command, output):
                calls.append(command)
                output.write(source_file(source,command).read_bytes())
            self.assertEqual(pull_catalog(fetch,dest)["sha256"], h1["sha256"])
            cat=Catalog(dest)
            self.assertEqual(cat.current()[1]["pages"],data()["pages"])
            pull_catalog(fetch,dest)
            self.assertEqual(calls.count("archive:"+h1["sha256"]),1)
            (dest/(h1["sha256"]+".json.gz")).write_bytes(b"corrupt cache")
            pull_catalog(fetch,dest)
            h2=make_snapshot(source,{**data(),"version":"20261126T030638063913Z"})
            archive=source/(h2["sha256"]+".json.gz")
            good=archive.read_bytes()
            archive.write_bytes(b"corrupt transport")
            with self.assertRaises(ValueError): pull_catalog(fetch,dest)
            self.assertEqual(cat.current()[0]["version"],h1["version"])
            archive.write_bytes(good)
            self.assertEqual(pull_catalog(fetch,dest)["sha256"],h2["sha256"])
            self.assertTrue((dest/(h1["sha256"]+".json.gz")).is_file())
            self.assertEqual(cat.current()[0]["version"],h2["version"])
            make_snapshot(source,data())
            self.assertEqual(install(source,dest)["sha256"],h2["sha256"])
            with self.assertRaises(ValueError): source_file(source,"archive:../../private")
            (source/"publishers.json").write_bytes(canon({"addresses":["10.1.2.3"]}))
            target=dest/"config"/"publishers.json"
            self.assertEqual(pull_publishers(fetch,target),1)
            (source/"publishers.json").write_bytes(canon({"addresses":["8.8.8.8"]}))
            with self.assertRaises(ValueError): pull_publishers(fetch,target)
            self.assertEqual(json.loads(target.read_bytes())["addresses"],["10.1.2.3"])
            (source/"publishers.json").write_bytes(canon({"addresses":[]}))
            self.assertEqual(pull_publishers(fetch,target),0)
            self.assertEqual(json.loads(target.read_bytes())["addresses"],[])

    def test_shared_lookup_mixed_outcomes_and_immutable_candidate_blob(self):
        from features.fb_page_assignment_v2.ledger import freeze_candidates
        cd=data()
        class Snap:
            def current(self): return {"version":cd["version"],"sha256":"b"*64},cd
        class Conn:
            def cursor(self): return nullcontext(object())
        calls=[]
        def connection(**kwargs):
            calls.append(kwargs)
            return nullcontext(Conn())
        selector=Selector(Snap(),connection,freeze_candidates)
        reqs=[dict(external_task_id=k,page_group_id=p,drama_ids=ids) for k,p,ids in (
            ("cross:10:a",24,["A","A"]),("cross:10:b",24,["A"]),("cross:10:c",62,["A"]),
            ("arbitrary-tracking-id",24,["A"]),("3087027",24,["missing","A"]))]
        raw=[["2","en",0,0,1,0,0,""],["3","en",0,0,1,0,0,""]]
        reader=Maps({"LEFT JOIN":[dict(group_id=24,page_id=pid,language="en",source_id=int(pid),
                       status=0,forbidden_status=0,is_permission=1,active_ad_count=0,fan_ccount=0,updated_at="")
                       for pid in ("2","3")],
                     "ads_facebook_page_group":[dict(id=24,app_id=1479)],
                     "ads_drama_info":[dict(content_id="A",app_id=1479,language="en")]})
        with patch("features.fb_page_assignment_v2.selection.Reads",return_value=reader):
            replies=selector.select_batch(reqs,time.monotonic()+1)
        self.assertEqual([r["page_id"] for r in replies[0]["rows"]],["2","3"])
        self.assertEqual(replies[1]["rows"][0]["page_id"],"2")
        self.assertEqual(replies[2].status,403)
        self.assertEqual(replies[2].code,"publication_pool_unavailable")
        self.assertEqual(replies[3]["rows"][0]["page_id"],"2")
        self.assertEqual(replies[4]["rows"][0]["error_code"],"drama_not_found")
        self.assertEqual(replies[4]["rows"][1]["page_id"],"2")
        self.assertIs(replies[0]["snapshot"]["candidates"],replies[1]["snapshot"]["candidates"])
        self.assertEqual(replies[0]["snapshot"]["candidate_sha256"],freeze_candidates(raw).digest)
        self.assertEqual((len(calls),len(reader.touched)),(1,3))
        self.assertTrue(all("ads_facebook_page_group" in q or "ads_drama_info" in q for q in reader.touched))
        self.assertEqual(replies[0]["snapshot"]["pool_app_id"],1479)

    def test_themes_normalize_and_series_ambiguity(self):
        rows = [dict(series_code="A",language="jp",labels='["ＴＥＳＴ"]')]
        self.assertEqual(topics(rows,{"test":"avenged"},{"avenged":"revenge"}),{"ja":["revenge"]})
        self.assertEqual(topics(rows + [{**rows[0],"series_code":"C"}],
                               {"test":"avenged"},{"avenged":"revenge"}),{"ja":[]})


if __name__ == "__main__":
    unittest.main()
