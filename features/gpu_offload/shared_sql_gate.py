"""Compatible local admission plus persistent remote occupancy, one total of 14.

The legacy CPU wire protocol and child protections are preserved. Replacement
does not call the legacy daemon's destructive stop/reconciliation routines.
"""
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import socket
import threading
import time

from . import legacy_sql_gate as legacy
from .sql_leases import LeaseBook, LeaseError
from .sql_reconciliation import GateReconciliation


class SharedCoordinator(legacy.GateCoordinator):
    def __init__(self, *, root, state_path, legacy_reserved=14, active_timeout=1500):
        root = Path(root); root.mkdir(parents=True, exist_ok=True)
        self.book, self.legacy_reserved, self.recovered_local = None, legacy_reserved, {}
        ledger = root/'local-sessions.json'
        if ledger.exists():
            prior = json.loads(ledger.read_text())
            self.recovered_local = prior.get('sessions', {})
        super().__init__(14, 100, 300, legacy.DEFAULT_POOL, str(state_path), active_timeout=active_timeout,
                         ledger_path=str(ledger), boot_id=legacy.read_boot_id())
        self.book = LeaseBook(root/'remote-leases.sqlite3', lock=self.condition)
        self.write_state()

    @property
    def capacity(self):
        remote = self.book.occupied if self.book else 0
        return max(0, self.total_capacity-self.legacy_reserved-len(self.recovered_local)-remote)

    @capacity.setter
    def capacity(self, value):
        if int(value) != 14: raise ValueError('shared_query_budget_must_remain_fourteen')
        self.total_capacity = 14

    def _snapshot_unlocked(self):
        result = super()._snapshot_unlocked()
        remote = self.book.occupied if self.book else 0
        result.update(version='2.0.0-offload', capacity=14, local_in_use=len(self.active),
                      remote_in_use=remote, legacy_reserved=self.legacy_reserved,
                      recovered_local_reserved=len(self.recovered_local),
                      in_use=len(self.active)+remote+self.legacy_reserved+len(self.recovered_local),
                      available=max(0,self.capacity-len(self.active)))
        return result

    def snapshot(self):
        result=super().snapshot()
        if len(json.dumps(result,separators=(',',':')).encode())>3600:
            # The compatible wire protocol is limited to 4096 bytes. Full job
            # metrics remain in the atomic state file; admission never depends
            # on their fitting into a control reply.
            result={key:value for key,value in result.items() if key not in ('job_metrics','active_jobs','queued_jobs','counters')}
            result['details_state_path']=self.state_path
        return result

    def _ledger_projection_unlocked(self, exclude_ticket_id=None):
        sessions = dict(self.recovered_local)
        for key, ticket in self.active.items():
            if key == exclude_ticket_id: continue
            sessions[key] = dict(ticket_id=key, node='cpu-main', job=ticket.job,
                                 wrapper_pid=ticket.peer_pid, wrapper_starttime_ticks=ticket.peer_starttime,
                                 uid=ticket.peer_uid, sid=ticket.child_session, leader_pid=ticket.child_pid,
                                 leader_starttime_ticks=ticket.child_starttime,
                                 registered_at=ticket.granted_at)
        return sessions

    def set_legacy_reserved(self, value):
        with self.condition:
            value = min(14,max(0,int(value)))
            if value != self.legacy_reserved:
                self.legacy_reserved = value; self.write_state(); self.condition.notify_all()

    def reserve_remote(self, task):
        with self.condition:
            if self.stopping: raise LeaseError('gate_draining')
            if self.legacy_reserved: raise LeaseError('legacy_draining',retry_after=5)
            # Older local FIFO waiters retain priority. Remote retries back off.
            value = self.book.reserve('hk-read-assist',task,
                available=lambda:0 if self.queue else self.capacity-len(self.active))
            self.write_state(); self.condition.notify_all()
            return value

    def set_child(self,ticket,child_pid,child_pgid):
        with self.condition:
            was_stopping=self.stopping
            # A granted operation remains entitled to start/finish its child
            # while admission drains. The shared lock excludes new grants.
            if ticket.ticket_id in self.active:self.stopping=False
            try:return super().set_child(ticket,child_pid,child_pgid)
            finally:self.stopping=was_stopping

    def drain(self):
        # Mark admission closed; never close active control connections or kill
        # a query simply because the daemon is being upgraded or stopped.
        with self.condition:
            self.stopping = True; self.write_state(); self.condition.notify_all()


