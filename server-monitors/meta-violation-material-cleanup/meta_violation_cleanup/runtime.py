"""Durable run/notification state separate from the shared deletion ledger."""
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time
import uuid

CST = timezone(timedelta(hours=8))
DATA_UUID = '3e8ac4e8-7770-456d-9e89-2ec5dd405fa8'
_RUN_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,99}\Z')
_ARTIFACT = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z')
_LOCAL_IDENTITY = uuid.uuid4().hex


class RuntimeLedgerError(RuntimeError):
    code = 'runtime_ledger_unavailable'


def now():
    return datetime.now(CST).isoformat(timespec='seconds')


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str)


def digest(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def validate_run_id(run_id):
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        raise ValueError('invalid_run_id')
    return run_id


def _phase(phase):
    match = re.fullmatch(r'(start|end)(?:-([1-9][0-9]{0,8}))?(?:-part-([0-9]{3,6}))?', phase) if isinstance(phase, str) else None
    if not match:
        raise ValueError('invalid_notice_phase')
    attempt, part = match.group(2), match.group(3)
    if attempt is not None and int(attempt) < 2:
        raise ValueError('invalid_notice_phase')
    if part is not None and (int(part) < 1 or str(int(part)).zfill(3) != part):
        raise ValueError('invalid_notice_phase')


def _process_start(pid):
    """False proves death; None is indeterminate and must keep the fence."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    if os.name == 'nt':
        return _LOCAL_IDENTITY if pid == os.getpid() else None
    try:
        raw = Path('/proc/{}/stat'.format(pid)).read_text(encoding='ascii')
        fields = raw[raw.rfind(')') + 2:].split()
        return False if fields[0] == 'Z' else fields[19]
    except FileNotFoundError:
        return False
    except (OSError, IndexError, UnicodeError):
        return None


def _owner():
    start = _process_start(os.getpid())
    if start is None or start is False:
        raise RuntimeLedgerError('process_identity_unavailable')
    return {'pid': os.getpid(), 'start': str(start)}


def _owner_alive(pid, start):
    current = _process_start(pid)
    if current is False:
        return False
    if current is None or not start:
        return None
    return str(current) == str(start)


def _fsync_directory(path):
    if os.name == 'nt':
        return
    fd = os.open(str(path), os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def verify_mount():
    p = subprocess.run(['findmnt', '-no', 'UUID', '--target', '/mnt/data-disk'],
                       capture_output=True, text=True, timeout=5)
    if p.returncode or p.stdout.strip() != DATA_UUID:
        raise RuntimeError('required_data_disk_unavailable')
    if not os.access('/mnt/data-disk', os.W_OK):
        raise RuntimeError('data_disk_not_writable')


@contextmanager
def process_lock(path, blocking=False):
    import fcntl
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'a') as fp:
        fcntl.flock(fp, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        try:
            yield fp
        finally:
            fcntl.flock(fp, fcntl.LOCK_UN)


def _legacy_priority(t=None):
    t = t or datetime.now(CST)
    return (t.hour == 9 and t.minute >= 55) or (t.hour == 10 and t.minute < 5)


class LegacyPriorityWindow(InterruptedError):
    """Cooperative handoff, not a worker failure or a write-pause reason."""


class _LegacyLease:
    def __init__(self, stop):
        self.stop = stop

    def check(self):
        if self.stop():
            raise InterruptedError('stopping')
        if _legacy_priority():
            # The caller drains the current request and releases the lock.
            raise LegacyPriorityWindow('legacy_priority_window')


@contextmanager
def legacy_delete_lock(path, stop):
    """Fence the old 10:00 cron; check the lease before every new DELETE."""
    import fcntl
    with open(path, 'a') as fp:
        while True:
            if stop():
                raise InterruptedError('stopping')
            if _legacy_priority():
                time.sleep(5)
                continue
            try:
                fcntl.flock(fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(5)
        try:
            lease = _LegacyLease(stop)
            lease.check()
            yield lease
        finally:
            fcntl.flock(fp, fcntl.LOCK_UN)


class Runtime:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self._unhealthy = False
        try:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.db = sqlite3.connect(str(self.root / 'runs.sqlite3'), timeout=30,
                                      isolation_level=None)
            self.db.row_factory = sqlite3.Row
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.execute('PRAGMA busy_timeout=30000')
            self.db.executescript('''
            CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, mode TEXT, status TEXT, started TEXT, ended TEXT, data TEXT);
            CREATE TABLE IF NOT EXISTS notices (run_id TEXT, phase TEXT, uuid TEXT, status TEXT, body TEXT, receipt TEXT, updated TEXT, owner_pid INTEGER, owner_start TEXT, PRIMARY KEY(run_id,phase));
            CREATE TABLE IF NOT EXISTS inventory (run_id TEXT, account_id TEXT, ad_id TEXT, creative_id TEXT, data TEXT, PRIMARY KEY(run_id,ad_id));
            CREATE INDEX IF NOT EXISTS inventory_creative ON inventory(run_id,creative_id);
            CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, run_id TEXT, at TEXT, kind TEXT, data TEXT);
            ''')
            columns = {row['name'] for row in self.db.execute('PRAGMA table_info(notices)')}
            for name, kind in (('owner_pid', 'INTEGER'), ('owner_start', 'TEXT')):
                if name not in columns:
                    self.db.execute('ALTER TABLE notices ADD COLUMN {} {}'.format(name, kind))
            _fsync_directory(self.root)
        except (sqlite3.Error, OSError):
            self._unhealthy = True
            raise RuntimeLedgerError('runtime_ledger_unavailable') from None

    def close(self):
        self.db.close()

    def _healthy(self):
        if self._unhealthy:
            raise RuntimeLedgerError('runtime_ledger_unavailable')

    @contextmanager
    def _transaction(self):
        self._healthy()
        try:
            self.db.execute('BEGIN IMMEDIATE')
            yield
            self.db.commit()
        except (sqlite3.Error, OSError):
            self._unhealthy = True
            try:
                self.db.rollback()
            except sqlite3.Error:
                pass
            raise RuntimeLedgerError('runtime_ledger_unavailable') from None
        except BaseException:
            self.db.rollback()
            raise

    def begin(self, run_id, mode, data):
        validate_run_id(run_id)
        if mode not in ('preview', 'run'):
            raise ValueError('invalid_run_mode')
        data = dict(data, _owner=_owner())
        with self._transaction():
            if self.db.execute('SELECT 1 FROM runs WHERE id=?', (run_id,)).fetchone():
                return False
            self.db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?)',
                            (run_id, mode, 'running', now(), None, canonical(data)))
        return True

    def get(self, run_id):
        validate_run_id(run_id)
        row = self.db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
        if not row:
            raise ValueError('run_not_found')
        return dict(row, data=json.loads(row['data']))

    def update(self, run_id, status=None, **data):
        validate_run_id(run_id)
        with self._transaction():
            r = self.get(run_id)
            r['data'].update(data)
            # Only CLI holding process_lock may transition an existing run.
            if status == 'running':
                r['data']['_owner'] = _owner()
            ended = None if status == 'running' else (now() if status else r['ended'])
            self.db.execute('UPDATE runs SET status=?,data=?,ended=? WHERE id=?',
                            (status or r['status'], canonical(r['data']), ended, run_id))

    def event(self, run_id, kind, data):
        validate_run_id(run_id)
        with self._transaction():
            self.get(run_id)
            self.db.execute('INSERT INTO events(run_id,at,kind,data) VALUES(?,?,?,?)',
                            (run_id, now(), kind, canonical(data)))

    def write_barrier(self, run_id):
        """Actual FULL-synchronous write, required immediately before DELETE."""
        self.event(run_id, 'mutation_write_barrier', {})

    def inventory(self, run_id, account, ads):
        validate_run_id(run_id)
        with self._transaction():
            self.get(run_id)
            for ad in ads:
                row = (run_id, str(account), str(ad['id']),
                       str((ad.get('creative') or {}).get('id', '')), canonical(ad))
                old = self.db.execute('SELECT * FROM inventory WHERE run_id=? AND ad_id=?',
                                      (run_id, row[2])).fetchone()
                if old and tuple(old) != row:
                    raise ValueError('inventory_identity_changed')
                if not old:
                    self.db.execute('INSERT INTO inventory VALUES(?,?,?,?,?)', row)

    def references(self, run_id, creative):
        validate_run_id(run_id)
        return [json.loads(r['data']) for r in self.db.execute(
            'SELECT data FROM inventory WHERE run_id=? AND creative_id=?', (run_id, creative))]

    def _cleanup_queue_schema(self):
        self._healthy()
        if getattr(self,'_cleanup_queue_ready',False):return
        with self._transaction():
            self.db.execute('CREATE TABLE IF NOT EXISTS cleanup_backlog '
                            '(creative_id TEXT PRIMARY KEY, data TEXT NOT NULL, updated TEXT NOT NULL)')
        self._cleanup_queue_ready=True

    def cleanup_backlog(self):
        self._cleanup_queue_schema()
        return [json.loads(row['data']) for row in self.db.execute(
            'SELECT data FROM cleanup_backlog ORDER BY creative_id')]

    def queue_creative(self, entry):
        self._cleanup_queue_schema()
        cid=str(entry['basis']['creative_id'])
        if not re.fullmatch(r'[1-9][0-9]{0,31}',cid): raise ValueError('invalid_creative_identity')
        with self._transaction():
            previous=self.db.execute('SELECT data FROM cleanup_backlog WHERE creative_id=?',(cid,)).fetchone()
            data=json.loads(previous['data']) if previous else {}
            if data and data['basis']['account_id']!=entry['basis']['account_id']:
                raise ValueError('backlog_account_conflict')
            data.update(entry)
            self.db.execute('INSERT OR REPLACE INTO cleanup_backlog VALUES(?,?,?)',(cid,canonical(data),now()))

    def complete_creative(self, creative_id):
        self._cleanup_queue_schema()
        with self._transaction():
            self.db.execute('DELETE FROM cleanup_backlog WHERE creative_id=?',(str(creative_id),))

    def _artifact_path(self, run_id, name):
        validate_run_id(run_id)
        if not isinstance(name, str) or not _ARTIFACT.fullmatch(name) or '..' in name:
            raise ValueError('invalid_artifact_name')
        base = self.root / 'runs'
        folder = base / run_id
        target = folder / name
        for path in (base, folder, target):
            if path.is_symlink():
                raise ValueError('artifact_symlink_forbidden')
            try:
                path.resolve().relative_to(self.root)
            except ValueError:
                raise ValueError('artifact_path_outside_runtime') from None
        return folder, target

    def artifact(self, run_id, name, data):
        self._healthy()
        self.get(run_id)
        folder, target = self._artifact_path(run_id, name)
        tmp = folder / ('.write-' + uuid.uuid4().hex)
        try:
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            _fsync_directory(self.root)
            _fsync_directory(folder.parent)
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                         getattr(os, 'O_NOFOLLOW', 0), 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as f:
                f.write(canonical(data))
                f.flush()
                os.fsync(f.fileno())
            os.replace(str(tmp), str(target))
            _fsync_directory(folder)
            return str(target)
        except OSError:
            self._unhealthy = True
            raise RuntimeLedgerError('runtime_artifact_unavailable') from None
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass

    def load_artifact(self, run_id, name):
        self.get(run_id)
        _, target = self._artifact_path(run_id, name)
        try:
            return json.loads(target.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            raise ValueError('artifact_unavailable_or_invalid') from None

    def list(self, limit=10):
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
            raise ValueError('invalid_run_limit')
        return [dict(r, data=json.loads(r['data'])) for r in self.db.execute(
            'SELECT * FROM runs ORDER BY started DESC LIMIT ?', (limit,))]

    def notice(self, run_id, phase):
        validate_run_id(run_id)
        _phase(phase)
        row = self.db.execute('SELECT * FROM notices WHERE run_id=? AND phase=?',
                              (run_id, phase)).fetchone()
        return dict(row) if row else None

    def claim_notice(self, run_id, phase, body):
        validate_run_id(run_id)
        _phase(phase)
        if not isinstance(body, str):
            raise ValueError('invalid_notice_body')
        with self._transaction():
            self.get(run_id)
            if not self.notice(run_id, phase):
                uid = digest({'run_id': run_id, 'phase': phase})[:32]
                self.db.execute('INSERT INTO notices(run_id,phase,uuid,status,body,receipt,updated) VALUES(?,?,?,?,?,?,?)',
                                (run_id, phase, uid, 'pending', body, '{}', now()))
        return self.notice(run_id, phase)

    def _check_notice_parts(self, run_id, phase, receipt, require_sent=False):
        if not isinstance(receipt, dict) or receipt.get('delivery') != 'multipart':
            return
        parts = receipt.get('parts')
        if '-part-' in phase or not isinstance(parts, list) or not 2 <= len(parts) <= 999999:
            raise ValueError('invalid_multipart_notice')
        for index, part in enumerate(parts, 1):
            expected = phase + '-part-' + str(index).zfill(3)
            if not isinstance(part, dict) or part.get('phase') != expected:
                raise ValueError('invalid_multipart_notice')
            row = self.notice(run_id, expected)
            if not row or digest(row['body']) != part.get('body_sha256'):
                raise ValueError('multipart_notice_body_mismatch')
            if require_sent and (row['status'] != 'sent' or json.loads(row['receipt']) != part.get('receipt')):
                raise ValueError('multipart_notice_not_all_sent')

    def start_notice(self, run_id, phase, receipt=None):
        with self._transaction():
            notice = self.notice(run_id, phase)
            if not notice:
                raise ValueError('notice_not_found')
            if notice['status'] not in ('pending', 'failed'):
                return dict(notice, claimed=False)
            self._check_notice_parts(run_id, phase, receipt)
            owner = _owner()
            self.db.execute('UPDATE notices SET status=?,receipt=?,updated=?,owner_pid=?,owner_start=? WHERE run_id=? AND phase=?',
                            ('sending', canonical(receipt or {}), now(), owner['pid'], owner['start'], run_id, phase))
        return dict(self.notice(run_id, phase), claimed=True)

    def finish_notice(self, run_id, phase, status, receipt):
        if status not in ('sent', 'failed', 'unknown'):
            raise ValueError('invalid_notice_status')
        with self._transaction():
            old = self.notice(run_id, phase)
            if not old:
                raise ValueError('notice_not_found')
            if old['status'] in ('sent', 'unknown'):
                if old['status'] == status and old['receipt'] == canonical(receipt):
                    return
                raise ValueError('notice_terminal_requires_reconciliation')
            if old['status'] == 'sending':
                owner = _owner()
                if old['owner_pid'] != owner['pid'] or old['owner_start'] != owner['start']:
                    raise ValueError('notice_owner_mismatch')
            elif status != 'failed':
                raise ValueError('notice_not_sending')
            previous = json.loads(old['receipt'])
            if previous.get('delivery') == 'multipart':
                if not isinstance(receipt, dict) or receipt.get('delivery') != 'multipart':
                    raise ValueError('multipart_notice_plan_changed')
                before = [(p.get('phase'), p.get('body_sha256')) for p in previous['parts']]
                after = [(p.get('phase'), p.get('body_sha256')) for p in receipt.get('parts', [])]
                if before != after:
                    raise ValueError('multipart_notice_plan_changed')
            self._check_notice_parts(run_id, phase, receipt, require_sent=status == 'sent')
            self.db.execute('UPDATE notices SET status=?,receipt=?,updated=? WHERE run_id=? AND phase=?',
                            (status, canonical(receipt), now(), run_id, phase))

    def recover_interrupted(self):
        """Under process_lock, fence only provably dead owners of this runtime.

        The caller may finish the matching shared_run_id as interrupted after
        verifying the shared job identity, then clear recovery_pending. Until
        that acknowledgement this method returns the dead run again, including
        after restart. Never recover the whole shared Store.
        """
        recovered = []
        live = 0
        notices_unknown = 0
        with self._transaction():
            for row in self.db.execute("SELECT * FROM runs WHERE status='running'").fetchall():
                data = json.loads(row['data'])
                owner = data.get('_owner') or {}
                if _owner_alive(owner.get('pid'), owner.get('start')) is not False:
                    live += 1
                    continue
                # Keep this local commit discoverable until the CLI confirms
                # the matching shared-Store fence has committed as well.
                data.update(recovery_reason='owner_terminated', recovered_at=now(),
                            recovery_pending=True)
                self.db.execute('UPDATE runs SET status=?,ended=?,data=? WHERE id=?',
                                ('interrupted', now(), canonical(data), row['id']))
                self.db.execute('INSERT INTO events(run_id,at,kind,data) VALUES(?,?,?,?)',
                                (row['id'], now(), 'run_recovered', '{}'))
                recovered.append(row['id'])
            for row in self.db.execute("SELECT * FROM runs WHERE status='interrupted'").fetchall():
                data = json.loads(row['data'])
                owner = data.get('_owner') or {}
                if (data.get('recovery_pending') is True and row['id'] not in recovered
                        and _owner_alive(owner.get('pid'), owner.get('start')) is False):
                    recovered.append(row['id'])
            for row in self.db.execute("SELECT * FROM notices WHERE status='sending'").fetchall():
                dead = _owner_alive(row['owner_pid'], row['owner_start']) is False
                if not dead and row['run_id'] not in recovered:
                    continue
                receipt = json.loads(row['receipt'])
                receipt['reason'] = 'sender_terminated'
                self.db.execute('UPDATE notices SET status=?,receipt=?,updated=? WHERE run_id=? AND phase=?',
                                ('unknown', canonical(receipt), now(),
                                 row['run_id'], row['phase']))
                notices_unknown += 1
        return {'run_ids': recovered, 'notices_unknown': notices_unknown, 'live_runs': live}
