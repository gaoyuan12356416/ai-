"""Versioned upload and attribution boundaries for frozen X work."""
from __future__ import annotations

CURRENT_VERSION = 2
LEGACY_VERSION = 1
STANDARD_LIMITS = {LEGACY_VERSION: 140.0, CURRENT_VERSION: 1200.0}
PREMIUM_LIMIT = 14400.0


def version(record=None):
    """Missing metadata on an existing record means the historical contract."""
    value = CURRENT_VERSION if record is None else dict(record).get(
        "duration_policy_version", LEGACY_VERSION
    )
    if type(value) is not int or value not in STANDARD_LIMITS:
        raise ValueError("invalid X duration policy version")
    return value


def standard_limit(record=None):
    return STANDARD_LIMITS[version(record)]


def repair_policy(record=None):
    return "standard_20m" if version(record) == CURRENT_VERSION else "standard"


def identity_suffix(record):
    # Keep historical GPU job keys byte-for-byte unchanged.
    return ("duration-policy-v2",) if version(record) == CURRENT_VERSION else ()


def migrate(conn, tables):
    """Add a legacy default; current writers must explicitly freeze version 2."""
    for table in tables:
        columns = {row[1] for row in conn.execute('PRAGMA table_info("%s")' % table)}
        if "duration_policy_version" not in columns:
            conn.execute(
                'ALTER TABLE "%s" ADD COLUMN duration_policy_version '
                'INTEGER NOT NULL DEFAULT 1 CHECK(duration_policy_version IN (1,2))' % table
            )
        conn.execute(
            'CREATE TRIGGER IF NOT EXISTS "trg_%s_duration_policy_immutable" '
            'BEFORE UPDATE OF duration_policy_version ON "%s" '
            'WHEN NEW.duration_policy_version<>OLD.duration_policy_version '
            "BEGIN SELECT RAISE(ABORT,'frozen X duration policy'); END" % (table, table)
        )
