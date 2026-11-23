"""Linux socket tests use no database, system unit or production gate path."""
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from features.gpu_offload import legacy_sql_gate as legacy
from features.gpu_offload.shared_sql_gate import SharedCoordinator, legacy_occupancy
from features.gpu_offload import sql_handover


@unittest.skipUnless(os.name=='posix' and hasattr(socket,'AF_UNIX'),'Linux production socket protocol')
class SharedGateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.servers=[];self.clients=[]
        self.coordinator=SharedCoordinator(root=self.root/'shared',state_path=self.root/'shared-state.json',legacy_reserved=0)

    def tearDown(self):
        for client in self.clients:client.close()
        deadline=time.monotonic()+3
        while any(s.coordinator.active for s in self.servers) and time.monotonic()<deadline:time.sleep(0.01)
        for server in self.servers:server.shutdown();server.server_close()
        self.coordinator.book.close();self.temp.cleanup()

    def server(self,path,coordinator):
        server=legacy.ThreadingUnixGateServer(str(path),legacy.GateRequestHandler)
        server.allowed_uids={os.getuid()};server.coordinator=coordinator
        threading.Thread(target=server.serve_forever,daemon=True).start();self.servers.append(server)
        return server

    def acquire(self,path,job='fixture'):
        client,result=legacy.acquire_permit(str(path),legacy.DEFAULT_POOL,job,2)
        self.clients.append(client);self.assertEqual(result['status'],'granted');return client

    def test_legacy_connection_survives_atomic_listener_handover(self):
        canonical=self.root/'gate.sock';alias=self.root/'legacy.sock';staged=self.root/'staged.sock'
        old=legacy.GateCoordinator(14,100,300,legacy.DEFAULT_POOL,str(self.root/'old-state.json'),active_timeout=1500)
        self.server(canonical,old);inflight=self.acquire(canonical)
        self.server(staged,self.coordinator);self.coordinator.set_legacy_reserved(14)
        os.link(str(canonical),str(alias));os.replace(str(staged),str(canonical))
        # The status reply is an accept barrier for connections already queued
        # on the old listener. Its open in-flight lease must remain intact.
        self.assertEqual(legacy.status_request(str(alias))['in_use'],1)
        marker=dict(pid=os.getpid(),starttime=legacy.read_proc_identity(os.getpid())['starttime'],bound_path=str(self.root/'gate.sock'),state_path=str(self.root/'old-state.json'),barrier_complete=True)
        held=legacy_occupancy(marker);self.assertGreaterEqual(held,1)
        self.coordinator.set_legacy_reserved(held)
        self.acquire(canonical,'new-request')
        self.assertEqual(old.snapshot()['in_use'],1)
        inflight.settimeout(0.05)
        with self.assertRaises(socket.timeout):inflight.recv(1)
        legacy.send_json_line(inflight,{'op':'release','reason':'fixture_complete'})
        inflight.settimeout(2);self.assertEqual(inflight.recv(1),b'')
        self.assertEqual(old.snapshot()['in_use'],0)
        self.coordinator.set_legacy_reserved(0)
        self.assertEqual(self.coordinator.snapshot()['local_in_use'],1)

    def test_remote_occupancy_reduces_same_local_budget(self):
        path=self.root/'gate.sock';self.server(path,self.coordinator)
        for i in range(13):self.acquire(path,'local-'+str(i))
        lease=self.coordinator.reserve_remote('fixture')
        self.coordinator.book.begin(lease['id'],'hk-read-assist')
        self.assertEqual(self.coordinator.snapshot()['in_use'],14)
        self.assertEqual(self.coordinator.snapshot()['available'],0)
        self.assertEqual(self.coordinator.capacity,13)

    def test_draining_does_not_close_active_control_socket(self):
        path=self.root/'gate.sock';self.server(path,self.coordinator);client=self.acquire(path)
        self.coordinator.drain();client.settimeout(0.05)
        with self.assertRaises(socket.timeout):client.recv(1)
        self.assertEqual(self.coordinator.snapshot()['local_in_use'],1)
        legacy.send_json_line(client,{'op':'release','reason':'fixture_complete'})
        client.settimeout(2);self.assertEqual(client.recv(1),b'')

    def test_recovered_local_occupancy_is_preserved_without_killing_processes(self):
        ledger=self.root/'shared'/'local-sessions.json'
        data=json.loads(ledger.read_text());data['sessions']={'unknown':{'wrapper_pid':os.getpid(),'node':'cpu-main'}}
        legacy.durable_replace_json(str(ledger),data)
        self.coordinator.book.close()
        self.coordinator=SharedCoordinator(root=self.root/'shared',state_path=self.root/'shared-state.json',legacy_reserved=0)
        self.assertEqual(self.coordinator.capacity,13)
        self.assertEqual(self.coordinator.snapshot()['recovered_local_reserved'],1)
        self.assertTrue(legacy.process_exists(os.getpid()))

    def test_handover_command_preserves_old_lease_and_refuses_busy_retirement(self):
        canonical=self.root/'gate.sock';staged=self.root/'staged.sock';old_state=self.root/'old-state.json'
        old=legacy.GateCoordinator(14,100,300,legacy.DEFAULT_POOL,str(old_state),active_timeout=1500)
        # The live daemon has accumulated more per-job metrics than its own
        # control-message limit permits. That must not block the accept barrier.
        for number in range(50):old._job_metrics('old-metric-'+str(number))['granted']=1
        old.write_state()
        self.server(canonical,old);inflight=self.acquire(canonical)
        self.coordinator.set_legacy_reserved(14);self.server(staged,self.coordinator)
        unit=self.root/'units'/'old.service';unit.parent.mkdir();unit.write_text('[Service]\nExecStart=old-gate\n')
        state=self.root/'handover';state.mkdir()
        fake=lambda *args:str(os.getpid()) if args[:1]==('show',) else ''
        with patch.multiple(sql_handover,CANONICAL=canonical,STAGED=staged,ALIAS=self.root/'legacy.sock',
                            UNIT_PATH=unit,DROPIN=self.root/'units'/'old.service.d'/'90-offload.conf',LEGACY_STATE=old_state):
            with patch.object(sql_handover,'systemctl',side_effect=fake) as ctl:
                result=sql_handover.activate(state)
                self.assertEqual(result['stage'],'legacy_draining')
                self.assertEqual(legacy.status_request(str(canonical))['version'],'2.0.0-offload')
                self.assertEqual(old.snapshot()['in_use'],1)
                retired=sql_handover.retire(state)
                self.assertEqual(retired['stage'],'legacy_draining')
                self.assertFalse(any(call.args[:1]==('stop',) for call in ctl.call_args_list))
                self.assertTrue(unit.is_file());self.assertFalse(unit.is_symlink())
                inflight.settimeout(0.05)
                with self.assertRaises(socket.timeout):inflight.recv(1)

    def test_large_metrics_do_not_break_compatible_status(self):
        path=self.root/'gate.sock';self.server(path,self.coordinator)
        for number in range(50):self.coordinator._job_metrics('metric-'+str(number))['granted']=1
        self.coordinator.write_state()
        snapshot=legacy.status_request(str(path))
        self.assertEqual(snapshot['capacity'],14)
        self.assertIn('details_state_path',snapshot)
        self.assertEqual(len(json.loads(Path(snapshot['details_state_path']).read_text())['job_metrics']),50)


if __name__=='__main__':unittest.main()
