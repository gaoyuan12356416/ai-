"""Process-shared FIFO admission and durable MySQL session identity on the HK host."""
from contextlib import closing, contextmanager
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid


CAPACITY = 30
DEFAULT_DIRECTORY = "/data/fb-page-assignment-v2/sql-budget"


class BudgetError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def process_identity(pid):
    """A PID alone is reusable. Proc start ticks and kernel boot ID identify its owner."""
    try:
        stat = Path("/proc", str(pid), "stat").read_text().rsplit(")", 1)[1].split()
        if stat[0] == "Z":
            return None
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        return boot + "/" + stat[19]
    except FileNotFoundError:
        if os.name == "posix":
            return None
        return str(pid) if pid == os.getpid() else "unverifiable"
    except (OSError, IndexError):
        return "unverifiable"


def owner_finished(row):
    current = process_identity(row["pid"])
    return current is None or (current != "unverifiable" and current != row["owner"])


class Ledger:
    def __init__(self, directory=None):
        self.directory = Path(directory or os.environ.get("HK_SQL_BUDGET_DIR", DEFAULT_DIRECTORY))
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = self.directory / "leases.sqlite3"
        self._check_lock = threading.Lock()
        self._next_check = {}
        with closing(self._connect()) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS config (
                    id INTEGER PRIMARY KEY CHECK(id=1), capacity INTEGER NOT NULL,
                    legacy_reserved INTEGER NOT NULL);
                INSERT OR IGNORE INTO config VALUES (1,30,0);
                CREATE TABLE IF NOT EXISTS waiter (
                    ticket INTEGER PRIMARY KEY AUTOINCREMENT, token TEXT UNIQUE NOT NULL,
                    pid INTEGER NOT NULL, owner TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS lease (
                    token TEXT PRIMARY KEY, role TEXT NOT NULL, pid INTEGER NOT NULL,
                    owner TEXT NOT NULL, state TEXT NOT NULL, created REAL NOT NULL,
                    server_uuid TEXT, connection_id INTEGER, account TEXT);
            """)

    def _connect(self):
        db = sqlite3.connect(str(self.path), timeout=2, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        return db

    @contextmanager
    def _transaction(self):
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def configure(self, *, capacity=None, legacy_reserved=None):
        with self._transaction() as db:
            config = db.execute("SELECT capacity,legacy_reserved FROM config").fetchone()
            cap = config["capacity"] if capacity is None else int(capacity)
            old = config["legacy_reserved"] if legacy_reserved is None else int(legacy_reserved)
            if not 1 <= cap <= CAPACITY or not 0 <= old <= CAPACITY:
                raise ValueError("invalid_hk_sql_capacity")
            db.execute("UPDATE config SET capacity=?,legacy_reserved=? WHERE id=1", (cap, old))

    def status(self):
        with self._transaction() as db:
            config = dict(db.execute("SELECT capacity,legacy_reserved FROM config").fetchone())
            states = dict(db.execute("SELECT state,COUNT(*) FROM lease GROUP BY state"))
            roles = dict(db.execute("SELECT role,COUNT(*) FROM lease GROUP BY role"))
            queued = db.execute("SELECT COUNT(*) FROM waiter").fetchone()[0]
        used = sum(states.values()) + config["legacy_reserved"]
        return dict(**config, in_use=used, available=max(0, config["capacity"]-used),
                    queued=queued, roles=roles, states=states)

    def reserve(self, role, deadline):
        if role not in ("read", "write"):
            raise ValueError("invalid_sql_role")
        token, pid = uuid.uuid4().hex, os.getpid()
        owner = process_identity(pid)
        if not owner or owner == "unverifiable":
            raise BudgetError("process_identity_unavailable")
        queued = False
        next_cleanup = time.monotonic()
        try:
            while time.monotonic() < deadline:
                with self._transaction() as db:
                    cap, legacy = db.execute("SELECT capacity,legacy_reserved FROM config").fetchone()
                    occupied = db.execute("SELECT COUNT(*) FROM lease").fetchone()[0]
                    room = cap - legacy - occupied
                    if room <= 0 and time.monotonic() >= next_cleanup:
                        next_cleanup = time.monotonic()+1
                        for orphan in db.execute("SELECT token,pid,owner FROM lease WHERE state='reserved'").fetchall():
                            if owner_finished(orphan):
                                db.execute("DELETE FROM lease WHERE token=? AND state='reserved'", (orphan["token"],))
                                room += 1
                    if not queued:
                        db.execute("INSERT INTO waiter(token,pid,owner) VALUES(?,?,?)", (token,pid,owner))
                        queued = True
                    ahead = db.execute("""SELECT ticket,pid,owner FROM waiter WHERE ticket <
                        (SELECT ticket FROM waiter WHERE token=?) ORDER BY ticket""", (token,)).fetchall()
                    dead = [(row["ticket"],) for row in ahead if owner_finished(row)]
                    if dead:
                        db.executemany("DELETE FROM waiter WHERE ticket=?", dead)
                    if room > len(ahead) - len(dead) and time.monotonic() < deadline:
                        db.execute("INSERT INTO lease(token,role,pid,owner,state,created) VALUES(?,?,?,?,'reserved',?)",
                                   (token,role,pid,owner,time.time()))
                        db.execute("DELETE FROM waiter WHERE token=?", (token,))
                        queued = False
                        return token
                time.sleep(min(0.025, max(0, deadline-time.monotonic())))
            raise BudgetError("hk_sql_budget_timeout")
        finally:
            if queued:
                with self._transaction() as db:
                    db.execute("DELETE FROM waiter WHERE token=?", (token,))

    def opening(self, token):
        with self._transaction() as db:
            changed = db.execute("UPDATE lease SET state='opening' WHERE token=? AND state='reserved'", (token,))
            if changed.rowcount != 1:
                raise BudgetError("hk_sql_lease_missing")

    def bind(self, token, identity):
        with self._transaction() as db:
            changed = db.execute("""UPDATE lease SET state='bound',server_uuid=?,connection_id=?,account=?
                WHERE token=? AND state='opening'""", (*identity,token))
            if changed.rowcount != 1:
                raise BudgetError("hk_sql_lease_missing")

    def end(self, token, confirmed):
        with self._transaction() as db:
            if confirmed:
                db.execute("DELETE FROM lease WHERE token=?", (token,))
            else:
                db.execute("UPDATE lease SET state='unknown' WHERE token=?", (token,))

    def inspect_abandoned(self):
        """Only an owner that clearly died prior to an attempted DB open is released here."""
        with self._transaction() as db:
            for row in db.execute("SELECT token,pid,owner,state FROM lease WHERE state <> 'unknown'").fetchall():
                if owner_finished(row):
                    if row["state"] == "reserved":
                        db.execute("DELETE FROM lease WHERE token=?", (row["token"],))
                    else:
                        db.execute("UPDATE lease SET state='unknown' WHERE token=?", (row["token"],))
            return [dict(r) for r in db.execute("""SELECT token,role,server_uuid,connection_id,account,created
                FROM lease WHERE state='unknown' ORDER BY created""")]

    def periodic_candidates(self, role):
        # Scan process ownership at most once/second per worker and role.
        with self._check_lock:
            now = time.monotonic()
            if self._next_check.get(role, 0) > now:
                return ()
            self._next_check[role] = now+1
        return self.inspect_abandoned()

    def verify_absence(self, connection, identity, candidates):
        """Use an already admitted observer on the same instance and effective account."""
        server_id, observer_id, account = identity
        same = [r for r in candidates if r["server_uuid"] == server_id and r["account"] == account
                and r["connection_id"] and r["connection_id"] != observer_id]
        if not same:
            return 0
        ids = [observer_id] + [r["connection_id"] for r in same]
        with connection.cursor() as cur:
            marks = ",".join(["%s"]*len(ids))
            cur.execute("SELECT ID AS id FROM information_schema.processlist WHERE ID IN ("+marks+")", ids)
            visible = {int(r["id"] if isinstance(r, dict) else r[0]) for r in cur.fetchall()}
        if observer_id not in visible:
            return 0
        recovered = 0
        with self._transaction() as db:
            for row in same:
                if row["connection_id"] not in visible:
                    recovered += db.execute("""DELETE FROM lease WHERE token=? AND state='unknown'
                        AND server_uuid=? AND connection_id=? AND account=?""",
                        (row["token"],server_id,row["connection_id"],account)).rowcount
        return recovered


_instance = None
_instance_lock = threading.Lock()


def local_ledger():
    global _instance
    with _instance_lock:
        if _instance is None:
            _instance = Ledger()
        return _instance
