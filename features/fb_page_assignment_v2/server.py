"""Internal HTTP/1.1 endpoint for the Hong Kong publisher subnet."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import hmac
import ipaddress
import json
import os
from pathlib import Path
import signal
import time
import uuid

import h11

from features.fb_page_assignment.runtime import log
from .service import Assignment, private_error

ROUTE = b"/api/integrations/v2/drama-page-assignments"
BODY_LIMIT = 65536
SERVER_BUDGET = 47.


class Sources:
    """Reload only complete, valid inventory snapshots. Always allow the local probe."""
    def __init__(self, path, bind):
        self.path = Path(path)
        self.local = {ipaddress.ip_address(bind), ipaddress.ip_address("127.0.0.1")}
        self.current = set()
        self.version = None
        self.next_check = 0.
        self.valid_until = 0.

    def allows(self, peer):
        now = time.monotonic()
        if now >= self.next_check:
            self.next_check = now + 2
            try:
                sig = self.path.stat().st_mtime_ns
                if sig != self.version:
                    payload = json.loads(self.path.read_text(encoding="utf-8"))
                    entries = {ipaddress.ip_address(item) for item in payload["addresses"]}
                    if not isinstance(payload["addresses"], list) or any(not addr.is_private for addr in entries):
                        raise ValueError("invalid_private_sources")
                    stamp = datetime.fromisoformat(payload["generated_at"])
                    if stamp.tzinfo is None or stamp.timestamp() > time.time() + 60:
                        raise ValueError("invalid_inventory_timestamp")
                    self.current, self.version = entries, sig
                    # Cloud discovery runs every five minutes. Expire missed refreshes.
                    self.valid_until = stamp.timestamp() + 1800
            except (OSError, KeyError, ValueError, TypeError) as exc:
                log("v2_inventory_read_failed", category=type(exc).__name__)
        address = ipaddress.ip_address(peer)
        return address in self.local or (time.time() < self.valid_until and address in self.current)


async def event(reader, http, deadline):
    while True:
        value = http.next_event()
        if value is not h11.NEED_DATA:
            return value
        data = await asyncio.wait_for(reader.read(32768), max(0., deadline - time.monotonic()))
        http.receive_data(data)


async def reply(http, writer, status, body, request_id, close=False):
    data = json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    headers = [(b"Content-Type", b"application/json; charset=utf-8"),
               (b"Content-Length", str(len(data)).encode()), (b"Cache-Control", b"no-store"),
               (b"X-Request-ID", request_id.encode())]
    if close:
        headers.append((b"Connection", b"close"))
    if status == 503:
        headers.append((b"Retry-After", b"1"))
    writer.write(http.send(h11.Response(status_code=status, headers=headers)))
    writer.write(http.send(h11.Data(data=data)))
    writer.write(http.send(h11.EndOfMessage()))
    await asyncio.wait_for(writer.drain(), 2.)


class Endpoint:
    def __init__(self, service, token, sources, diagnostics=None):
        if not token or len(token) < 32:
            raise ValueError("assignment_token_required")
        self.service, self.token, self.sources = service, ("Bearer " + token).encode(), sources
        self.diagnostics = diagnostics
        self.clients = set()
        self.stopping = False

    async def serve(self, reader, writer):
        client = asyncio.current_task()
        self.clients.add(client)
        peer_info = writer.get_extra_info("peername")
        peer = peer_info[0] if peer_info else "127.0.0.1"
        http = h11.Connection(h11.SERVER, max_incomplete_event_size=16384)
        request_id = uuid.uuid4().hex
        authenticated = False
        try:
            while not self.stopping:
                authenticated = False
                item = await event(reader, http, time.monotonic()+8)
                if isinstance(item, h11.ConnectionClosed):
                    break
                if not isinstance(item, h11.Request):
                    break
                started = time.monotonic()
                request_id = uuid.uuid4().hex
                allowed = self.sources.allows(peer)
                if not allowed:
                    await reply(http, writer, 403, {"error":"source_forbidden"}, request_id, True)
                    break
                if self.diagnostics is not None and item.target == b"/qa/status" and item.method == b"GET":
                    headers = dict(item.headers)
                    if not hmac.compare_digest(headers.get(b"authorization", b""), self.token):
                        await reply(http, writer, 401, {"error":"unauthorized"}, request_id, True)
                    else:
                        await reply(http, writer, 200, await asyncio.to_thread(self.diagnostics), request_id, True)
                    break
                if item.target == b"/healthz" and item.method == b"GET":
                    await reply(http, writer, 200, {"ok":True,"version":2}, request_id, True)
                    break
                if item.target != ROUTE or item.method != b"POST":
                    await reply(http, writer, 405 if item.target == ROUTE else 404, {"error":"unknown_operation"}, request_id, True)
                    break
                headers = dict(item.headers)
                if not hmac.compare_digest(headers.get(b"authorization", b""), self.token):
                    self.service.notify("auth", code="unauthorized", event_id=request_id)
                    await reply(http, writer, 401, {"error":"unauthorized"}, request_id, True)
                    break
                self.service.recover("auth")
                authenticated = True
                raw = bytearray()
                while True:
                    part = await event(reader, http, started+10)
                    if isinstance(part, h11.EndOfMessage):
                        break
                    if not isinstance(part, h11.Data):
                        return
                    raw.extend(part.data)
                    if len(raw) > BODY_LIMIT:
                        self.service.notify("contract", code="body_too_large", event_id=request_id)
                        await reply(http, writer, 413, {"error":"body_too_large"}, request_id, True)
                        return
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeError, RecursionError):
                    self.service.notify("contract", code="invalid_json", event_id=request_id)
                    status, body = private_error(400, "invalid_json")
                else:
                    deadline = started + SERVER_BUDGET
                    try:
                        status, body = await asyncio.wait_for(self.service.submit(payload, deadline, request_id=request_id),
                                                             max(0., deadline-time.monotonic()))
                    except asyncio.TimeoutError:
                        self.service.notify("timeout", code="response_deadline", event_id=request_id)
                        status, body = private_error(503, "response_deadline")
                    if status == 200:
                        self.service.recover("contract")
                        self.service.recover("request_transport")
                        self.service.recover("request_internal")
                        self.service.recover("timeout")
                await reply(http, writer, status, body, request_id)
                log("v2_http_response", request_id=request_id, task_id=body.get("task_id"), status=status,
                    elapsed_ms=round((time.monotonic()-started)*1000,1))
                if http.our_state is h11.MUST_CLOSE or http.their_state is h11.MUST_CLOSE:
                    break
                http.start_next_cycle()
        except (h11.RemoteProtocolError, h11.LocalProtocolError, asyncio.TimeoutError, OSError) as exc:
            if authenticated:
                self.service.notify("request_transport", code=type(exc).__name__, event_id=request_id)
        except Exception as exc:
            log("v2_handler_failure", request_id=request_id, category=type(exc).__name__)
            self.service.notify("request_internal", code="handler_failure", event_id=request_id)
            try:
                await reply(http, writer, 503, {"error":"internal_error"}, request_id, True)
            except (OSError, h11.LocalProtocolError, asyncio.TimeoutError):
                pass
        finally:
            self.clients.discard(client)
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass


async def main_async(args):
    from .audit import Workers
    from .ledger import Store
    token = os.environ["FB_PAGE_ASSIGNMENT_API_TOKEN"]
    root = Path(args.data_root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    store = Store(root)
    assignment = Assignment(store)
    await assignment.start()
    background = Workers(store, busy=assignment.busy)
    assignment.alerts = background
    background.start()
    endpoint = Endpoint(assignment, token, Sources(args.sources, args.bind))
    stop = asyncio.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(signum, stop.set)
    try:
        async with await asyncio.start_server(endpoint.serve, args.bind, args.port, backlog=2048):
            log("v2_started", bind=args.bind, port=args.port)
            await stop.wait()
            endpoint.stopping = True
        if endpoint.clients:
            await asyncio.gather(*tuple(endpoint.clients), return_exceptions=True)
    finally:
        await assignment.close()
        await asyncio.to_thread(background.close)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bind", default="10.89.0.125")
    parser.add_argument("--port", type=int, default=8944)
    parser.add_argument("--data-root", default="/data/fb-page-assignment-v2/state")
    parser.add_argument("--sources", default="/data/fb-page-assignment-v2/config/publishers.json")
    asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    main()
