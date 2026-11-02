"""Offline canonical projection and FB execution-bridge safety checks."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime, timezone, timedelta

from features.pipeline_dispatch.adapters import AdapterRegistry, AdapterError, LedgerAdapter, LedgerSpec, public_descriptor, ClosingConnection, RemoteCanonicalDriver
from features.pipeline_dispatch.fb_native import FBNativeBridge
from features.fb_auto_posts.repositories import PageCredential
from features.fb_auto_posts.core import FBAutoPostStore
from features.fb_video_publish.core import Store as FBVideoStore
from features.fb_video_publish.guards import manual_block
from features.pipeline_dispatch.canonical_holds import pause_row


class ProjectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / "ledger.db")
        with sqlite3.connect(self.path, factory=ClosingConnection) as c:
            c.executescript("""CREATE TABLE tt_auto_task(id INTEGER PRIMARY KEY,status TEXT,account_id TEXT,account_username TEXT,
                scheduled_at_utc TEXT,created_at TEXT,run_id INTEGER,material_id TEXT,content_id TEXT,
                prepared_media_url TEXT,prepared_output_sha256 TEXT,prepared_output_size INTEGER,
                publish_attempt_count INTEGER,unknown_outcome INTEGER,publish_id TEXT);
                CREATE TABLE tt_auto_run(id INTEGER PRIMARY KEY,created_by_user_id TEXT,created_by_tenant_key TEXT);
                INSERT INTO tt_auto_run VALUES(1,'owner1','tenant1');""")
        self.adapter = LedgerAdapter(LedgerSpec("tt", "auto", self.path, "tt_auto_task", module="tt_posts"))
        self.registry = AdapterRegistry([self.adapter])

    def add(self, ident, status="pending", attempts=0, unknown=0, url=""):
        with sqlite3.connect(self.path, factory=ClosingConnection) as c:
            c.execute("INSERT INTO tt_auto_task VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (ident,status,"9","user9","2026-11-01T05:00:00Z","2026-11-01T04:00:00Z",1,"88","99",url,"a"*64,100,attempts,unknown,"post" if status=="published" else ""))

    def test_owner_and_created_cutoff_use_canonical_times(self):
        self.add(1)
        offer = self.registry.scan()[0]
        self.assertEqual((offer["owner_user_id"],offer["owner_tenant_key"]), ("owner1","tenant1"))
        self.assertEqual(offer["source_created_at"], "2026-11-01T04:00:00Z")
        self.assertEqual(offer["required_module"], "tt_posts")
        self.assertEqual(self.registry.apply_cutover("tt", "2026-11-02T00:00:00Z")["paused_source_keys"], ["tt:auto:1"])

    def test_prepared_backlog_is_paused_without_destroying_artifact(self):
        self.add(1,"ready",url="https://media.example/a.mp4")
        self.assertEqual(self.registry.scan()[0]["stage"],"publish")
        report=self.registry.apply_cutover("tt","2026-11-02T00:00:00Z")
        self.assertEqual(report["canonical_mutations"],0)
        self.assertEqual(self.adapter.read(1)["prepared_media_url"],"https://media.example/a.mp4")

    def test_unknown_and_attempted_are_reconcile_only(self):
        self.add(1,"ready",1,0)
        self.add(2,"unknown",0,1)
        rows=self.registry.scan()
        self.assertTrue(all(x["reconcile_only"] and not x["execution_available"] for x in rows))
        self.assertEqual(self.registry.apply_cutover("tt","2026-11-02T00:00:00Z")["paused_source_keys"],[])

    def test_inflight_exemption_is_per_source_not_batch(self):
        self.add(1,"preparing")
        self.add(2)
        report=self.registry.apply_cutover("tt","2026-11-02T00:00:00Z")
        self.assertEqual(report["grandfathered_source_keys"],["tt:auto:1"])
        self.assertEqual(report["paused_source_keys"],["tt:auto:2"])

    def test_history_cursor_and_active_rotation(self):
        for i in range(1,7):self.add(i,"published" if i<4 else "pending")
        a=self.registry.scan(limit=2)
        b=self.registry.scan(limit=2)
        self.assertEqual({x["source_id"] for x in a if x["observation_only"]},{"1","2"})
        self.assertIn("3", {x["source_id"] for x in b if x["observation_only"]})
        self.assertIn("6",{x["source_id"] for x in b})
        cursors=self.registry.export_cursors()
        fresh=AdapterRegistry([LedgerAdapter(self.adapter.spec)])
        fresh.restore_cursors(cursors)
        self.assertEqual(fresh.export_cursors(),cursors)

    def test_watched_old_active_terminal_transition_is_not_lost(self):
        self.add(1);self.add(2,"published")
        self.registry.scan()
        with sqlite3.connect(self.path, factory=ClosingConnection) as c:c.execute("UPDATE tt_auto_task SET status='published',publish_id='new-post' WHERE id=1")
        rows=self.registry.scan()
        terminal=next(r for r in rows if r["source_id"]=="1")
        self.assertEqual(terminal["publish_id"],"new-post")
        self.assertTrue(terminal["observation_only"])

    def test_unconnected_driver_cannot_execute(self):
        self.add(1)
        with self.assertRaisesRegex(AdapterError,"canonical_execution_not_connected"):
            self.registry.reserve(self.registry.scan()[0],{})

    def test_control_only_driver_never_advertises_execution(self):
        self.add(1)
        self.adapter.driver = RemoteCanonicalDriver(18833, '/internal/pipeline-dispatch/x-auto', 'test-private', control_only=True)
        offer = self.registry.scan()[0]
        self.assertFalse(offer['execution_available'])
        with self.assertRaisesRegex(AdapterError, 'canonical_execution_not_connected'):
            self.registry.reserve(offer, {})

    def test_published_requires_platform_id_and_skips_are_not_failures(self):
        self.add(1, 'published')
        self.add(2, 'no_candidate')
        with sqlite3.connect(self.path, factory=ClosingConnection) as c:
            c.execute("UPDATE tt_auto_task SET publish_id='' WHERE id=1")
        offers = {row['source_id']: row for row in self.registry.scan()}
        self.assertEqual(offers['1']['status'], 'unknown')
        self.assertEqual(offers['2']['status'], 'completed')
        self.assertTrue(offers['2']['metadata_only'])
        proof = self.registry._verified_result(self.adapter, offers['2'], {})
        self.assertTrue(proof['metadata_only'])
        self.assertTrue(proof['dispatch_completed'])
        self.assertFalse(proof['published'])
        self.assertFalse(proof['definitive_failure'])

    def test_failed_preparation_is_not_counted_as_failed_publication(self):
        cases = [
            (LedgerSpec('x', 'queue', '', 'x_post_queue'), {'id': 1, 'status': 'failed', 'preflight_size': 0}),
            (LedgerSpec('fb', 'video', '', 'fb_video_task', identity='task_id'), {'task_id': '1', 'status': 'failed', 'prepare_state': 'failed'}),
            (LedgerSpec('fb', 'auto', '', 'fb_auto_task'), {'id': 1, 'status': 'failed'}),
        ]
        for spec, row in cases:
            with self.subTest(channel=spec.channel, family=spec.family):
                self.assertEqual(LedgerAdapter(spec).project(row)['stage'], 'prepare')
                self.assertEqual(LedgerAdapter(spec).project(dict(row, publish_attempt_count=1))['stage'], 'publish')

    def test_recent_terminal_priority_does_not_wait_for_old_history(self):
        for ident in range(1, 21):
            self.add(ident, 'published')
        with sqlite3.connect(self.path, factory=ClosingConnection) as c:
            c.execute("UPDATE tt_auto_task SET created_at='2026-01-01T00:00:00Z' WHERE id<20")
        offers = self.registry.scan(limit=2, now='2026-11-02T00:00:00Z')
        self.assertIn('20', {row['source_id'] for row in offers})
        self.assertEqual(self.registry.collection_status()['tt:auto'], {'recent_complete': True, 'history_complete': False})

    def test_secret_rejection_is_recursive(self):
        for key in ("refresh_token","session_uri","credential_envelope"):
            with self.assertRaisesRegex(AdapterError,"descriptor_contains_secret"):
                public_descriptor({"request":[{key:"private"}]})


class FakeFBStore:
    def __init__(self,path):
        self.path=path
        self.now_fn=lambda:datetime(2026,11,2,tzinfo=timezone.utc)
        with self.connect() as c:
            c.executescript("""CREATE TABLE fb_auto_task(id INTEGER PRIMARY KEY,status TEXT,page_id TEXT,lease_owner TEXT,
                lease_expires_at_utc TEXT,graph_post_id TEXT,unknown_outcome INTEGER);
                INSERT INTO fb_auto_task VALUES(4,'ready','page4','','','',0);""")
        self.attempts=[]
    def connect(self):
        c=sqlite3.connect(self.path,factory=ClosingConnection);c.row_factory=sqlite3.Row;return c
    def claim_next(self,worker_id,lease_seconds,task_id=None):
        if task_id!=4:return None
        with self.connect() as c:
            if c.execute("SELECT status FROM fb_auto_task WHERE id=4").fetchone()[0]!='ready':return None
            until=(self.now_fn()+timedelta(seconds=1200)).isoformat().replace('+00:00','Z')
            c.execute("UPDATE fb_auto_task SET status='running',lease_owner=?,lease_expires_at_utc=? WHERE id=4",(worker_id,until))
        return {"id":4,"status":"running","page_id":"page4","lease_owner":worker_id,"prepared_media_url":"https://cdn.example/output.mp4","media_url":"https://cdn.example/output.mp4","source_media_url":"https://cdn.example/source.mp4","message_text":"frozen body","short_url":"","long_url":""}
    def record_attempt(self,task_id,sequence,**data):self.attempts.append((sequence,data))
    def complete_submitted_with_attempt(self,task_id,sequence,**data):
        with self.connect() as c:c.execute("UPDATE fb_auto_task SET status='submitted',graph_post_id=? WHERE id=?",(data["graph_post_id"],task_id))
        self.attempts.append((sequence,data));return {"status":"submitted"}
    def complete_task(self,task_id,outcome):
        with self.connect() as c:c.execute("UPDATE fb_auto_task SET status=?,unknown_outcome=? WHERE id=?",(outcome["status"],int(outcome["status"]=='unknown'),task_id))
        return outcome


class FBBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=FakeFBStore(str(Path(self.tmp.name)/'fb.db'))
        self.credentials=[PageCredential('page4','user4',str(i),'private-'+str(i)) for i in range(10)]
        self.runtime=SimpleNamespace(store=self.store,pages=SimpleNamespace(eligible_credentials=lambda _:self.credentials),executor=SimpleNamespace(live_enabled=True))
        self.bridge=FBNativeBridge(self.runtime)
        self.job={'source_key':'fb:auto:4','channel':'fb','family':'auto','source_id':'4','stage':'publish','generation':1}
        self.claim={'claim_token':'c'*32,'node_id':'arbitrary-new-node'}
        self.descriptor=self.bridge.reserve(self.job,self.claim)
    def operation(self,i,previous=None):
        request=dict(self.descriptor['request'])
        if previous:request['previous_receipt']=previous
        return {'operation':'fb_video_publish','operation_id':'operation-'+str(i),'account_resource':'fb:page:page4','request':request,'request_sha256':'a'*64}
    def test_descriptor_never_contains_credentials(self):
        public_descriptor(self.descriptor)
        self.assertNotIn('private-',json.dumps(self.descriptor))
    def test_wrong_page_or_changed_body_rejected_before_intent(self):
        operation=self.operation(1);operation['account_resource']='fb:page:other'
        with self.assertRaises(AdapterError):self.bridge.begin_operation(self.job,self.claim,operation)
        operation=self.operation(1);operation['request']['message']='changed'
        with self.assertRaises(AdapterError):self.bridge.begin_operation(self.job,self.claim,operation)
    def test_no_fixed_eight_credential_limit_and_no_unknown_retry(self):
        previous=None
        for i in range(10):
            result=self.bridge.begin_operation(self.job,self.claim,self.operation(i,previous))
            self.assertIn('access_token',result['credentials'])
            previous={'kind':'definite_failure','operation_id':'operation-'+str(i),'error_code':'fb_graph_368'}
        result=self.bridge.begin_operation(self.job,self.claim,self.operation(10,previous))
        self.assertTrue(result['credentials_exhausted'])
        self.assertEqual(self.bridge.reconcile(self.job)['status'],'failed')
    def test_lost_operation_result_never_reissues_write_credential(self):
        self.bridge.begin_operation(self.job,self.claim,self.operation(1))
        with self.assertRaisesRegex(AdapterError,'operation_already_started'):
            self.bridge.begin_operation(self.job,self.claim,self.operation(1))
        with self.assertRaisesRegex(AdapterError,'previous_operation_unresolved'):
            self.bridge.begin_operation(self.job,self.claim,self.operation(2))
    def test_success_receipt_readback_and_duplicate_delivery(self):
        self.bridge.begin_operation(self.job,self.claim,self.operation(1))
        receipt={'kind':'success','post_id':'12345'}
        self.bridge.complete(self.job,receipt,self.claim)
        self.assertEqual(self.bridge.reconcile(self.job)['graph_post_id'],'12345')
        self.assertTrue(self.bridge.complete(self.job,receipt,self.claim)['idempotent'])
        self.assertEqual(len(self.store.attempts),1)


class CanonicalHoldTests(unittest.TestCase):
    def test_fb_page_order_skips_paused_item_but_material_dedupe_does_not(self):
        with tempfile.TemporaryDirectory() as directory:
            now=datetime(2026,11,2,tzinfo=timezone.utc)
            store=FBVideoStore(str(Path(directory)/'video.db'),now_fn=lambda:now)
            with store.connect() as c:
                c.execute("INSERT INTO fb_video_batch VALUES('batch','tenant','owner','Owner','op','hash','{}',?)",(now.isoformat(),))
                for ident,sequence in [('old',1),('new',2)]:
                    c.execute("""INSERT INTO fb_video_task(task_id,batch_id,tenant_key,owner_user_id,page_id,material_id,content_id,
                        sequence,page_sequence,planned_at_utc,planned_bj_date,processing_mode,status,snapshot_json,prepare_state,link_state,created_at_utc,updated_at_utc)
                        VALUES(?,'batch','tenant','owner','page',?,'content',?,?,'2026-11-01T00:00:00+00:00','2026-11-01','original','queued','{}','ready','ready',?,?)""",
                        (ident,ident,sequence,sequence,now.isoformat(),now.isoformat()))
                c.execute("BEGIN IMMEDIATE")
                pause_row(c,{'source_key':'fb:video:old','source_id':'old','channel':'fb','family':'video'},now.isoformat(),
                    table='fb_video_task',identity='task_id',state='status',allowed={'queued'},attempt_table='fb_video_attempt')
                c.commit()
                self.assertEqual(manual_block(c,'page','old'),'fb_video_material_active')
            claimed=store.claim('execute','pipeline-node',task_id='new')
            self.assertEqual(claimed['task_id'],'new')
            with store.connect() as c:
                self.assertEqual(c.execute("SELECT status FROM fb_video_task WHERE task_id='old'").fetchone()[0],'queued')


class NativeFBReceiptTests(unittest.TestCase):
    def test_late_exact_accepted_receipt_only_enters_get_reconciliation(self):
        with tempfile.TemporaryDirectory() as directory:
            now = datetime(2026, 11, 2, tzinfo=timezone.utc)
            store = FBAutoPostStore(str(Path(directory) / 'canonical.db'), now_fn=lambda: now)
            instant = now.isoformat()
            with store.connect() as c:
                c.execute("INSERT INTO fb_auto_template(id,name,status,created_at_utc,updated_at_utc) VALUES(1,'t','enabled',?,?)", (instant, instant))
                c.execute("INSERT INTO fb_auto_template_version(template_id,version,config_json,config_sha256,created_at_utc) VALUES(1,1,'{}',?,?)", ('a' * 64, instant))
                c.execute("INSERT INTO fb_auto_run(id,template_id,template_version,slot_key,trigger_type,status,config_json,created_at_utc) VALUES(1,1,1,'manual-slot','manual','queued','{}',?)", (instant,))
                c.execute("""INSERT INTO fb_auto_task(id,run_id,template_id,template_version,page_id,group_id,status,material_id,content_id,created_at_utc,
                    media_url,source_media_url,prepared_media_url,prepared_at_utc,planned_publish_at_utc)
                    VALUES(1,1,1,1,'page','group','ready','material','content',?,'https://cdn.example/output.mp4',
                    'https://cdn.example/source.mp4','https://cdn.example/output.mp4',?,?)""", (instant, instant, instant))
            runtime = SimpleNamespace(store=store, executor=SimpleNamespace(live_enabled=True),
                pages=SimpleNamespace(eligible_credentials=lambda _: [PageCredential('page', 'user', 'credential', 'private-test')]))
            bridge = FBNativeBridge(runtime)
            job = {'source_key': 'fb:auto:1', 'channel': 'fb', 'family': 'auto', 'source_id': '1', 'stage': 'publish', 'generation': 1}
            claim = {'claim_token': 'z' * 32, 'node_id': 'test-node'}
            descriptor = bridge.reserve(job, claim)
            operation = {'operation': 'fb_video_publish', 'operation_id': 'native-fb-operation', 'account_resource': 'fb:page:page',
                         'request': descriptor['request'], 'request_sha256': 'b' * 64}
            bridge.begin_operation(job, claim, operation)
            store.complete_task(1, {'status': 'unknown', 'error_code': 'connection_lost'})
            receipt = {'operation_id': operation['operation_id'], 'kind': 'success', 'post_id': '123456'}
            bridge.checkpoint(job, claim, 'fb_publish_attempt', receipt)
            bridge.checkpoint(job, claim, 'fb_publish_attempt', receipt)
            self.assertEqual(bridge.reconcile(job), {'status': 'submitted', 'graph_post_id': '123456', 'unknown_outcome': 0})
            with store.connect() as c:
                self.assertEqual(c.execute('SELECT COUNT(*) FROM fb_auto_publish_attempt').fetchone()[0], 1)
                self.assertEqual(c.execute('SELECT result_kind FROM fb_auto_publish_attempt').fetchone()[0], 'accepted')
            with self.assertRaisesRegex(AdapterError, 'operation_already_started'):
                bridge.begin_operation(job, claim, operation)


class YouTubeContinuationProjectionTests(unittest.TestCase):
    def setUp(self):
        self.spec = LedgerSpec('youtube', 'publish', '', 'drama_youtube_publish')
        self.row = {'id': 1, 'status': 'scheduled', 'schedule_status': 'armed', 'video_id': 'video-1',
                    'publish_at': '2026-11-03T00:00:00Z', 'unknown_outcome': 0, 'comment_status': 'pending',
                    'video_state': 'submitted', 'created_at_utc': '2026-11-02T00:00:00Z'}
        self.adapter = LedgerAdapter(self.spec)
        self.adapter.read = lambda _: dict(self.row)
        self.registry = AdapterRegistry([self.adapter])
        self.job = {'source_key': 'youtube:publish:1', 'source_id': '1', 'channel': 'youtube', 'family': 'publish', 'stage': 'publish'}
        self.continuation = {'video_id': 'video-1', 'scheduled_at': '2026-11-03T00:00:00Z'}

    def test_armed_schedule_is_not_published_and_continuation_identity_is_verified(self):
        result = self.registry._verified_result(self.adapter, self.job, {'continuation': self.continuation})
        self.assertTrue(result['dispatch_completed'])
        self.assertFalse(result['published'])
        self.assertEqual(result['continuation'], self.continuation)
        with self.assertRaisesRegex(AdapterError, 'canonical_continuation_unconfirmed'):
            self.registry._verified_result(self.adapter, self.job, {'continuation': dict(self.continuation, video_id='other')})

    def test_comment_unknown_preserves_real_video_metric_without_completing_job(self):
        self.row.update(status='unknown', video_state='published', unknown_outcome=1,
                        video_published_at_utc='2026-11-03T00:00:03Z', comment_status='unknown')
        result = self.registry._verified_result(self.adapter, self.job, {})
        self.assertTrue(result['canonical_receipt']['video_published'])
        self.assertEqual(result['canonical_receipt']['video_published_at'], '2026-11-03T00:00:03Z')
        self.assertFalse(result['dispatch_completed'])
        self.assertFalse(result['published'])

    def test_only_authorized_pending_control_can_wake_existing_continuation(self):
        self.row.update(status='schedule_pending', schedule_status='control_pending', publish_at='',
                        schedule_command='cancel', schedule_command_status='pending', schedule_operation_id='owner-control',
                        schedule_confirmed_publish_at='2026-11-03T00:00:00Z')
        self.assertTrue(self.adapter.project(self.row)['continuation_due'])
        self.assertEqual(self.registry._verified_continuation(self.adapter, self.row, self.continuation, allow_control=True), self.continuation)
        self.row['schedule_operation_id'] = ''
        self.assertFalse(self.adapter.project(self.row)['continuation_due'])
        with self.assertRaisesRegex(AdapterError, 'canonical_continuation_unconfirmed'):
            self.registry._verified_continuation(self.adapter, self.row, self.continuation, allow_control=True)

    def test_confirmed_schedule_cancellation_is_completed_without_publication_or_failure(self):
        self.row.update(status='cancelled', schedule_status='cancelled', schedule_command_status='confirmed')
        result = self.registry._verified_result(self.adapter, self.job, {})
        self.assertTrue(result['dispatch_completed'])
        self.assertTrue(result['canonical_receipt']['schedule_cancelled'])
        self.assertFalse(result['published'])
        self.assertFalse(result['definitive_failure'])
        self.assertFalse(result['metadata_only'])
        self.assertEqual(self.adapter.project(self.row)['status'], 'completed')

    def test_legacy_unreviewed_workflow_is_observation_only(self):
        self.adapter.driver = SimpleNamespace(control_only=False)
        self.row.update(status='queued', workflow='legacy', video_id='', video_attempt_count=0)
        projected = self.adapter.project(self.row)
        self.assertFalse(projected['execution_available'])
        self.assertTrue(projected['observation_only'])
        self.assertEqual(projected['hold_reason'], 'canonical_legacy_workflow')

    def test_future_visibility_is_not_old_backlog_but_upload_can_start_early(self):
        self.row.update(status='queued', video_id='', video_state='pending', scheduled_at='2026-11-02T00:00:00Z')
        offer = self.adapter.project(self.row)
        self.assertEqual(offer['scheduled_at'], '2026-11-02T00:00:00Z')
        self.assertEqual(offer['cutover_due_at'], '2026-11-03T00:00:00Z')
        self.adapter.scan = lambda *args: [offer]
        self.assertEqual(self.registry.apply_cutover('youtube', '2026-11-02T12:00:00Z')['paused_source_keys'], [])


if __name__=='__main__':unittest.main()
