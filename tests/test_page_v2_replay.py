"""Validate historical trace shape, request fencing, HTTP receipts and audit compaction without production I/O."""
import asyncio
from contextlib import closing
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

OPS = Path(__file__).resolve().parents[1]/"ops/fb-page-v2"
sys.path.insert(0,str(OPS))
import qa_mysql
import qa_replay
import qa_server


def fixture(sec, n, product=1479, pool=24, task=1, kind="native"):
    return dict(offset=sec, drama_ids=["d"+str(i) for i in range(n)], source_product=product,
                external_task_id=kind+":"+str(task)+":0", page_group_id=pool)


class Inputs(unittest.TestCase):
    def test_historical_profile_counts_and_item_mixes(self):
        meta, burst = qa_replay.read_trace("burst")
        self.assertEqual((meta["seconds"],len(burst),sum(r[1] for r in burst)),(10,720,1600))
        fixtures = [fixture(s,n,task=i+1) for i,(s,n,p,g,c) in enumerate(burst)]
        plan, coverage = qa_replay.make_plan(burst, fixtures, False, "A")
        self.assertTrue(coverage["exact_historical_arrivals_and_inputs"])
        self.assertEqual(coverage["replayed_distinct_source_tasks"],720)
        self.assertEqual(len({req["external_task_id"] for _,req in plan}),720)
        meta, hour = qa_replay.read_trace("hour")
        self.assertEqual((meta["seconds"],len(hour),sum(r[1] for r in hour)),(3600,6165,11269))
        self.assertEqual(sum(r[2]==3302 for r in hour),503)

    def test_representation_preserves_authorized_ids_and_discloses_substitutions(self):
        trace = [[0,2,1479,25,1],[2,2,1479,25,1],[5,1,3302,54,1]]
        fs = [fixture(10,3),fixture(11,1,3302,54,3,"cross")]
        with self.assertRaises(ValueError):
            qa_replay.make_plan(trace,fs,False,"a")
        plan, coverage = qa_replay.make_plan(trace,fs,True,"a")
        self.assertEqual(coverage["differences"],{"different_authorized_pool":2,
                         "different_publishing_task_type":2,"subset_of_real_drama_ids":2})
        self.assertEqual([p["page_group_id"] for _,p in plan],[24,24,54])
        self.assertEqual(plan[0][1]["drama_ids"],["d0","d1"])
        with self.assertRaisesRegex(ValueError,"missing authorized fixture"):
            qa_replay.make_plan([[0,4,3302,54,1]],fs,True,"a")

    def test_exact_historical_request_keeps_its_actual_current_authorized_pool(self):
        trace = [[1,2,1479,4,1],[1,2,1479,25,1]]
        first = fixture(1,2,1479,24,61,"cross")
        first["historical_pool"] = 4
        second = fixture(1,2,1479,55,62,"cross")
        second["historical_pool"] = 25
        plan,stats = qa_replay.make_plan(trace,[second,first],False,"x")
        self.assertTrue(stats["exact_historical_arrivals_and_inputs"])
        self.assertEqual(stats["differences"]["different_authorized_pool"],2)
        self.assertEqual([p["page_group_id"] for _,p in plan],[24,55])
        self.assertTrue(plan[0][1]["external_task_id"].startswith("cross:61:"))

    def test_prevent_mistarget_and_reuse_of_non_qa_state_directory(self):
        sources = qa_server.QASources("10.89.0.125")
        self.assertTrue(sources.allows("10.89.0.17"))
        self.assertTrue(sources.allows("127.0.0.1"))
        self.assertFalse(sources.allows("10.89.0.18"))
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp)/"assignment-v2.sqlite3").touch()
            with self.assertRaisesRegex(ValueError,"empty QA directory"):
                qa_server.initialize(tmp)
        for url in ("http://8.8.8.8:8945","https://127.0.0.1","http://user@127.0.0.1"):
            with self.assertRaises(ValueError):
                qa_replay.Client(url,"a"*40)


class ReplayHTTP(unittest.IsolatedAsyncioTestCase):
    async def test_real_endpoint_temp_sink_no_remote_calls_and_replay_archive(self):
        async def choose(group, deadline):
            return [(200,{"rows":[{"drama_id":d,"page_id":"7" if d=="yes" else None,
                                   "selection_stage":"language_profile" if d=="yes" else "none",
                                   "error_code":"" if d=="yes" else "drama_profile_missing"} for d in req["drama_ids"]],
                          "snapshot":{"candidates":[["7", "en"]],"catalog_version":"fixture"}})
                    for req in group]
        with tempfile.TemporaryDirectory() as tmp:
            root = qa_server.initialize(tmp)
            port = asyncio.get_running_loop().create_future()
            stop = asyncio.Event()
            with patch("features.fb_page_assignment_v2.audit.MySQLSink",side_effect=AssertionError("live writer")), \
                 patch("features.fb_page_assignment_v2.alerts.Relay",side_effect=AssertionError("live notifier")), \
                 patch("sys.stdout",new_callable=io.StringIO):
                service = asyncio.create_task(qa_server.serve(root,"127.0.0.1",0,compute=choose,on_ready=port,stop=stop))
                try:
                    target = "http://127.0.0.1:%d" % await asyncio.wait_for(port,3)
                    client = qa_replay.Client(target,(root/"qa-bearer").read_text())
                    wrong = qa_replay.Client(target,"p"*40)
                    with self.assertRaisesRegex(ValueError,"isolated QA"):
                        await wrong.status()
                    plan = [(0,{"external_task_id":"native:991::qa.u."+str(i), "page_group_id":24,
                                "drama_ids":["yes","no"]}) for i in range(4)]
                    evidence = {}
                    self.assertTrue(await qa_replay.replay(client,plan,1.,.01,10,evidence))
                    self.assertEqual((evidence["assigned"],evidence["null"],evidence["unique_receipts"]),(4,4,4))
                    self.assertEqual(evidence["audit_delta"]["audit_headers"],4)
                    self.assertEqual(evidence["audit_delta"]["audit_items"],8)
                    self.assertEqual(evidence["samples"][-1]["unretired_details"],0)
                finally:
                    stop.set()
                    await asyncio.wait_for(service,8)
                archive = root/"export.sqlite3"
                qa_server.export(root,archive)
            with closing(sqlite3.connect(str(archive))) as db:
                self.assertEqual(qa_mysql.verify_source(db),{"requests":4,"decisions":8,"snapshots":1})
                chunks = list(qa_mysql.batches(db,requests=1))
                self.assertEqual(sum(len(chunk) for chunk in chunks),13)
                self.assertTrue(all(len({row[0] for row in chunk})==1 for chunk in chunks))


if __name__ == "__main__":
    unittest.main()
