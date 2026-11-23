"""Durable remote SQL occupancy; disconnects never free an issued connection."""
from contextlib import contextmanager
import json
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid


class LeaseError(RuntimeError):
    def __init__(self, code, *, retry_after=0):
        self.code, self.retry_after = code, retry_after
        super().__init__(code)


class LeaseBook:
    """The caller shares ``lock`` with local admission, making the budget atomic.

    A reservation alone cannot open MySQL. ``begin`` durably records that the
    client may connect before acknowledging it, and may be called only once.
    After that point only observed server EOF or same-server session absence
    releases occupancy. Expiration and daemon recovery retain unknown sessions.
    """
    def __init__(self, path, *, lock=None, clock=time.time, node_limit=1, heartbeat_seconds=90, recover=True):
        if node_limit not in (1, 2): raise ValueError('node_limit_must_be_one_or_two')
        self.lock, self.clock = lock or threading.RLock(), clock
        self.node_limit, self.heartbeat_seconds, self.fault = node_limit, heartbeat_seconds, False
        path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS remote_lease(
            id TEXT PRIMARY KEY, node TEXT NOT NULL, task TEXT NOT NULL,
            state TEXT NOT NULL, endpoint TEXT NOT NULL, created REAL NOT NULL,
            heartbeat REAL NOT NULL, identity TEXT, reason TEXT NOT NULL DEFAULT '');
          CREATE INDEX IF NOT EXISTS remote_lease_state ON remote_lease(state);
          CREATE TABLE IF NOT EXISTS query_rate(node TEXT PRIMARY KEY, next_start REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS session_visibility(lease_id TEXT PRIMARY KEY, observed REAL NOT NULL);
        ''')
        if recover:
          with self.lock, self.transaction():
            # Reserved rows have never received permission to open TCP. Retire
            # them so a delayed begin cannot open an unaccounted connection.
            self.db.execute("UPDATE remote_lease SET state='cancelled',reason='restart_before_connect' WHERE state='reserved'")
            self.db.execute("UPDATE remote_lease SET state='unknown',reason='broker_restarted' WHERE state IN ('connecting','connected')")

    @contextmanager
    def transaction(self):
        if self.fault: raise LeaseError('lease_storage_unavailable')
        try:
            self.db.execute('BEGIN IMMEDIATE')
            yield
            self.db.execute('COMMIT')
        except Exception:
            if self.db.in_transaction: self.db.execute('ROLLBACK')
            self.fault = True
            raise

    def close(self):
        with self.lock: self.db.close()

    def rows(self):
        with self.lock:
            return [dict(row) for row in self.db.execute("SELECT * FROM remote_lease WHERE state NOT IN ('closed','cancelled') ORDER BY created")]

    @property
    def occupied(self):
        with self.lock:
            if self.fault: return 14
            return self.db.execute("SELECT COUNT(*) FROM remote_lease WHERE state NOT IN ('closed','cancelled')").fetchone()[0]

    def get(self, lease_id, node=None):
        with self.lock:
            row = self.db.execute('SELECT * FROM remote_lease WHERE id=?', (lease_id,)).fetchone()
            if row is None or (node is not None and row['node'] != node): raise LeaseError('lease_not_found')
            value = dict(row)
            value['identity'] = json.loads(value['identity']) if value['identity'] else None
            return value

    def reserve(self, node, task, *, available):
        if node != 'hk-read-assist': raise LeaseError('unknown_node')
        if not isinstance(task, str) or not re.fullmatch(r'[a-zA-Z0-9_.:/-]{1,96}', task): raise LeaseError('invalid_task')
        with self.lock:
            if self.fault: raise LeaseError('lease_storage_unavailable')
            own = self.db.execute("SELECT COUNT(*) FROM remote_lease WHERE node=? AND state NOT IN ('closed','cancelled')", (node,)).fetchone()[0]
            if own >= self.node_limit: raise LeaseError('node_capacity', retry_after=5)
            # ``available`` includes active/queued local and legacy occupancy;
            # it is evaluated under this same lock, never a remote snapshot.
            if available() <= 0: raise LeaseError('global_capacity', retry_after=1)
            key, now = uuid.uuid4().hex, self.clock()
            with self.transaction():
                self.db.execute('INSERT INTO remote_lease(id,node,task,state,endpoint,created,heartbeat) VALUES(?,?,?,?,?,?,?)',
                                (key, node, task, 'reserved', '101.32.56.53:63350 via CPU tunnel', now, now))
            return self.get(key, node)

    def begin(self, key, node):
        with self.lock:
            row = self.get(key, node)
            if row['state'] != 'reserved': raise LeaseError('connect_permission_already_issued')
            with self.transaction():
                self.db.execute("UPDATE remote_lease SET state='connecting',heartbeat=? WHERE id=?", (self.clock(), key))
            return self.get(key, node)

    @staticmethod
    def identity(value):
        if not isinstance(value, dict): raise LeaseError('database_identity_required')
        try:
            server = str(uuid.UUID(value['server_uuid']))
            connection = int(value['connection_id'])
            reader, transaction = int(value['read_only']), int(value['transaction_read_only'])
        except (ValueError, TypeError, KeyError): raise LeaseError('database_identity_required')
        if connection <= 0 or reader != 1 or transaction != 1: raise LeaseError('read_only_session_required')
        result=dict(server_uuid=server, connection_id=connection, read_only=reader, transaction_read_only=transaction)
        if value.get('database_user'):
            username=str(value['database_user'])
            if not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}',username):raise LeaseError('invalid_database_user')
            result['database_user']=username
        return result

    def bind(self, key, node, identity):
        identity = self.identity(identity)
        with self.lock:
            row = self.get(key, node)
            if row['state'] == 'connected' and row['identity'] == identity: return row
            if row['state'] != 'connecting': raise LeaseError('lease_not_connecting')
            with self.transaction():
                self.db.execute("UPDATE remote_lease SET state='connected',identity=?,heartbeat=? WHERE id=?",
                                (json.dumps(identity, sort_keys=True), self.clock(), key))
            return self.get(key, node)

    def heartbeat(self, key, node):
        with self.lock:
            row = self.get(key, node)
            if row['state'] not in ('connecting', 'connected'): raise LeaseError('lease_not_active')
            with self.transaction():
                self.db.execute('UPDATE remote_lease SET heartbeat=? WHERE id=?', (self.clock(), key))
            return {'state': row['state']}

    def start_query(self, key, node, *, setup=False):
        with self.lock:
            row = self.get(key, node)
            if row['state'] != 'connected' and not (setup and row['state'] == 'connecting'):
                raise LeaseError('lease_not_active')
            now = self.clock()
            if now-row['heartbeat'] > self.heartbeat_seconds: raise LeaseError('lease_heartbeat_lost')
            rate = self.db.execute('SELECT next_start FROM query_rate WHERE node=?', (node,)).fetchone()
            if rate and rate[0] > now: raise LeaseError('query_rate', retry_after=rate[0]-now)
            with self.transaction():
                self.db.execute('INSERT OR REPLACE INTO query_rate VALUES(?,?)', (node, now+0.5))
                self.db.execute('UPDATE remote_lease SET heartbeat=? WHERE id=?', (now, key))
            return {'allowed': True}

    def expire(self):
        with self.lock:
            boundary = self.clock()-self.heartbeat_seconds
            with self.transaction():
                self.db.execute("UPDATE remote_lease SET state='cancelled',reason='expired_before_connect' WHERE state='reserved' AND heartbeat<?", (boundary,))
                self.db.execute("UPDATE remote_lease SET state='unknown',reason='heartbeat_lost' WHERE state IN ('connecting','connected') AND heartbeat<?", (boundary,))

    def release(self, key, node, *, server_eof=False, identity=None):
        with self.lock:
            row = self.get(key, node)
            if row['state'] in ('closed','cancelled'): return {'state': row['state']}
            if row['state'] != 'reserved':
                if server_eof is not True: raise LeaseError('server_close_not_confirmed')
                if row['identity'] is not None and self.identity(identity) != row['identity']:
                    raise LeaseError('database_identity_changed')
            with self.transaction():
                self.db.execute("UPDATE remote_lease SET state=?,reason=? WHERE id=?",
                                ('cancelled' if row['state']=='reserved' else 'closed', 'never_connected' if row['state']=='reserved' else 'server_eof', key))
            return self.get(key, node)

    def observe_presence(self,key,*,observed_server_uuid,observed_connection_id):
        with self.lock:
            row=self.get(key);identity=row['identity']
            if (row['state'] not in ('connected','unknown') or identity is None
                or observed_server_uuid!=identity['server_uuid'] or observed_connection_id!=identity['connection_id']):
                raise LeaseError('session_visibility_not_proven')
            with self.transaction():
                self.db.execute('INSERT OR IGNORE INTO session_visibility VALUES(?,?)',(key,self.clock()))

    def reconcile_absence(self, key, *, observed_server_uuid, observed_connection_id, session_present):
        """Only called by the CPU reconciler after its own bounded database read."""
        with self.lock:
            row = self.get(key)
            identity = row['identity']
            if row['state'] != 'unknown' or identity is None: raise LeaseError('unidentified_session_retained')
            if (observed_server_uuid != identity['server_uuid'] or observed_connection_id != identity['connection_id']
                    or session_present is not False):
                raise LeaseError('session_absence_not_proven')
            if not self.db.execute('SELECT 1 FROM session_visibility WHERE lease_id=?',(key,)).fetchone():
                raise LeaseError('prior_session_visibility_not_proven')
            with self.transaction():
                self.db.execute("UPDATE remote_lease SET state='closed',reason='verified_session_absent' WHERE id=?", (key,))
            return self.get(key)
