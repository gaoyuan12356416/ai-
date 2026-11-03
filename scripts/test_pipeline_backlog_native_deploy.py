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


if __name__=='__main__':unittest.main()
