import concurrent.futures
from contextlib import closing
from http.server import ThreadingHTTPServer
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from features.fb_page_assignment_v2.alerts import AlertHub, Relay, ROUTE, text_for, validate
from features.fb_page_assignment_v2.alerts_server import deliver, handler
from features.fb_page_assignment_v2.audit import Workers, prepare
from features.fb_page_assignment_v2.audit_sandbox import SQLiteAuditSink, isolated_mysql_sink
from features.fb_page_assignment_v2.ledger import Store, freeze_candidates


class Receipts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "hk")
        self.sink = SQLiteAuditSink(self.root / "remote.db")
        self.select = {"snapshot": {"catalog_version": "v61", "candidates": freeze_candidates([["9", "en"]]),
                                    "dramas": {"abc": {"language": "en"}}},
                       "rows": [{"drama_id": "abc", "page_id": "9", "selection_stage": "language_profile"},
                                {"drama_id": "abc", "page_id": None, "error_code": "no_profile_match"}]}

    def save(self, key="s1", fp="f1"):
        return self.store.save(key, fp, 62, ["abc", "abc"], self.select)

    def test_parallel_retry_conflict_and_order(self):
        with concurrent.futures.ThreadPoolExecutor(10) as e:
            observations = list(e.map(lambda _: self.save(), range(30)))
        self.assertEqual(len({o[1]["task_id"] for o in observations}), 1)
        response = observations[0][1]
        self.assertEqual(response["results"], [{"drama_id": "abc", "page_id": "9"},
                                               {"drama_id": "abc", "page_id": None}])
        self.assertEqual(self.store.status()["pending"], 1)
        self.assertEqual(self.save(fp="changed")[0], 409)
        self.assertEqual(Store(self.root / "hk").lookup("s1", "f1"), observations[0])

    def test_batch_and_shared_snapshot_confirm_and_retire(self):
        for k in range(4):
            self.save("key%d" % k)
        tasks = self.store.claim()
        self.assertFalse(self.store.claim())
        self.assertEqual(len(prepare(self.store, tasks)[0]), 13)
        Workers(self.store, sink=self.sink).flush(tasks)
        self.assertEqual(self.store.status()["pending"], 0)
        with self.store.db() as c:
            self.assertEqual(c.execute("SELECT count(details),count(receipt) FROM assignment").fetchone()[:], (0, 4))
            self.assertEqual(c.execute("SELECT count(packed),count(receipt) FROM candidate_snapshot").fetchone()[:], (0, 1))
        self.save("key-extra")
        more = self.store.claim()
        self.assertEqual(len(prepare(self.store, more)[0]), 3)
        Workers(self.store, sink=self.sink).flush(more)
        with closing(sqlite3.connect(self.sink.db)) as r:
            self.assertEqual(r.execute("SELECT count(*) FROM audit").fetchone()[0], 16)
        self.assertEqual(self.store.status()["pending"], 0)

    def test_persisted_incidents_can_recover_after_restart(self):
        self.store.incident("page_source", "t", 62, "lookup_failed")
        reopened = Store(self.root / "hk")
        self.assertEqual(reopened.active_incidents(), {"page_source"})
        reopened.recovery("page_source")
        self.assertEqual(reopened.active_incidents(), set())
        self.assertEqual([e["action"] for e in reopened.alert_events()], ["fault", "recovered"])

    def test_lost_commit_reconciles_original_and_cleans_after_readback(self):
        result = self.save()
        sink = self.sink
        class Lost:
            def write(self, rows, expected):
                sink.write(rows, expected)
                raise ConnectionError("lost ack")
        Workers(self.store, sink=Lost()).flush(self.store.claim())
        self.assertEqual(self.store.status()["failed"], 1)
        self.assertEqual(self.store.lookup("s1", "f1"), result)
        with self.store.db(True) as c:
            self.assertIsNotNone(c.execute("SELECT details FROM assignment").fetchone()[0])
            self.assertIsNotNone(c.execute("SELECT packed FROM candidate_snapshot").fetchone()[0])
            c.execute("UPDATE assignment SET next_try=0")
        Workers(self.store, sink=sink).flush(self.store.claim())
        self.assertEqual(self.store.status()["pending"], 0)
        self.assertEqual([e["action"] for e in self.store.alert_events()], ["fault", "recovered"])
        with closing(sqlite3.connect(sink.db)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM audit").fetchone()[0], 4)

    def test_damaged_remote_row_is_isolated(self):
        a,b = self.save("one"),self.save("two")
        tasks = self.store.claim()
        rows, expected, _, _ = prepare(self.store, tasks)
        self.sink.write(rows, expected)
        with closing(sqlite3.connect(self.sink.db)) as c:
            c.execute("UPDATE audit SET diagnostics_json='{}' WHERE task_id=? AND item_index=1",
                      (a[1]["task_id"],))
            c.commit()
        Workers(self.store, sink=self.sink).flush(tasks)
        with self.store.db() as c:
            marks = dict(c.execute("SELECT task_id,receipt IS NOT NULL FROM assignment"))
            self.assertEqual(marks,{a[1]["task_id"]:0,b[1]["task_id"]:1})
        self.assertEqual(self.store.lookup("two", "f1"), b)

    def test_disk_failure_can_notify_the_independent_receiver(self):
        class BrokenStore:
            def incident(self, *args):
                raise sqlite3.OperationalError("disk full")
            def recovery(self, kind):
                raise sqlite3.OperationalError("disk full")
        worker = Workers(BrokenStore(), sink=self.sink)
        worker.incident("persistence", "a1", 62, "disk_full")
        worker.recovery("persistence")
        self.assertEqual([e["action"] for e in worker._emergency_events],["fault","recovered"])

    @unittest.skipUnless(os.environ.get("FB_PAGE_V2_QA_CONTAINER"), "isolated MySQL 5.7 container required")
    def test_mysql_commit_and_readback(self):
        sink=isolated_mysql_sink(os.environ["FB_PAGE_V2_QA_CONTAINER"])
        result=self.save()
        Workers(self.store,sink=sink).flush(self.store.claim())
        self.assertEqual(self.store.status()["pending"],0)
        self.assertEqual(self.store.lookup("s1","f1"),result)


class Incidents(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.now = 10000.
        self.hub = AlertHub(self.directory.name, clock=lambda:self.now)

    def ev(self, k, external="s1", action="fault"):
        return {"id":k,"kind":"connection","action":action,"external_task_id":external,
                "page_group_id":62,"detail":"connect_timeout","at":self.now}

    def test_existing_conflict_summary_is_rendered_in_chinese_without_changing_receipt(self):
        payload = dict(at=self.now, kind="request_conflict", reason="idempotency_conflict",
                       mode="summary", affected=2, new_occurrences=1, since_last=1,
                       examples=[dict(external_task_id="3092716", page_group_id=24,
                                      detail="idempotency_conflict")])
        text = text_for(payload)
        self.assertIn("请求内容一致性校验", text)
        self.assertEqual(text.count("同一子批标识已用于不同的 Page 池或剧目列表"), 2)
        self.assertIn("子批 3092716，Page 池 24", text)
        self.assertIn("本次新增异常 1 次，新增受影响子批 1 个", text)
        self.assertIn("拆分后的不同子批", text)
        self.assertNotIn("idempotency_conflict", text)
        self.assertEqual(payload["reason"], "idempotency_conflict")
        payload["mode"] = "recovered"
        self.assertIn("已恢复", text_for(payload))
        self.assertNotIn("处理建议", text_for(payload))

    def test_new_reason_has_chinese_fallback_and_database_number_is_preserved(self):
        payload = dict(at=self.now, kind="audit_sync", reason="OperationalError.2013",
                       mode="first", affected=1,
                       examples=[dict(external_task_id="s1", page_group_id=24, detail="new_failure")])
        text = text_for(payload)
        self.assertIn("数据库操作失败（错误码 2013）", text)
        self.assertIn("未识别的异常，请核查服务日志（代码：new_failure）", text)

    def test_first_summary_recovery_retry_and_independent_subbatches(self):
        first = self.ev("a")
        self.assertEqual(self.hub.accept({"events":[first]}), ["a"])
        self.hub.accept({"events":[first]})
        m = self.hub.claim()
        self.assertEqual((m["payload"]["mode"],m["payload"]["affected"]),("first",1))
        self.hub.finish(m,"sent","om_one")
        self.now += 3
        self.hub.accept({"events":[self.ev("b"),self.ev("c","s2")]})
        self.hub.summaries();self.assertIsNone(self.hub.claim())
        self.now += 300
        self.hub.summaries();m=self.hub.claim()
        self.assertEqual((m["payload"]["mode"],m["payload"]["affected"],
                          m["payload"]["new_occurrences"]),("summary",2,2))
        self.hub.finish(m,"sent","om_two")
        self.now += 1
        self.hub.accept({"events":[self.ev("d",action="recovered")]})
        m=self.hub.claim();self.assertEqual(m["payload"]["mode"],"recovered")
        self.hub.finish(m,"sent","om_three");self.assertIsNone(self.hub.claim())
        self.hub.accept({"events":[self.ev("e",action="recovered")]})
        self.assertIsNone(self.hub.claim())

    def test_unknown_send_not_blindly_retried(self):
        self.hub.accept({"events":[self.ev("x")]})
        class Sender:
            def send(self, msg):
                raise TimeoutError("test")
        self.assertTrue(deliver(self.hub,Sender()))
        self.now+=600
        self.assertIsNone(self.hub.claim())
        self.assertEqual(self.hub.status(),{"unknown":1})
        self.hub.interrupted()
        self.assertEqual(self.hub.status(),{"unknown":1})

    def test_late_fault_after_recovery_does_not_reopen_or_join_a_new_episode(self):
        self.hub.accept({"events":[self.ev("recovered",action="recovered")]})
        self.now += 2
        self.hub.accept({"events":[self.ev("new"),self.ev("old")|{"at":self.now-5}]})
        with self.hub.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM episode WHERE closed IS NULL").fetchone()[0],1)
            self.assertEqual(db.execute("SELECT occurrences FROM episode WHERE closed IS NULL").fetchone()[0],1)
            self.assertEqual([r[0] for r in db.execute("SELECT mode FROM message ORDER BY seq")],
                             ["first","late_recovered"])

    def test_lost_sender_claim_keeps_unknown_fence(self):
        self.hub.accept({"events":[self.ev("x")]})
        self.assertIsNotNone(self.hub.claim())
        self.hub.interrupted()
        self.assertEqual(self.hub.status(),{"unknown":1})

    def test_distinct_causes_each_report_and_only_close_on_fresh_recovery(self):
        self.hub.accept({"events":[self.ev("x"),self.ev("y")|{"detail":"peer_disconnected"}]})
        first = self.hub.claim()
        self.hub.finish(first,"sent","one")
        second = self.hub.claim()
        self.hub.finish(second,"sent","two")
        self.assertEqual({first["payload"]["reason"],second["payload"]["reason"]},
                         {"connect_timeout","peer_disconnected"})
        self.hub.accept({"events":[self.ev("r", action="recovered")|{"at":self.now-10}]})
        self.assertIsNone(self.hub.claim())

    def test_authenticated_http_ingress_deduplicates_after_client_retry(self):
        tok = "s"*36
        server=ThreadingHTTPServer(("127.0.0.1", 0),handler(self.hub,("Bearer "+tok).encode(),threading.Event()))
        thread=threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        try:
            url="http://127.0.0.1:%d%s" % (server.server_port, ROUTE)
            with self.assertRaises(Exception):
                Relay(url,"wrong").push([self.ev("z")])
            self.assertIsNone(self.hub.claim())
            client=Relay(url,tok)
            client.push([self.ev("z")]);client.push([self.ev("z")])
            self.assertEqual(self.hub.status(),{"pending":1})
        finally:
            server.shutdown();server.server_close();thread.join()

    def test_validation(self):
        for case in ({"events":[]},{"events":[self.ev("x")|{"kind":"bad kind"}]},
                     {"events":[self.ev("x")|{"page_group_id":True}]}):
            with self.assertRaises(ValueError):
                validate(case,self.now)


if __name__ == "__main__":
    unittest.main()