def legacy_occupancy(marker):
    """Conservative bound after the old listener's FIFO accept barrier passed."""
    if not marker.get('barrier_complete'): return 14
    if marker.get('retired'): return 0
    identity = legacy.read_proc_identity(marker['pid'])
    if identity is None or identity['starttime'] != marker['starttime']: return 14
    try:
        # Only AF_UNIX endpoints bound to this gate's path are counted. NSS and
        # journald also own sockets in the daemon; they are not SQL clients.
        unix = {}
        for line in Path('/proc/net/unix').read_text().splitlines()[1:]:
            cells = line.split()
            if len(cells)>=8 and cells[7] == marker['bound_path']:
                unix[cells[6]] = cells[3] != '00010000'
        clients = 0
        for fd in Path('/proc/{}/fd'.format(marker['pid'])).iterdir():
            try: link = str(fd.readlink())
            except FileNotFoundError: continue
            if link.startswith('socket:[') and unix.get(link[8:-1],False): clients += 1
        state = json.loads(Path(marker['state_path']).read_text())
        return min(14,max(clients,int(state['in_use'])+int(state['queued'])))
    except (OSError, ValueError, KeyError): return 14


class RemoteAPI(ThreadingHTTPServer):
    daemon_threads = False
    def __init__(self,address,coordinator,config_path,token):
        super().__init__(address,RemoteHandler)
        self.coordinator,self.config_path,self.token = coordinator,Path(config_path),token


class RemoteHandler(BaseHTTPRequestHandler):
    def log_message(self,*_): pass
    def setup(self):
        super().setup(); self.connection.settimeout(6)

    def reply(self,status,payload):
        data = json.dumps(payload,separators=(',',':')).encode()
        self.send_response(status); self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(data))); self.send_header('Connection','close')
        self.end_headers(); self.wfile.write(data); self.close_connection = True

    def do_POST(self):
        coordinator = self.server.coordinator
        try:
            expected='Bearer '+self.server.token
            if not hmac.compare_digest(self.headers.get('Authorization',''),expected):
                return self.reply(401,{'error':'unauthorized'})
            length = int(self.headers.get('Content-Length','0'))
            if length<2 or length>8192: return self.reply(400,{'error':'invalid_body'})
            value = json.loads(self.rfile.read(length))
            if not isinstance(value,dict): raise LeaseError('invalid_body')
            key,node = value.get('lease_id'), 'hk-read-assist'
            config = json.loads(self.server.config_path.read_text())
            if self.path=='/v1/lease/acquire':
                if not config.get('gpu_queries_enabled',False): raise LeaseError('gpu_queries_disabled',retry_after=30)
                limit=int(config.get('gpu_query_slots',1))
                if limit not in (1,2):raise LeaseError('invalid_gpu_limit')
                coordinator.book.node_limit=limit
                result = coordinator.reserve_remote(value.get('task'))
            elif self.path=='/v1/lease/begin':
                if not config.get('gpu_queries_enabled',False): raise LeaseError('gpu_queries_disabled',retry_after=30)
                result=coordinator.book.begin(key,node)
            elif self.path=='/v1/lease/bind':result=coordinator.book.bind(key,node,value.get('identity'))
            elif self.path=='/v1/lease/heartbeat':result=coordinator.book.heartbeat(key,node)
            elif self.path=='/v1/lease/query':
                if not config.get('gpu_queries_enabled',False): raise LeaseError('gpu_queries_disabled',retry_after=30)
                result=coordinator.book.start_query(key,node,setup=value.get('setup') is True)
            elif self.path=='/v1/lease/release':
                result=coordinator.book.release(key,node,server_eof=value.get('server_eof'),identity=value.get('identity'))
            elif self.path=='/v1/status':result=coordinator.snapshot()
            else:return self.reply(404,{'error':'not_found'})
            coordinator.write_state()
            with coordinator.condition:coordinator.condition.notify_all()
            self.reply(200,result)
        except LeaseError as error:
            self.reply(429 if error.retry_after else 409,{'error':error.code,'retry_after':error.retry_after})
        except (BrokenPipeError,ConnectionResetError):pass
        except Exception:self.reply(503,{'error':'gate_unavailable'})


