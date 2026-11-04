"""Read safe coordinator state through a local, read-only Unix socket broker."""
from __future__ import annotations

import json
import socket
import time
from typing import Any, Dict

DEFAULT_SOCKET_PATH = "/run/x-auto-dispatch-state/state.sock"
MAX_REQUEST_BYTES = 1024
MAX_RESPONSE_BYTES = 16 * 1024
READ_TIMEOUT = 2.0
STATE_FIELDS = frozenset({"source_key", "job_id", "stage", "generation", "status", "error_code",
                         "error_message", "updated_at", "known_failure", "has_operations", "has_publication"})


class DispatchStateUnavailable(OSError):
    """No authoritative state is available; the caller must keep observing."""


def valid_queue_id(value: Any) -> bool:
    return type(value) is int and 0 < value <= 9223372036854775807


def receive_line(connection, limit: int, deadline: float) -> bytes:
    """Enforce a total deadline, including a peer that trickles partial bytes."""
    payload = bytearray()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DispatchStateUnavailable("dispatch_state_timeout")
        connection.settimeout(remaining)
        chunk = connection.recv(min(4096, limit + 1 - len(payload)))
        if not chunk:
            raise DispatchStateUnavailable("dispatch_state_incomplete")
        payload.extend(chunk)
        if len(payload) > limit:
            raise DispatchStateUnavailable("dispatch_state_too_large")
        if b"\n" in payload:
            line, _, rest = bytes(payload).partition(b"\n")
            if rest.strip():
                raise DispatchStateUnavailable("dispatch_state_multiple_messages")
            return line


def read_dispatch_state(queue_id: int, *, socket_path: str = DEFAULT_SOCKET_PATH,
                        timeout: float = READ_TIMEOUT) -> Dict[str, Any]:
    """There is deliberately no direct database fallback in the Auto process."""
    if not valid_queue_id(queue_id):
        raise DispatchStateUnavailable("dispatch_state_queue_invalid")
    deadline = time.monotonic() + timeout
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout)
            connection.connect(socket_path)
            connection.sendall(json.dumps({"queue_id": queue_id}, separators=(",", ":")).encode() + b"\n")
            raw = receive_line(connection, MAX_RESPONSE_BYTES, deadline)
        reply = json.loads(raw.decode("utf-8"))
        if not isinstance(reply, dict) or reply.get("ok") is not True or not isinstance(reply.get("state"), dict):
            raise DispatchStateUnavailable("dispatch_state_unavailable")
        state = reply["state"]
        if not state:
            return {}
        if (set(state) - STATE_FIELDS or state.get("source_key") != "x:queue:" + str(queue_id)
            or not valid_queue_id(state.get("job_id")) or type(state.get("generation")) is not int
            or state["generation"] < 0 or state.get("stage") not in {"prepare", "publish"}
            or not isinstance(state.get("status"), str)
            or any(type(state.get(key)) is not bool for key in ("known_failure", "has_operations", "has_publication"))):
            raise DispatchStateUnavailable("dispatch_state_identity_invalid")
        return state
    except (OSError, AttributeError, UnicodeError, ValueError, TypeError) as exc:
        if isinstance(exc, DispatchStateUnavailable):
            raise
        raise DispatchStateUnavailable("dispatch_state_unavailable") from None
