"""Real-ledger legacy readback tests: platform writes are forbidden."""
from datetime import timedelta
import os
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from scripts import test_tt_auto_post_publisher as fixtures
from scripts import test_tt_posts_service as pool_fixtures
from scripts.tt_post_runner import TTPostSidecarClient, execute_runner_tick
from scripts.tt_auto_post_reconcile import PATH, reconcile_once
from features.pipeline_dispatch.legacy_admission import handoff
from features.pipeline_dispatch.tt_readback import reconcile_next
from features.tt_posts.service import GPUClientError


class LegacyTTReadbackTests(unittest.TestCase):
    setUp = fixtures.TTAutoPostPublisherIntegrationTests.setUp
    reserved_task = fixtures.TTAutoPostPublisherIntegrationTests.reserved_task
    executor = fixtures.TTAutoPostPublisherIntegrationTests.executor

    def pending(self, suffix="readback", **fields):
        task = self.reserved_task(suffix=suffix)
        values = dict(status="unknown", publish_id="pub-"+suffix, publish_attempt_count=1,
                      unknown_outcome=1, claim_phase="reconcile", lease_expires_at_utc="", claim_token="")
        values.update(fields)
        with self.store._transaction() as c:
            c.execute("UPDATE tt_auto_task SET " + ",".join(k+"=?" for k in values) + " WHERE id=?", (*values.values(),task.id))
            c.execute("UPDATE tt_auto_run SET status='running' WHERE id=?", (task.run_id,))
        return self.store.get_task(task.id)

    def readback_executor(self, result):
        gpu = fixtures.FakeGPU(reconcile_result=result)
        for name in ("prepare", "creator_info", "publish"):
            setattr(gpu, name, Mock(side_effect=AssertionError("platform write/preparation forbidden")))
        executor = self.executor(gpu)
        for name in ("execute_next", "_select", "_prepare", "_publish", "_reconcile"):
            setattr(executor, name, Mock(side_effect=AssertionError("legacy execution forbidden")))
        return executor, gpu

    def attempts(self, task):
        with self.store._transaction() as c:
            return c.execute("SELECT publish_attempt_count FROM tt_auto_task WHERE id=?",(task.id,)).fetchone()[0]

    def test_pending_preserves_unknown_and_original_video_attempt(self):
        task = self.pending()
        executor, gpu = self.readback_executor({"publish_id":task.publish_id, "state":"processing"})
        result = reconcile_next(executor, "readback")
        self.assertTrue(result["claimed"])
        final = self.store.get_task(task.id)
        self.assertEqual((final.status, final.unknown_outcome, self.attempts(final)), ("unknown",True,1))
        self.assertEqual(final.publish_id,task.publish_id)
        self.assertEqual(len(gpu.reconcile_calls),1)
        with self.store._transaction() as c:
            self.assertEqual(c.execute("SELECT next_attempt_at_utc FROM tt_auto_task WHERE id=?",(task.id,)).fetchone()[0], (self.clock()+timedelta(minutes=5)).isoformat(timespec="seconds"))
            self.assertEqual(c.execute("SELECT claim_token FROM tt_auto_task WHERE id=?",(task.id,)).fetchone()[0],"")

    def test_confirmed_original_id_is_required_before_success_or_definite_failure(self):
        for outcome, expected in (("published","published"),("failed","failed")):
            with self.subTest(outcome=outcome):
                task = self.pending(suffix=outcome)
                executor, _ = self.readback_executor({"publish_id":task.publish_id, "state":outcome})
                reconcile_next(executor, "readback")
                final = self.store.get_task(task.id)
                self.assertEqual((final.status, final.unknown_outcome, self.attempts(final)),(expected,False,1))

    def test_conflicting_id_missing_gpu_ledger_or_transport_error_never_falls_back(self):
        for index, remote in enumerate(({"publish_id":"other-id", "state":"published"},
                GPUClientError("publish_ledger_not_found", "missing ledger", 404),
                GPUClientError("tt_post_unreachable", "temporary failure", 503))):
            with self.subTest(remote=type(remote).__name__):
                task = self.pending(suffix=str(index))
                executor, _ = self.readback_executor(remote)
                result = reconcile_next(executor, "readback",after_task_id=task.id-1)
                self.assertTrue(result["claimed"])
                final = self.store.get_task(task.id)
                self.assertEqual((final.status,final.unknown_outcome,self.attempts(final)),("retry_wait",True,1))
                self.assertEqual(final.publish_id,task.publish_id)

    def test_missing_publish_id_active_lease_and_pipeline_owned_are_not_claimed(self):
        missing = self.pending(suffix="missing",publish_id="")
        active = self.pending(suffix="active",claim_token="active-token",lease_expires_at_utc=(self.clock()+timedelta(hours=1)).isoformat())
        owned = self.pending(suffix="owned")
        interrupted_binding = self.pending(suffix="native-binding",claim_worker="pipeline:interrupted",claim_token="old-pipeline-token")
        with self.store._transaction() as c:
            c.execute("CREATE TABLE pipeline_tt_claim(source_key TEXT)")
            c.execute("INSERT INTO pipeline_tt_claim VALUES(?)",("tt:auto:"+str(owned.id),))
        executor, gpu = self.readback_executor({"publish_id":"pub-known", "state":"published"})
        self.assertFalse(reconcile_next(executor, "readback")["claimed"])
        self.assertEqual(gpu.reconcile_calls,[])
        for task in (missing,active,owned,interrupted_binding):
            self.assertEqual(self.store.get_task(task.id).status,"unknown")

    def test_readback_does_not_wait_behind_unstarted_old_same_account_task(self):
        reserved = self.reserved_task(suffix="old-unstarted")
        task = self.pending(suffix="known")
        executor, _ = self.readback_executor({"publish_id":"pub-known", "state":"published"})
        self.assertEqual(reconcile_next(executor, "readback")["task"]["id"],task.id)
        self.assertEqual(self.store.get_task(reserved.id).status,"reserved")

    def test_runtime_frozen_video_route_and_published_code_sync_are_preserved(self):
        task = self.pending(suffix="frozen-route")
        executor, default_gpu = self.readback_executor({"publish_id":"wrong-default", "state":"published"})
        frozen_gpu = fixtures.FakeGPU(reconcile_result={"publish_id":task.publish_id, "state":"published"})
        default_gpu.reconcile = Mock(side_effect=AssertionError("default route must not be used"))
        executor._video_route_for_task = Mock(return_value=SimpleNamespace(gpu_client=frozen_gpu))
        executor._sync_code_state = Mock()
        result = reconcile_next(executor, "readback")
        self.assertEqual(result["task"]["status"], "published")
        requested = executor._video_route_for_task.call_args.args[0]
        self.assertEqual((requested.template_id, requested.template_version), (task.template_id, task.template_version))
        default_gpu.reconcile.assert_not_called()
        self.assertEqual(len(frozen_gpu.reconcile_calls), 1)
        self.assertEqual(executor._sync_code_state.call_args.args[1], "published")
        self.assertEqual(executor._sync_code_state.call_args.kwargs, {"best_effort":True})