def main():
    root = Path(os.environ['SHARED_SQL_ROOT'])
    run = Path(os.environ.get('SHARED_SQL_RUN','/run/sql-shared-gate'));run.mkdir(parents=True,exist_ok=True)
    config_path=Path(os.environ['SHARED_SQL_CONFIG'])
    marker_path=root/'handover.json'
    lock=legacy.acquire_daemon_lock(str(run/'daemon.lock'))
    legacy.prepare_session_cleanup_locks()
    coordinator=SharedCoordinator(root=root,state_path=run/'state.json')
    reconciliation=GateReconciliation(coordinator.book)
    socket_path=run/'staged.sock'
    if socket_path.exists():socket_path.unlink()
    server=legacy.ThreadingUnixGateServer(str(socket_path),legacy.GateRequestHandler)
    server.coordinator,server.allowed_uids=coordinator,{0}
    os.chmod(socket_path,0o660)
    if marker_path.exists():
        marker=json.loads(marker_path.read_text())
        canonical=Path(marker['canonical'])
        if marker.get('barrier_complete'):
            if canonical.exists() and canonical.stat().st_ino!=marker['canonical_inode']:
                raise RuntimeError('canonical_gate_changed_since_handover')
            canonical.parent.mkdir(parents=True,exist_ok=True)
            os.replace(str(socket_path),str(canonical))
            marker.update(canonical_inode=canonical.stat().st_ino,shared_pid=os.getpid())
            legacy.durable_replace_json(str(marker_path),marker)
    remote=RemoteAPI(('127.0.0.1',int(os.environ.get('SHARED_SQL_PORT','19966'))),coordinator,config_path,os.environ['SHARED_SQL_TOKEN'])
    remote_thread=threading.Thread(target=remote.serve_forever,daemon=True);remote_thread.start()
    stopping=threading.Event()
    def watch():
        last_expire=0
        while not stopping.wait(0.25):
            try:
                if marker_path.exists():coordinator.set_legacy_reserved(legacy_occupancy(json.loads(marker_path.read_text())))
                if time.monotonic()-last_expire>=5:
                    coordinator.book.expire();coordinator.write_state();last_expire=time.monotonic()
                    try:reconciliation.tick()
                    except Exception:
                        # A failed auxiliary readback retains the specific
                        # unknown lease; it does not freeze unrelated CPU work.
                        print(json.dumps({'event':'remote_lease_reconciliation_unavailable'}),flush=True)
                    with coordinator.condition:coordinator.condition.notify_all()
            except Exception:
                coordinator.set_legacy_reserved(14)
    watcher=threading.Thread(target=watch,daemon=True);watcher.start()
    def stop(*_):
        def drain():
            coordinator.drain()
            while True:
                with coordinator.condition:
                    if not coordinator.active and not coordinator.queue and not coordinator.book.occupied:break
                time.sleep(0.25)
            server.shutdown()
        threading.Thread(target=drain,daemon=True).start()
    for signum in (signal.SIGTERM,signal.SIGINT):signal.signal(signum,stop)
    print(json.dumps({'event':'shared_gate_staged','pid':os.getpid(),'capacity':14,'remote_enabled':False}),flush=True)
    try:server.serve_forever(poll_interval=0.2)
    finally:
        stopping.set();watcher.join(timeout=2);remote.shutdown();remote.server_close()
        server.server_close();coordinator.book.close();lock.close()


if __name__=='__main__':main()
