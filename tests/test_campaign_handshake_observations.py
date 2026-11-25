from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch


TOOLS = Path(__file__).resolve().parents[1] / 'ops/campaign-copy-recovery'
sys.path.insert(0, str(TOOLS))
import cloud_sessions
import collect_preauth
import observe_worker


class ObservationTests(unittest.TestCase):
    def test_healthy_or_bound_leases_only_check_local_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            book = Path(tmp).resolve() / 'test.sqlite3'
            with closing(sqlite3.connect(book)) as db:
                db.execute('CREATE TABLE remote_lease(id,node,task,state,identity,reason,heartbeat)')
                for state, identity in [('closed', None), ('connected', '{}'), ('unknown', '{}')]:
                    db.execute('INSERT INTO remote_lease VALUES(?,?,?,?,?,?,?)',
                               ('abc', 'hk-read-assist', 'campaign-copy-report', state, identity, 'heartbeat_lost', 1000))
                db.commit()
            with patch.object(collect_preauth, 'observe_cluster', side_effect=AssertionError), \
                    patch.object(collect_preauth, 'guarded_endpoint', side_effect=AssertionError):
                self.assertEqual(collect_preauth.collect(Path('unused'), book), {'pending': []})

    def cloud(self, behavior):
        def call(action, args):
            if action == 'DescribeInstances':
                rows = [{'ClusterId': cloud_sessions.CLUSTER, 'InstanceId': role, 'InstanceRole': role,
                         'Status': 'running'} for role in ('master', 'ro')]
                return {'RequestId': 'inventory', 'TotalCount': 2, 'InstanceSet': rows}
            rows = []
            if args.get('ID') == 3 and args['InstanceId'] == 'ro':
                rows = [dict(ID=3, User='ads_aius', Host='43.166.187.96:3312', Command='Sleep')]
            if args.get('Command') == 'Connect':
                rows = [dict(ID=8, User='unauthenticated user', Command='Connect')]
            if args.get('ID') == 8 and behavior == 'still connecting':
                rows = [dict(ID=8, User='unauthenticated user', Command='Connect')]
            if 'Time' in args and behavior == 'truncated':
                rows = [dict(ID=8, User='user', Command='Sleep', Time=args['Time'])] * args['Limit']
            return {'RequestId': 'request', 'ProcessList': rows}
        return call

    def test_all_members_and_individual_login_readback(self):
        result = cloud_sessions.observe_cluster(1000, 3, 'ads_aius', self.cloud('gone'))
        self.assertEqual(result['member_ids'], ['master', 'ro'])
        self.assertTrue(all(n['resolved']['8']['gone'] for n in result['nodes']))
        for behavior in ('still connecting', 'truncated'):
            with self.subTest(behavior=behavior), self.assertRaises(ValueError):
                cloud_sessions.observe_cluster(1000, 3, 'ads_aius', self.cloud(behavior))

    def test_post_authentication_error_cannot_be_a_greeting_timeout(self):
        lease = 'a' * 32
        lines = ['in _do_handshake _socket.recv() header = self._recv_chunk',
                 'mysql.connector.errors.ConnectionTimeoutError', 'in _do_auth',
                 json.dumps(dict(event='offload_sql_close_unconfirmed', lease=lease,
                                 task='campaign-copy-report', locally_aborted=False))]
        journal = b'\n'.join(json.dumps(dict(MESSAGE=line, _SYSTEMD_INVOCATION_ID='a', _PID='4444')).encode()
                             for line in lines)
        with patch.object(observe_worker.subprocess, 'check_output', return_value=journal), self.assertRaises(ValueError):
            observe_worker.worker_proof(lease, 1000)
