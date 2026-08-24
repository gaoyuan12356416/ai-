import unittest

from features.fb_auto_posts.publisher import AutoPostExecutor, GraphResult, RequestsGraphTransport
from features.fb_auto_posts.links import FBPostLinkError, build_short_url, build_w2a_url
from features.fb_auto_posts.repositories import PageCredential


class Store:
    def __init__(self): self.attempts=[]; self.completed=[]; self.reconciled=[]; self.task={"id": 1, "page_id": "10001", "source_media_url": "https://cdn.example/source.mp4", "prepared_media_url": "https://cdn.example/prepared.mp4", "media_url": "https://cdn.example/prepared.mp4", "message_text": "hello"}
    def claim_next(self, *_args): return dict(self.task)
    def record_attempt(self, *args, **kwargs): self.attempts.append((args,kwargs))
    def complete_task(self, task_id, outcome): self.completed.append(outcome); return outcome
    def complete_submitted_with_attempt(self, task_id, sequence, **kwargs):
        self.attempts.append(((task_id,sequence),kwargs)); outcome={"status":"submitted","graph_post_id":kwargs["graph_post_id"]}; self.completed.append(outcome); return outcome
    def claim_submitted(self, *_args): return {"id": 1, "page_id": "10001", "graph_post_id": "vid_9"}
    def reconcile_task(self, task_id, status, **kwargs): self.reconciled.append((status,kwargs)); return {"status":status}


class Pages:
    def eligible_credentials(self, _page): return [PageCredential("10001","7","1","token-A"), PageCredential("10001","8","2","token-B")]


class Graph:
    def __init__(self, results, reconcile=None):
        self.results=list(results); self.calls=0
        self.reconcile_results = list(reconcile) if isinstance(reconcile, list) else [reconcile]
        self.reconcile_calls=[]
    def publish_video(self, *_args): self.calls+=1; return self.results.pop(0)
    def reconcile_video(self, _object_id, credential):
        self.reconcile_calls.append(credential.credential_id)
        return self.reconcile_results.pop(0)


