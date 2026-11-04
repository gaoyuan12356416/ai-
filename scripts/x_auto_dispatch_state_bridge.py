#!/usr/bin/env python3
"""Root-owned local broker for the Auto service's read-only dispatch snapshot.

It accepts only a positive queue_id, never SQL, credentials, or platform writes.
The root process can open SQLite WAL metadata while the Auto service retains
its existing filesystem restrictions and never opens the coordinator database.
"""
from __future__ import annotations

import argparse
import errno
import json
import os
from pathlib import Path
import socket
import socketserver
import sqlite3
import stat
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.x_auto_posts.dispatch_reader import (
    DEFAULT_SOCKET_PATH, MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES, READ_TIMEOUT,
    STATE_FIELDS, DispatchStateUnavailable, receive_line, valid_queue_id,
)
from features.x_auto_posts.publisher import read_dispatch_queue_state

DEFAULT_DB_PATH = "/mnt/data-disk/pipeline-dispatch/dispatch.sqlite3"


class StateHandler(socketserver.BaseRequestHandler):
    def handle(self):
        deadline = time.monotonic() + READ_TIMEOUT
        try:
            raw = receive_line(self.request, MAX_REQUEST_BYTES, deadline)
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict) or set(payload) != {"queue_id"} or not valid_queue_id(payload["queue_id"]):
                response = {"ok": False, "error": "invalid_request"}
            else:
                state = self.server.state_reader(payload["queue_id"])
                if not isinstance(state, dict):
                    raise ValueError("invalid state")
                response = {"ok": True, "state": {key: value for key, value in state.items() if key in STATE_FIELDS}}
        except (DispatchStateUnavailable, UnicodeError, ValueError, TypeError):
            response = {"ok": False, "error": "invalid_request"}
        except (OSError, sqlite3.Error):
            response = {"ok": False, "error": "state_unavailable"}
        # Response contents are always a fixed allowlist, never exception text.
        try:
            encoded = json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
            if len(encoded) > MAX_RESPONSE_BYTES:
                encoded = b'{"ok":false,"error":"state_unavailable"}\n'
            self.request.settimeout(READ_TIMEOUT)
            self.request.sendall(encoded)
        except (OSError, ValueError, TypeError):
            return


class DispatchStateServer(socketserver.ThreadingMixIn, getattr(socketserver, "UnixStreamServer", object)):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 16

    def __init__(self, socket_path, state_reader, *, max_handlers=16):
        if not hasattr(socket, "AF_UNIX"):
            raise RuntimeError("unix_sockets_required")
        self.state_reader = state_reader
        self.slots = threading.BoundedSemaphore(max_handlers)
        super().__init__(socket_path, StateHandler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        # Do not put request bytes or database exception text into the journal.
        return


def prepare_socket_path(socket_path, group):
    import grp
    target = Path(socket_path)
    if not target.is_absolute():
        raise ValueError("socket_path_must_be_absolute")
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    info = parent.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise ValueError("socket_directory_invalid")
    group_id = grp.getgrnam(group).gr_gid
    os.chown(parent, os.geteuid(), group_id)
    os.chmod(parent, 0o750)
    if target.exists() or target.is_symlink():
        info = target.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid():
            raise ValueError("socket_path_is_not_owned_socket")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.2)
                probe.connect(str(target))
        except OSError as exc:
            if exc.errno not in {errno.ECONNREFUSED, errno.ENOENT}:
                raise ValueError("socket_state_unavailable") from None
            target.unlink(missing_ok=True)
        else:
            raise ValueError("socket_already_listening")
    return target, group_id


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", default=DEFAULT_SOCKET_PATH)
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    parser.add_argument("--group", default="x-post-daily")
    args = parser.parse_args()
    target, group_id = prepare_socket_path(args.socket, args.group)
    previous_umask = os.umask(0o177)
    try:
        server = DispatchStateServer(str(target), lambda qid: read_dispatch_queue_state(args.db_path, qid))
    finally:
        os.umask(previous_umask)
    os.chown(target, os.geteuid(), group_id)
    os.chmod(target, 0o660)
    identity = target.stat().st_ino
    try:
        with server:
            server.serve_forever(poll_interval=0.25)
    finally:
        if target.exists() and target.lstat().st_ino == identity:
            target.unlink()


if __name__ == "__main__":
    main()
