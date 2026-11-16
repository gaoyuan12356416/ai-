"""Compressed MySQL protocol transport; stored SQL values stay unchanged."""
from __future__ import annotations


class Connection:
    def __init__(self, raw):
        self.raw = raw

    def begin(self):
        return self.raw.start_transaction()

    def __getattr__(self, name):
        return getattr(self.raw, name)


def connect_writer(config, *, connect=None):
    if connect is None:
        from mysql.connector import connect
    raw = connect(**config, charset="utf8mb4", collation="utf8mb4_general_ci",
                  autocommit=False, buffered=True, use_pure=True, compress=True,
                  connection_timeout=3, read_timeout=5, write_timeout=5)
    return Connection(raw)