class PublisherTests(unittest.TestCase):
    def test_graph_timeout_cannot_exceed_static_lease_budget(self):
        self.assertEqual(RequestsGraphTransport(timeout_seconds=120).timeout_seconds, 120)
        with self.assertRaises(ValueError):
            RequestsGraphTransport(timeout_seconds=121)

    def test_definite_failure_uses_another_unused_token_then_submits(self):
        store=Store(); graph=Graph([GraphResult("definite_failure",error_code="fb_graph_190"),GraphResult("success",post_id="vid_9")]); result=AutoPostExecutor(store,Pages(),graph,live_enabled=True).execute_next("w")
        self.assertEqual(result["status"],"submitted"); self.assertEqual(graph.calls,2); self.assertEqual(len(store.attempts),2)

    def test_each_execution_refreshes_dynamic_page_credentials(self):
        class DynamicPages(Pages):
            def __init__(self): self.calls=0
            def eligible_credentials(self, page):
                self.calls+=1
                return [] if self.calls==1 else super().eligible_credentials(page)
        store=Store(); pages=DynamicPages(); graph=Graph([GraphResult("success",post_id="vid_9")])
        executor=AutoPostExecutor(store,pages,graph,live_enabled=True,min_request_interval_seconds=0)
        self.assertEqual(executor.execute_next("w1")["error_code"],"fb_page_missing_eligible_token")
        self.assertEqual(executor.execute_next("w2")["status"],"submitted")
        self.assertEqual(pages.calls,2); self.assertEqual(graph.calls,1)

    def test_unknown_never_fails_over(self):
        store=Store(); graph=Graph([GraphResult("unknown",error_code="fb_graph_network_outcome_unknown")]); result=AutoPostExecutor(store,Pages(),graph,live_enabled=True).execute_next("w")
        self.assertEqual(result["status"],"unknown"); self.assertEqual(graph.calls,1)

    def test_returned_id_is_submitted_not_published(self):
        store=Store(); result=AutoPostExecutor(store,Pages(),Graph([GraphResult("success",post_id="vid_9")]),live_enabled=True).execute_next("w")
        self.assertEqual(result["status"],"submitted"); self.assertEqual(store.completed[0]["graph_post_id"],"vid_9")

    def test_reconcile_ready_becomes_published_without_post(self):
        store=Store(); graph=Graph([],GraphResult("success",post_id="vid_9")); result=AutoPostExecutor(store,Pages(),graph,live_enabled=True).reconcile_next("r")
        self.assertEqual(result["status"],"published"); self.assertEqual(graph.calls,0); self.assertEqual(len(graph.reconcile_calls),1)

    def test_reconcile_credential_failure_uses_another_unused_token(self):
        store=Store(); graph=Graph([], [GraphResult("credential_failure",error_code="fb_graph_190"), GraphResult("success",post_id="vid_9")])
        result=AutoPostExecutor(store,Pages(),graph,live_enabled=True,rng=__import__("random").Random(1),min_request_interval_seconds=0).reconcile_next("r")
        self.assertEqual(result["status"],"published"); self.assertEqual(len(graph.reconcile_calls),2); self.assertEqual(len(set(graph.reconcile_calls)),2)

    def test_reconcile_all_credentials_rejected_becomes_terminal_unknown(self):
        store=Store(); graph=Graph([], [GraphResult("credential_failure",error_code="fb_graph_190"), GraphResult("credential_failure",error_code="fb_graph_10")])
        result=AutoPostExecutor(store,Pages(),graph,live_enabled=True,min_request_interval_seconds=0).reconcile_next("r")
        self.assertEqual(result["status"],"unknown"); self.assertEqual(len(graph.reconcile_calls),2)
        self.assertEqual(store.reconciled[-1][1]["error_code"],"fb_graph_reconcile_all_credentials_rejected")

    def test_reconcile_indeterminate_response_stays_submitted_without_failover(self):
        store=Store(); graph=Graph([], [GraphResult("unknown",error_code="fb_graph_reconcile_transient"), GraphResult("success",post_id="vid_9")])
        result=AutoPostExecutor(store,Pages(),graph,live_enabled=True,min_request_interval_seconds=0).reconcile_next("r")
        self.assertEqual(result["status"],"submitted"); self.assertEqual(len(graph.reconcile_calls),1)

    def test_reconcile_processing_failure_never_uses_another_token(self):
        store=Store(); graph=Graph([], [GraphResult("definite_failure",error_code="fb_graph_video_processing_failed"), GraphResult("success",post_id="vid_9")])
        result=AutoPostExecutor(store,Pages(),graph,live_enabled=True,min_request_interval_seconds=0).reconcile_next("r")
        self.assertEqual(result["status"],"failed_without_retry"); self.assertEqual(len(graph.reconcile_calls),1)

    def test_live_gate_calls_nothing(self):
        store=Store(); graph=Graph([]); result=AutoPostExecutor(store,Pages(),graph,live_enabled=False).execute_next("w")
        self.assertEqual(result["status"],"live_gate_closed"); self.assertEqual(graph.calls,0)

    @staticmethod
    def _long_url():
        return build_w2a_url({"username":"10001","timestamp":1787191200,"material_language":"en","drama_name":"Drama","tag":"hook","task_id":1,"page_name":"Page","page_id":"10001","material_name":"Material","material_id":"501","content_id":"d1"})

    def test_short_wrapper_is_materialized_before_token_and_graph(self):
        events=[]; store=Store(); short=build_short_url(1); store.task.update({"short_url":short,"long_url":self._long_url(),"message_text":"watch "+short})
        class OrderedPages(Pages):
            def eligible_credentials(self,page): events.append("tokens"); return super().eligible_credentials(page)
        class OrderedGraph(Graph):
            def publish_video(self,*args): events.append("graph"); return super().publish_video(*args)
        def writer(root,task_id,long_url): events.append("wrapper"); self.assertEqual((root,task_id),("/safe/fb",1)); self.assertEqual(long_url,self._long_url())
        graph=OrderedGraph([GraphResult("success",post_id="vid_9")])
        result=AutoPostExecutor(store,OrderedPages(),graph,live_enabled=True,short_link_root="/safe/fb",short_link_writer=writer,min_request_interval_seconds=0).execute_next("w")
        self.assertEqual(result["status"],"submitted"); self.assertEqual(events,["wrapper","tokens","graph"])

    def test_short_wrapper_failure_blocks_token_and_graph(self):
        store=Store(); short=build_short_url(1); store.task.update({"short_url":short,"long_url":self._long_url(),"message_text":"watch "+short})
        class CountingPages(Pages):
            def __init__(self): self.calls=0
            def eligible_credentials(self,page): self.calls+=1; return super().eligible_credentials(page)
        def failed_writer(*_args): raise FBPostLinkError("fb_auto_short_link_write_failed","磁盘不可用",500)
        pages=CountingPages(); graph=Graph([GraphResult("success",post_id="must-not-run")])
        result=AutoPostExecutor(store,pages,graph,live_enabled=True,short_link_root="/safe/fb",short_link_writer=failed_writer).execute_next("w")
        self.assertEqual(result["status"],"failed"); self.assertEqual(result["error_code"],"fb_auto_short_link_write_failed")
        self.assertEqual(pages.calls,0); self.assertEqual(graph.calls,0)


if __name__ == "__main__": unittest.main()
