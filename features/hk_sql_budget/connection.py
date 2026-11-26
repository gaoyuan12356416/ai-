"""One private HK MySQL session per durable shared lease."""
from contextlib import contextmanager
import os
import socket
import threading
import time

from .ledger import BudgetError, local_ledger


HOST = "10.89.0.108"
PORT = 3306


class _Session:
    def __init__(self, deadline, role):
        self.deadline = deadline
        self.role = role
        self.key = None
        self.connection = None
        self.business = False
        self.locally_aborted = False
        self.closing = False
        self.timer = None
        self.ledger = None
        self.authentication_started = False

    def before_authentication(self):
        self.ledger.opening(self.key, self.deadline)
        self.authentication_started = True

    def remaining(self):
        seconds = self.deadline - time.monotonic()
        if seconds <= 0:
            raise BudgetError("hk_sql_deadline")
        return seconds

    def admit_sql(self, sql):
        if self.closing:
            return
        self.remaining()
        if self.role == "read" and self.business:
            if isinstance(sql, (bytes, bytearray)):
                sql = sql.decode("utf-8", "replace")
            cmd = str(sql).strip().rstrip(";").strip()
            first = cmd.split(None, 1)[0].upper() if cmd else ""
            if first not in ("SELECT", "SHOW", "EXPLAIN") and cmd.upper() not in ("COMMIT", "ROLLBACK"):
                raise BudgetError("business_statement_must_be_read_only")

    def start_deadline(self):
        def abort():
            self.locally_aborted = True
            wire = getattr(self.connection, "_sock", None)
            if wire is not None:
                try:
                    wire.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        self.timer = threading.Timer(self.remaining(), abort)
        self.timer.daemon = True
        self.timer.start()

    def finish(self):
        """COM_QUIT has no reply packet. A peer EOF confirms transport closure."""
        self.closing = True
        if self.timer is not None:
            self.timer.cancel()
            self.timer.join()
        con = self.connection
        if con is None:
            return False
        try:
            if self.locally_aborted:
                return False
            wire = con._sock
            if wire is None:
                return False
            try:
                with con.cursor() as cur:
                    cur.execute("ROLLBACK")
            except Exception:
                return False
            con._write_bytes(b"\x01\x00\x00\x00\x01")
            wire.settimeout(min(1.0, max(0.02, self.deadline-time.monotonic())))
            return wire.recv(1) == b""
        except (OSError, AttributeError, ValueError, RuntimeError):
            return False
        except Exception:
            # Drivers translate wire failures into their own exception types.
            return False
        finally:
            con._force_close()


def _values(row, columns):
    return [row[c] for c in columns] if isinstance(row, dict) else list(row)


def _ordinary_connection(session, user, password, database, dict_cursor):
    import pymysql
    from pymysql.constants import COMMAND

    class Connection(pymysql.connections.Connection):
        def _request_authentication(self):
            # PyMySQL has read the server greeting; no credentials were sent.
            # A first-packet timeout needs no unknown session reservation.
            session.before_authentication()
            return super()._request_authentication()

        def _execute_command(self, command, sql):
            if command == COMMAND.COM_QUERY:
                session.admit_sql(sql)
            return super()._execute_command(command, sql)

        def ping(self, reconnect=False):
            if reconnect:
                raise BudgetError("implicit_reconnect_requires_new_lease")
            return super().ping(reconnect=False)

    remaining = session.remaining()
    conn = Connection(host=HOST, port=PORT, user=user, password=password,
                      database=database, charset="utf8mb4", autocommit=True,
                      cursorclass=pymysql.cursors.DictCursor if dict_cursor else pymysql.cursors.Cursor,
                      defer_connect=True, connect_timeout=min(4,remaining),
                      read_timeout=min(4,remaining), write_timeout=min(4,remaining),
                      program_name="hk-sql-budget/" + session.key, local_infile=False)
    session.connection = conn
    return conn


@contextmanager
def session_connection(role, deadline, *, database=None, statement_timeout_ms=8000,
                       report_options=False, dict_cursor=False, user=None, password=None):
    if role not in ("read", "write"):
        raise ValueError("invalid_sql_role")
    deadline = time.monotonic()+45 if deadline is None else float(deadline)
    session = _Session(deadline, role)
    session.remaining()
    user = user or os.environ["SQL_OFFLOAD_USER"]
    password = password or os.environ["SQL_OFFLOAD_PASSWORD"]
    statement_timeout_ms = max(1, int(statement_timeout_ms))
    ledger = session.ledger = local_ledger()
    token = session.key = ledger.reserve(role, deadline)
    safe_failure = False
    raw = None
    try:
        conn = _ordinary_connection(session, user, password, database, dict_cursor)
        raw = socket.create_connection((HOST,PORT), timeout=min(4,session.remaining()))
        conn.connect(sock=raw)
        conn._read_timeout = min(60,session.remaining())
        conn._write_timeout = min(15,session.remaining())
        session.start_deadline()
        with conn.cursor() as cur:
            cur.execute("SET SESSION MAX_EXECUTION_TIME=%s",
                        (min(statement_timeout_ms, max(1,int(session.remaining()*1000))),))
            mode = "READ ONLY" if role == "read" else "READ WRITE"
            cur.execute("SET SESSION TRANSACTION " + mode)
            if report_options:
                cur.execute("SET SESSION range_optimizer_max_mem_size=67108864")
                cur.execute("SET SESSION time_zone='+08:00'")
            cur.execute("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            cur.execute("START TRANSACTION " + mode)
            cur.execute("SELECT @@server_uuid AS server_uuid,CONNECTION_ID() AS connection_id,"
                        "CURRENT_USER() AS account,@@session.tx_read_only AS tx_ro,"
                        "@@global.read_only AS server_ro")
            server, cid, account, tx_ro, server_ro = _values(cur.fetchone(),
                ("server_uuid","connection_id","account","tx_ro","server_ro"))
            identity = (str(server),int(cid),str(account))
        ledger.bind(token, identity, deadline)
        if int(tx_ro) != int(role == "read") or (role == "write" and int(server_ro) != 0):
            raise BudgetError("database_role_verification_failed")
        # Only inspect the processlist when a recorded session needs reconciliation.
        pending = ledger.periodic_candidates(role, deadline)
        if pending:
            ledger.verify_absence(conn, identity, pending, deadline)
        session.business = True
        session.remaining()
        yield conn
        if session.locally_aborted:
            raise BudgetError("hk_sql_deadline")
    except BaseException as error:
        # Authentication rejection cannot have created an authorized DB session.
        if not session.business and getattr(error,"args",()) and error.args[0] in (1045,1130):
            safe_failure = True
        raise
    finally:
        confirmed = not session.authentication_started or safe_failure
        try:
            if session.authentication_started:
                confirmed = session.finish() or confirmed
        finally:
            if raw is not None:
                raw.close()
            # Preserve the existing cleanup allowance even after a request expires.
            ledger.end(token, confirmed, max(deadline, time.monotonic() + 2.0))


def budget_connection(role="read", deadline=None):
    """Yield a tuple-cursor PyMySQL connection inside a verified transaction.

    A deadline is an absolute time.monotonic() value. Writes require caller commit.
    """
    return session_connection(role, deadline)