class ReadbackRunnerTests(unittest.TestCase):
    def test_pool_and_direct_readback_lists_exclude_pipeline_owned_not_ui_history(self):
        fixture=pool_fixtures.ServiceLifecycleTests()
        fixture.setUp();self.addCleanup(fixture.tearDown)
        service,queue,direct=fixture.create_mixed_publish_tasks()
        with service.store._transaction() as c:
            c.execute("UPDATE tt_post_queue SET status='reconciling',publish_id='pool-existing' WHERE id=?",(queue["id"],))
            c.execute("UPDATE tt_post_direct_test SET status='reconciling',publish_id='direct-existing' WHERE id=?",(direct["id"],))
        self.assertEqual(len(service.reconciling()["items"]),1)
        self.assertEqual(len(service.direct_reconciling()["items"]),1)
        with service.store._transaction() as c:
            c.execute("CREATE TABLE pipeline_tt_claim(source_key TEXT)")
            c.executemany("INSERT INTO pipeline_tt_claim VALUES(?)",[("tt:queue:"+str(queue["id"]),),("tt:direct_test:"+str(direct["id"]),)])
        self.assertEqual(service.reconciling()["items"],[])
        self.assertEqual(service.direct_reconciling()["items"],[])
        self.assertEqual(len(service.store.list_direct_tests(status="reconciling")),1)

    def test_auto_cli_is_bounded_and_only_calls_readback_route(self):
        calls=[]
        def post(path,payload,timeout):
            calls.append((path,payload,timeout))
            return {"claimed":True,"task":{"id":payload["after_task_id"]+1,"publish_id":"existing"}}
        result=reconcile_once(SimpleNamespace(worker_id="reader"),limit=3,client=SimpleNamespace(post=post))
        self.assertEqual(result["checked"],3)
        self.assertEqual([call[0] for call in calls],[PATH]*3)
        self.assertEqual([call[1]["after_task_id"] for call in calls],[0,1,2])

    def test_actual_handoff_empty_claims_preserve_pool_scheduling_and_both_readbacks(self):
        config=pool_fixtures.RunnerTests.config(self)
        calls=[]; direct_pending=[True]
        class GuardedClient(TTPostSidecarClient):
            def _request(self,method,path,payload=None,timeout=None):
                calls.append(path)
                guarded=handoff("tt",path)
                if guarded is not None:return guarded
                if path=="/internal/tt-posts/schedules/due":return {"items":[]}
                if path.startswith("/internal/tt-posts/direct-tests/reconciling"):
                    return {"items":[{"id":11,"publish_id":"direct-id"}] if direct_pending[0] else []}
                if path.startswith("/internal/tt-posts/reconciling"):
                    return {"items":[{"id":12,"publish_id":"pool-id","status":"reconciling"}]}
                if path.endswith("/reconcile"):
                    direct_pending[0]=False
                    return {"item":{"id":11 if "direct-tests" in path else 12,"status":"published"}}
                raise AssertionError("unexpected route: "+path)
        client=GuardedClient(config.internal_url,config.internal_token,timeout=config.timeout,
            schedule_timeout=config.schedule_timeout,publish_timeout=config.publish_timeout,reconcile_timeout=config.reconcile_timeout)
        with patch.dict(os.environ,{"PIPELINE_TT_DISPATCH_ENABLED":"1"}):
            first=execute_runner_tick(config,client=client)
            second=execute_runner_tick(config,client=client)
        self.assertEqual(calls[0],"/internal/tt-posts/schedules/due")
        self.assertIn("/internal/tt-posts/direct-tests/11/reconcile",calls)
        self.assertIn("/internal/tt-posts/queue/12/reconcile",calls)
        self.assertFalse(any(path.endswith("/publish") for path in calls))
        self.assertEqual((first["direct_reconcile_count"],second["reconcile_count"]),(1,1))


if __name__ == "__main__":unittest.main()
