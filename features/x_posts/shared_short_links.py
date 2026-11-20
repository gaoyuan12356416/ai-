"""One durable, idempotent short-link sequence for both X publishing ledgers.

Internal queue/log IDs and attribution URLs are never renumbered. Historical
wrappers keep their original paths; only newly prepared links opt into /s/ID.
"""
from contextlib import closing
import os
from pathlib import Path
import sqlite3

PUBLIC_BASE = "https://gy.g2flow.com/s"
ORIGINS = {"https://gy.g2flow.com/s2l": "x", "https://gy.g2flow.com/s2l-tag": "x_tag"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS short_link (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    origin TEXT NOT NULL CHECK(origin IN ('x','x_tag')),
    log_id INTEGER NOT NULL CHECK(log_id > 0),
    long_url TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(origin, log_id)
);
"""


def fail(code, status=409):
    from features.x_posts.service import XPostError
    raise XPostError(code, "统一短链存储不可用或编号映射冲突", status)


def configured_root():
    value = os.environ.get("X_POST_SHARED_SHORT_ROOT", "").strip()
    return Path(value) if value else None


def initialize(root):
    """Deployment/test setup only. Runtime never creates a missing database."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    (root / "public").mkdir(exist_ok=True)
    with closing(sqlite3.connect(root / "links.sqlite3")) as conn:
        conn.executescript(SCHEMA)
        conn.commit()


def validate_root(root):
    root = Path(root)
    database = root / "links.sqlite3"
    public = root / "public"
    if (not root.is_absolute() or not root.is_dir() or root.is_symlink()
            or not database.is_file() or database.is_symlink()
            or not public.is_dir() or public.is_symlink()):
        fail("x_shared_short_storage_unavailable", 503)
    # Production state is on the mounted data volume, never its unmounted stub.
    mount = Path('/mnt/data-disk')
    if root.is_relative_to(mount):
        if (not os.path.ismount(mount) or root.resolve().parent != mount.resolve()
                or len({p.stat().st_dev for p in (mount, root, database, public)}) != 1):
            fail("x_shared_short_storage_unavailable", 503)
    return database, public


def identity(base_url, log_id):
    origin = ORIGINS.get(str(base_url).rstrip('/'))
    if origin is None or isinstance(log_id, bool) or not str(log_id).isdigit() or int(log_id) <= 0:
        fail("x_shared_short_identity_invalid")
    return origin, int(log_id)


def connect(root):
    database, _ = validate_root(root)
    conn = sqlite3.connect(database.as_uri() + '?mode=rw', uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA synchronous=FULL')
    return conn


def allocate(base_url, log_id):
    root = configured_root()
    if root is None:
        return None
    origin, log_id = identity(base_url, log_id)
    try:
        with closing(connect(root)) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT id FROM short_link WHERE origin=? AND log_id=?', (origin, log_id)).fetchone()
            if row is None:
                if os.environ.get('X_POST_SHARED_SHORT_ALLOCATE', '1') != '1':
                    return None
                short_id = conn.execute('INSERT INTO short_link(origin,log_id) VALUES(?,?)', (origin, log_id)).lastrowid
            else:
                short_id = row['id']
        return PUBLIC_BASE + '/' + str(short_id)
    except (sqlite3.Error, OSError):
        fail('x_shared_short_storage_unavailable', 503)


def write_allocated(base_url, log_id, long_url, write_file):
    """Resolve the source ledger's allocation, binding its destination once.

    Serialize the immutable file write with its mapping commit. A crash between
    the file and commit is recoverable only for the exact same destination.
    Unallocated historical logs continue through the legacy wrapper writer.
    """
    root = configured_root()
    if root is None:
        return None
    origin, log_id = identity(base_url, log_id)
    try:
        with closing(connect(root)) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM short_link WHERE origin=? AND log_id=?', (origin, log_id)).fetchone()
            if row is None:
                return None
            if row['long_url'] and row['long_url'] != long_url:
                fail('x_shared_short_target_conflict')
            _, public = validate_root(root)
            path = write_file(public, row['id'], long_url)
            if not row['long_url']:
                conn.execute('UPDATE short_link SET long_url=? WHERE id=?', (long_url, row['id']))
            return path
    except (sqlite3.Error, OSError):
        fail('x_shared_short_storage_unavailable', 503)
