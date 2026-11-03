"""Safety gates for sidecar restart while remote preparation continues."""
import datetime as dt
import contextlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from scripts import deploy_pipeline_backlog_native as deploy


class NativeDeploymentTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name)
        self.central=self.root/'dispatch.sqlite3';self.native=self.root/'tt.db'
        now=dt.datetime.now(dt.timezone.utc)
        with contextlib.closing(sqlite3.connect(self.central)) as c, c:
            c.executescript('''CREATE TABLE pipeline_config(id INTEGER,config_json TEXT);
            CREATE TABLE pipeline_job(id INTEGER,channel TEXT,family TEXT,source_key TEXT,source_id TEXT,stage TEXT,status TEXT,
             external_intent INTEGER,generation INTEGER,claim_hash TEXT,node_id TEXT,worker_id TEXT,lease_expires_at TEXT);
            CREATE TABLE pipeline_worker(node_id TEXT,worker_id TEXT,last_seen_at TEXT);''')
            c.execute('INSERT INTO pipeline_config VALUES(1,?)',(json.dumps({'enabled_channels':['fb','youtube']}),))
            c.execute('INSERT INTO pipeline_job VALUES(1,?,?,?,?,?,?,?,?,?,?,?,?)',('tt','auto','tt:auto:7','7','prepare','running',0,2,'frozenhash','cpu-main','prepare-1',(now+dt.timedelta(seconds=110)).isoformat()))
            c.execute('INSERT INTO pipeline_worker VALUES(?,?,?)',('cpu-main','prepare-1',now.isoformat()))
        with contextlib.closing(sqlite3.connect(self.native)) as c, c:
            c.executescript('''CREATE TABLE tt_auto_task(id INTEGER,status TEXT,claim_phase TEXT,unknown_outcome INTEGER,publish_id TEXT,publish_attempt_count INTEGER,claim_token TEXT);
            CREATE TABLE pipeline_tt_claim(source_key TEXT,stage TEXT,generation INTEGER,claim_hash TEXT,native_token TEXT,receipt_json TEXT);
            CREATE TABLE pipeline_tt_operation(source_key TEXT);''')
            c.execute("INSERT INTO tt_auto_task VALUES(7,'preparing','prepare',0,'',0,'native-token')")
            c.execute("INSERT INTO pipeline_tt_claim VALUES('tt:auto:7','prepare',2,'frozenhash','native-token','')")
        self.addCleanup(patch.stopall)
        patch.object(deploy,'DATA',self.root).start()
        patch.dict(deploy.SPECS,{'tt_auto':{'ledgers':[str(self.native)]}}).start()

    def mutate(self,path,sql):
        with contextlib.closing(sqlite3.connect(path)) as c, c:c.execute(sql)

    def test_exact_running_preparation_can_continue_without_any_row_changes(self):
        before=[p.read_bytes() for p in (self.central,self.native)]
        self.assertEqual(deploy.admission('tt_auto'),[{'id':1,'source_key':'tt:auto:7','generation':2,'node_id':'cpu-main'}])
        self.assertEqual(before,[p.read_bytes() for p in (self.central,self.native)])

    def test_new_claims_running_publish_and_stale_heartbeats_are_rejected(self):
        for sql,code in [("UPDATE pipeline_config SET config_json='{\"enabled_channels\":[\"tt\"]}'",'channel_claims_still_enabled'),
                         ("UPDATE pipeline_job SET stage='publish'",'tt_publish_or_other_work_still_running'),
                         ("UPDATE pipeline_worker SET last_seen_at='2000-01-01T00:00:00Z'",'tt_node_heartbeat_stale'),
                         ("UPDATE pipeline_job SET lease_expires_at='2000-01-01T00:00:00Z'",'tt_central_lease_expired')]:
            with self.subTest(code=code):
                original=self.central.read_bytes();self.mutate(self.central,sql)
                with self.assertRaisesRegex(RuntimeError,code):deploy.admission('tt_auto')
                self.central.write_bytes(original)

    def test_mismatched_native_identity_and_any_publish_operation_are_rejected(self):
        for sql,code in [("UPDATE pipeline_tt_claim SET native_token='different'",'tt_native_binding_mismatch'),
                         ("UPDATE tt_auto_task SET unknown_outcome=1",'tt_native_execution_not_prepare'),
                         ("INSERT INTO pipeline_tt_operation VALUES('tt:auto:7')",'tt_native_publish_operation_present'),
                         ("UPDATE tt_auto_task SET status='ready'",'tt_native_not_exact_node_owned')]:
            with self.subTest(code=code):
                original=self.native.read_bytes();self.mutate(self.native,sql)
                with self.assertRaisesRegex(RuntimeError,code):deploy.admission('tt_auto')
                self.native.write_bytes(original)


