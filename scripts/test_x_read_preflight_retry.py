import errno
import json
import socket
import unittest
from unittest import mock

from features.x_posts import drama_selector,selector
from features.x_posts.read_failures import read_failure
from scripts import x_post_schedule_runner as runner
from scripts import test_x_post_schedule_runner as fixtures

OperationalError=type('OperationalError',(Exception,),{'__module__':'pymysql.err'})


class ReadBoundaryTests(unittest.TestCase):
    def test_mysql_connection_allowlist_excludes_permissions_sql_and_unknown_driver(self):
        for code in (2002,2003,2006,2013,2055):
            self.assertEqual(read_failure(OperationalError(code,'secret SQL/host')),{'kind':'mysql_connection','errno':code})
        for code in (1044,1045,1049,1064,1142,1146,1205,1213):self.assertIsNone(read_failure(OperationalError(code,'secret')))
        other=type('OperationalError',(Exception,),{})
        self.assertIsNone(read_failure(other(2013,'not a mysql driver')))
        self.assertIsNone(read_failure(ValueError('bad json')))
        self.assertIsNone(read_failure(socket.gaierror(socket.EAI_NONAME,'bad config')))
        self.assertEqual(read_failure(OSError(errno.ECONNRESET,'private address'))['kind'],'network_connection')

    def test_both_select_boundaries_keep_only_safe_errno_and_retry_same_read_once(self):
        for module in (selector,drama_selector):
            with self.subTest(module=module.__name__):
                cursor=mock.Mock();cursor.execute.side_effect=[OperationalError(2013,'private SQL/access'),None];cursor.fetchall.return_value=[]
                connection=mock.Mock();connection.cursor.return_value=cursor;reconnect=mock.Mock()
                with mock.patch.object(runner.time,'sleep'):
                    result=runner._readonly_preflight_call(lambda:module._cursor_rows(connection,'SELECT 1',()),reconnect=reconnect)
                self.assertEqual(result,[]);self.assertEqual(cursor.execute.call_count,2);reconnect.assert_called_once()
                cursor.execute.side_effect=OperationalError(1045,'private password')
                with self.assertRaises((selector.CandidateQueryError,drama_selector.DramaQueryError)) as rejected:
                    runner._readonly_preflight_call(lambda:module._cursor_rows(connection,'SELECT 1',()))
                self.assertIsNone(rejected.exception.read_failure);self.assertNotIn('private',str(rejected.exception))

    def test_only_structured_known_read_errors_are_retried_twice(self):
        accepted=[fixtures.SidecarError('x_sidecar_unreachable','network',503),fixtures.SidecarError('x_upstream_error','X API请求失败（HTTP 503，upstream）',502)]
        for exc in accepted:
            call=mock.Mock(side_effect=exc)
            with mock.patch.object(runner.time,'sleep'),self.assertRaises(runner.ScheduleReadPreflightTransient) as rejected:runner._readonly_preflight_call(call)
            self.assertEqual(call.call_count,2);self.assertTrue(rejected.exception.read_failure)
        rejected=[fixtures.SidecarError('x_upstream_error','X API网络请求失败',502),fixtures.SidecarError('x_upstream_error','X API请求失败（HTTP 403，forbidden）',502),fixtures.SidecarError('x_sidecar_unreachable','network',503,True),fixtures.SidecarError('x_post_account_locked','locked',409)]
        for exc in rejected:
            call=mock.Mock(side_effect=exc)
            with self.assertRaises(fixtures.SidecarError):runner._readonly_preflight_call(call)
            self.assertEqual(call.call_count,1)


class RunnerReadWaitTests(unittest.TestCase):
    setUp=fixtures.ScheduleRunnerTests.setUp
    tearDown=fixtures.ScheduleRunnerTests.tearDown
    execute=fixtures.ScheduleRunnerTests.execute
    scope_wait_fixture=fixtures.ScheduleRunnerTests.scope_wait_fixture

    def test_account_read_retry_exhaustion_waits_without_plan_and_next_tick_continues(self):
        store,sidecar,identity,_,_=self.scope_wait_fixture()
        with mock.patch.object(sidecar,'verify_account',side_effect=fixtures.SidecarError('x_sidecar_unreachable','network',503)) as verify,mock.patch.object(runner.time,'sleep'):
            result=self.execute(sidecar)
        self.assertEqual(verify.call_count,2);self.assertEqual(result['status'],'waiting_preflight')
        self.assertFalse(any(c[0] in {'create','publish','failure'} for c in sidecar.calls))
        self.assertEqual(store.query_schedule_plan('material',identity['run_date'],identity['publish_time'])['run']['status'],'claimed')
        self.execute(sidecar);self.assertEqual(len([c for c in sidecar.calls if c[0]=='create']),1)

    def test_typed_db_connection_waits_but_unclassified_operational_error_fails(self):
        store,sidecar,identity,_,_=self.scope_wait_fixture()
        typed=drama_selector.DramaQueryError('read-only failed',read_failure={'kind':'mysql_connection','errno':2013})
        result=self.execute(sidecar,material_candidate_loader=mock.Mock(side_effect=typed))
        self.assertEqual(result['status'],'waiting_preflight')
        self.assertFalse(any(c[0]=='failure' for c in sidecar.calls))
        old=drama_selector.DramaQueryError('read-only drama query failed: OperationalError')
        result=self.execute(sidecar,material_candidate_loader=mock.Mock(side_effect=old))
        self.assertEqual(result['batches'][0]['status'],'failed_preflight')

    def test_drama_null_snapshot_is_stable_but_existing_queue_or_plan_fence_refuses(self):
        store,sidecar,identity,_,_=self.scope_wait_fixture()
        original=store.query_schedule_plan('material',identity['run_date'],identity['publish_time'])['run']
        original.update(source_type='drama',material_pool_snapshot_max_id=None)
        current=dict(original);client=mock.Mock();client.query_schedule_plan.return_value={'found':True,'run':current,'queues':[]}
        client.heartbeat_schedule_run.return_value=dict(current,heartbeat_recorded=True,plan_attempt_recorded=False)
        self.assertTrue(runner._wait_for_account_scope(client,self.config,dict(identity,source_type='drama'),original,read_preflight=True))
        current['plan_attempted_at']='attempt'
        self.assertFalse(runner._wait_for_account_scope(client,self.config,identity,original,read_preflight=True))
        current['plan_attempted_at']='';client.query_schedule_plan.return_value['queues']=[{'id':99}]
        self.assertFalse(runner._wait_for_account_scope(client,self.config,identity,original,read_preflight=True))

    def test_same_sidecar_code_outside_read_boundary_does_not_defer_mutation(self):
        store,sidecar,identity,_,_=self.scope_wait_fixture()
        result=self.execute(sidecar,material_candidate_loader=mock.Mock(side_effect=fixtures.SidecarError('x_sidecar_unreachable','pool audit network error',503)))
        self.assertEqual(result['batches'][0]['status'],'failed_preflight')


if __name__=='__main__':unittest.main()
