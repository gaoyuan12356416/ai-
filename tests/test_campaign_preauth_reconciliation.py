import copy
from contextlib import closing
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / 'ops/campaign-copy-recovery/reconcile_preauth.py'
SPEC = importlib.util.spec_from_file_location('reconcile_preauth', SCRIPT)
recovery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(recovery)


class ReconcilePreauthTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.book = Path(folder.name).resolve() / 'ledger.sqlite3'
        self.lease = '1' * 32
        user = 'ads_aius'
        self.witness = {'id': 17, 'user': user, 'host': '43.166.187.96:45678'}
        identity = {'server_uuid': 'fixture-uuid', 'database_user': user, 'read_only': 1, 'transaction_read_only': 1}
        with closing(sqlite3.connect(self.book, isolation_level=None)) as db:
            db.execute('CREATE TABLE remote_lease(id TEXT PRIMARY KEY,node,task,created,heartbeat,endpoint,state,identity,reason)')
            common = ('hk-read-assist', 'campaign-copy-report', 998.0, 1000.0, '101.32.56.53:63350 via CPU tunnel')
            db.execute('INSERT INTO remote_lease VALUES(?,?,?,?,?,?,?,?,?)', ('previous', *common, 'closed', json.dumps(identity), 'server_eof'))
            db.execute('INSERT INTO remote_lease VALUES(?,?,?,?,?,?,?,?,?)', (self.lease, *common, 'unknown', None, 'heartbeat_lost'))
        nodes = []
        for role in ('master', 'ro'):
            result = {'receipt': 'r', 'rows': 0, 'limit': 20, 'wrong_time_filter': False, 'possible_old_preauth': [], 'positive': []}
            node = {name: copy.deepcopy(result) for name in ('connecting', 'preauth', 'old', 'witness')}
            node.update(instance=role, role=role, resolved={})
            if role == 'ro':
                node['witness'].update(rows=1, positive=[self.witness.copy()])
            nodes.append(node)
        self.proof = {
            'worker': {'lease': self.lease, 'node': common[0], 'task': common[1], 'observed_at': 6995, 'failure_at': 1004,
                       'preauth_before_first_header': True, 'worker_exited': True, 'open_data_sockets': 0, 'journal_hash': 'a'*64},
            'endpoint': {'observed_at': 6996, 'endpoint': '101.32.56.53:63350', 'server_uuid': identity['server_uuid'],
                         'readonly': True, 'transaction_readonly': True, 'gate_used': True, 'witness_id': 17, 'witness_user': user,
                         'witness_host': self.witness['host'], 'remote_ssh_data_sockets': 0, 'unattributed_sockets': 0},
            'cloud': {'started_at': 6980, 'collected': 6998, 'incident': 1000, 'cut': 5300, 'region': 'ap-hongkong',
                      'cluster': 'cynosdbmysql-5kxxsre7', 'inventory_receipt': 'r', 'member_ids': ['master','ro'], 'nodes': nodes}}

    def test_dry_run_and_single_idempotent_close(self):
        self.assertEqual(recovery.reconcile(self.book, self.proof, 7000)['status'], 'verified')
        self.assertEqual(recovery.reconcile(self.book, self.proof, 7000, True)['status'], 'closed')
        self.assertEqual(recovery.reconcile(self.book, self.proof, 9000, True)['status'], 'already_closed')
        with closing(sqlite3.connect(self.book)) as db:
            self.assertEqual(db.execute("SELECT reason FROM remote_lease WHERE id='previous'").fetchone()[0], 'server_eof')

    def test_incomplete_or_wrong_evidence_never_changes_lease(self):
        for modify in (
            lambda p: p['worker'].update(worker_exited=False), lambda p: p['worker'].update(observed_at=3000),
            lambda p: p['endpoint'].update(server_uuid='other'), lambda p: p['endpoint'].update(unattributed_sockets=1),
            lambda p: p['cloud']['member_ids'].append('unseen'), lambda p: p['cloud']['nodes'][0]['old'].update(rows=20),
            lambda p: p['cloud']['nodes'][1]['witness']['positive'][0].update(host='43.166.187.96:9999'),
            lambda p: p['cloud']['nodes'][0]['connecting'].update(rows=1, possible_old_preauth=[{'thread': 42}]),
        ):
            with self.subTest(modify=modify):
                proof = copy.deepcopy(self.proof)
                modify(proof)
                with self.assertRaises(ValueError):
                    recovery.reconcile(self.book, proof, 7000, True)
        self.assertEqual(recovery.reconcile(self.book, self.proof, 7000)['status'], 'verified')

    def test_transient_candidate_requires_individual_readback(self):
        node = self.proof['cloud']['nodes'][0]
        node['connecting'].update(rows=1, possible_old_preauth=[{'thread': 42}])
        node['resolved']['42'] = {'gone': False, 'authenticated': False, 'request': 'r2'}
        with self.assertRaises(ValueError):
            recovery.reconcile(self.book, self.proof, 7000, True)
        node['resolved']['42']['gone'] = True
        self.assertEqual(recovery.reconcile(self.book, self.proof, 7000, True)['status'], 'closed')