class DeploymentRetryTests(unittest.TestCase):
    def test_failed_oneshot_with_zero_pid_is_reset_only_after_requests_finish(self):
        state={'LoadState':'loaded','MainPID':'0','ActiveState':'failed'};commands=[]
        def invoke(args):
            commands.append(args)
            if args[1]=='reset-failed':state['ActiveState']='inactive'
            return b''
        with patch.object(deploy,'show',side_effect=lambda u,k:state[k]),patch.object(deploy,'accepted',return_value=b''),patch.object(deploy,'run',side_effect=invoke):
            result=deploy.stop_metadata_caller('x-auto-post-runner.service',[18833,8810])
        self.assertTrue(result['reset_failed'])
        self.assertEqual(commands,[['systemctl','stop','x-auto-post-runner.service'],['systemctl','reset-failed','x-auto-post-runner.service']])

    def test_live_process_or_accepted_request_never_resets_failed(self):
        for pid,accepted in [('12',b''),('0',b'accepted')]:
            with self.subTest(pid=pid,accepted=accepted),patch.object(deploy,'show',side_effect=lambda u,k:{'LoadState':'loaded','MainPID':pid,'ActiveState':'failed'}[k]),patch.object(deploy,'accepted',return_value=accepted),patch.object(deploy,'run') as invoke:
                with self.assertRaises(RuntimeError):deploy.stop_metadata_caller('x-auto-post-runner.service',[18833])
                self.assertFalse(any(c.args[0][1]=='reset-failed' for c in invoke.call_args_list))

    def test_attempt_two_preserves_original_audit_and_rejects_unsafe_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp);old=folder/'old';old.mkdir();first=folder/'apply-x.json'
            audit={'lane':'x','commit':'a'*40,'rules_removed_at':'2026-11-03T04:13:45Z','rule_cleanup_failed':[],'rules':[]}
            first.write_text(json.dumps(audit));original=first.read_bytes()
            manifest={'commit':'a'*40};entry={'previous_root':str(old)}
            with patch.dict(deploy.SPECS,{'x':{'root':str(old),'ports':[8810,18833]}}):
                path,previous=deploy.new_audit_path(folder,'x',manifest,entry)
                self.assertEqual(path.name,'apply-x.attempt-2.json');self.assertEqual(first.read_bytes(),original)
                self.assertEqual(previous['sha256'],deploy.digest(first))
                for updates in [{'swapped':True},{'rules_removed_at':''},{'rule_cleanup_failed':['failed']}]:
                    first.write_text(json.dumps({**audit,**updates}))
                    with self.assertRaises(RuntimeError):deploy.new_audit_path(folder,'x',manifest,entry)
                first.write_bytes(original)
                with self.assertRaisesRegex(RuntimeError,'retry_runtime_already_switched'):deploy.new_audit_path(folder,'x',manifest,{'previous_root':str(folder/'different')})

    def test_listener_startup_delay_is_polled_without_restart(self):
        with patch.object(deploy,'run',side_effect=[b'',b'',b'LISTEN 127.0.0.1:18831']) as invoke,patch.object(deploy.time,'sleep'):
            deploy.wait_listener(18831)
        self.assertEqual(invoke.call_count,3)
        self.assertTrue(all(c.args[0][0]=='ss' for c in invoke.call_args_list))

    def test_prior_firewall_rule_must_be_proved_absent(self):
        from types import SimpleNamespace
        rule=['OUTPUT','-d','127.0.0.1','-p','tcp','--dport','8810','-m','conntrack','--ctstate','NEW','-m','comment','--comment','backlog-x-aaaaaaaaaa','-j','REJECT','--reject-with','tcp-reset']
        audit={'commit':'a'*40,'rules':[rule]}
        with patch.object(deploy.subprocess,'run',return_value=SimpleNamespace(returncode=1)):
            deploy.exact_rules_absent(audit,'x')
        for code in [0,2]:
            with self.subTest(code=code),patch.object(deploy.subprocess,'run',return_value=SimpleNamespace(returncode=code)):
                with self.assertRaisesRegex(RuntimeError,'previous_admission_rule_present_or_unverifiable'):deploy.exact_rules_absent(audit,'x')


if __name__=='__main__':unittest.main()
