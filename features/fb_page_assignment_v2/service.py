"""Synchronous-to-caller assignment with bounded coalescing and durable idempotency."""
from __future__ import annotations

import asyncio
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import partial
import hashlib
import json
import multiprocessing
from threading import BrokenBarrierError
import time

from features.fb_page_assignment.runtime import log
from features.fb_page_assignment.service import InvalidRequest, validate


def private_error(status, code):
    return status, {"error": code, "results": []}


def normalize(payload):
    external, dramas = validate(payload)
    pool = payload.get("page_group_id")
    if isinstance(pool, str) and pool.isascii() and pool.isdecimal() and len(pool) <= 10:
        pool = int(pool)
    if type(pool) is not int or not 0 < pool < 2**32:
        raise InvalidRequest("page_group_id_required")
    content = json.dumps([pool, dramas], ensure_ascii=False, separators=(",", ":")).encode()
    return {"external_task_id": external, "page_group_id": pool, "drama_ids": dramas}, hashlib.sha256(content).hexdigest()


_selector = None


def _preload_worker(ready):
    global _selector
    from .selection import Selector
    _selector = Selector()
    try:
        _selector.preload()
    except Exception as exc:
        # A later atomic publication can repair a missing catalog without restarting.
        log("v2_worker_preload_failed", category=type(exc).__name__)
    try:
        ready.wait(timeout=60)
    except BrokenBarrierError:
        log("v2_worker_preload_delayed")


def _worker_ready():
    # The initializer has completed before work is accepted by this process.
    return True


def select_process(requests, deadline):
    global _selector
    from .selection import Selector, SelectionError
    if _selector is None:
        _selector = Selector()
    results = _selector.select_batch(requests, deadline)
    normalized = []
    for result in results:
        if isinstance(result, SelectionError):
            normalized.append((result.status, str(result.code)))
        elif isinstance(result, Exception):
            normalized.append((503, "selection_unavailable"))
        else:
            normalized.append((200, result))
    return normalized


@dataclass
class Request:
    payload: dict
    fingerprint: str
    deadline: float
    future: asyncio.Future


class Assignment:
    def __init__(self, store, *, window=.100, parallel=4, compute=None, queue_limit=2048):
        self.store = store
        self.window = window
        self.parallel = parallel
        self.queue_limit = queue_limit
        self.compute = compute
        self.pending = {}
        self.admissions = {}
        self.incoming = asyncio.Queue()
        self.active = 0
        self.closed = False
        self.groups = set()
        self.pools = None
        self.warming = None
        self.recoverable = set()
        self.notifications = set()
        self.alerts = store
        self.notice_lock = asyncio.Lock()

    async def start(self):
        self.slots = asyncio.Semaphore(self.parallel)
        if hasattr(self.store, "active_incidents"):
            self.recoverable.update(await asyncio.to_thread(self.store.active_incidents))
        if self.compute is None:
            pool = self._pool()
            try:
                await self.warming
            except BaseException:
                self._retire(pool)
                raise
        self.dispatch = asyncio.create_task(self._dispatch())

    def _pool(self):
        if self.pools is None:
            context = multiprocessing.get_context("spawn")
            pool = ProcessPoolExecutor(self.parallel, mp_context=context, initializer=_preload_worker,
                                       initargs=(context.Barrier(self.parallel),))
            self.pools = pool
            self.warming = asyncio.create_task(self._warm(pool))
        return self.pools

    async def _warm(self, pool):
        loop = asyncio.get_running_loop()
        await asyncio.gather(*(loop.run_in_executor(pool, _worker_ready) for _ in range(self.parallel)))

    def _retire(self, pool):
        if self.pools is pool:
            self.pools = self.warming = None
            pool.shutdown(wait=False, cancel_futures=True)
            return True
        return False

    async def _select(self, requests, deadline):
        if self.compute is not None:
            return await self.compute(requests, deadline)
        for attempt in range(2):
            pool = self._pool()
            try:
                await asyncio.shield(self.warming)
                if time.monotonic() >= deadline:
                    return [(503, "selection_deadline")] * len(requests)
                result = await asyncio.get_running_loop().run_in_executor(pool, select_process, requests, deadline)
                self.recover("worker_pool")
                return result
            except BrokenProcessPool:
                if self._retire(pool):
                    self.notify("worker_pool", code="process_pool_broken")
                if attempt or time.monotonic() >= deadline:
                    raise

    def busy(self):
        return bool(self.active or self.pending)

    def notify(self, kind, external="", group=None, code="", event_id=None):
        self.recoverable.add(kind)
        self._schedule_notice(self.alerts.incident, kind, external_task_id=external,
                              page_group_id=group, detail=code, event_id=event_id)

    def recover(self, *kinds):
        for kind in kinds:
            if kind in self.recoverable:
                self.recoverable.discard(kind)
                self._schedule_notice(self.alerts.recovery, kind)

    def _schedule_notice(self, function, *args, **kwargs):
        async def apply_notice():
            try:
                async with self.notice_lock:
                    await asyncio.to_thread(function, *args, **kwargs)
            except Exception as exc:
                log("v2_notification_record_failed", operation=function.__name__, category=type(exc).__name__)
        task = asyncio.create_task(apply_notice())
        self.notifications.add(task)
        task.add_done_callback(self.notifications.discard)

    async def submit(self, payload, deadline):
        try:
            request, fingerprint = normalize(payload)
        except InvalidRequest as exc:
            self.notify("contract", code=str(exc))
            return private_error(400, str(exc))
        key, group = request["external_task_id"], request["page_group_id"]
        self.active += 1
        try:
            remaining = deadline - time.monotonic()
            if remaining > 0:
                try:
                    response = await asyncio.wait_for(self._submit(request, fingerprint, deadline), remaining)
                    if time.monotonic() < deadline:
                        return response
                except asyncio.TimeoutError:
                    pass
            dedup = hashlib.sha256((key + "\0" + fingerprint).encode()).hexdigest()
            self.notify("timeout", key, group, "request_deadline", event_id="timeout-" + dedup)
            return private_error(503, "request_deadline")
        finally:
            self.active -= 1

    @asynccontextmanager
    async def _admission(self, key):
        # Serialize lookup-to-enqueue for this key; other keys can perform I/O concurrently.
        entry = self.admissions.get(key)
        if entry is None:
            entry = self.admissions[key] = [asyncio.Lock(), 0]
        entry[1] += 1
        try:
            async with entry[0]:
                yield
        finally:
            entry[1] -= 1
            if not entry[1]:
                del self.admissions[key]

    async def _submit(self, request, fingerprint, deadline):
        key, group = request["external_task_id"], request["page_group_id"]
        async with self._admission(key):
            job = self.pending.get(key)
            if job is not None:
                if job.fingerprint != fingerprint:
                    self.notify("request_conflict", key, group, "idempotency_conflict")
                    return private_error(409, "idempotency_conflict")
                job.deadline = max(job.deadline, deadline)
            else:
                try:
                    cached = await asyncio.to_thread(self.store.lookup, key, fingerprint)
                except Exception as exc:
                    self.notify("persistence", key, group, "receipt_lookup_failed")
                    log("v2_lookup_failed", category=type(exc).__name__)
                    return private_error(503, "receipt_unavailable")
                if cached is not None:
                    if cached[0] == 409:
                        self.notify("request_conflict", key, group, "idempotency_conflict")
                    return cached
                if time.monotonic() >= deadline:
                    return private_error(503, "request_deadline")
                if self.closed or len(self.pending) >= self.queue_limit:
                    self.notify("saturation", key, group, "queue_full")
                    return private_error(503, "queue_full")
                future = asyncio.get_running_loop().create_future()
                job = Request(request, fingerprint, deadline, future)
                self.pending[key] = job
                self.incoming.put_nowait(job)
        # A timed out caller cannot cancel another caller or an in-flight durable save.
        return await asyncio.shield(job.future)

    async def _dispatch(self):
        while True:
            first = await self.incoming.get()
            if first is None:
                return
            jobs = [first]
            await asyncio.sleep(self.window)
            stop = False
            while not self.incoming.empty():
                item = self.incoming.get_nowait()
                if item is None:
                    stop = True
                    break
                jobs.append(item)
            grouped = defaultdict(list)
            for job in jobs:
                grouped[job.payload["page_group_id"]].append(job)
            for same_pool in grouped.values():
                for start in range(0, len(same_pool), 64):
                    task = asyncio.create_task(self._group(same_pool[start:start+64]))
                    self.groups.add(task)
                    task.add_done_callback(self.groups.discard)
            if stop:
                return

    def _done(self, job, result):
        if not job.future.done():
            job.future.set_result(result)
        key = job.payload["external_task_id"]
        if self.pending.get(key) is job:
            self.pending.pop(key)

    async def _group(self, jobs):
        try:
            async with self.slots:
                fresh = []
                for job in jobs:
                    if job.deadline <= time.monotonic():
                        self.notify("timeout", job.payload["external_task_id"],
                                    job.payload["page_group_id"], "request_deadline")
                        self._done(job, private_error(503, "request_deadline"))
                    else:
                        fresh.append(job)
                if not fresh:
                    return
                payloads = [job.payload for job in fresh]
                deadline = max(job.deadline for job in fresh)
                answers = await self._select(payloads, deadline)
                if len(answers) != len(fresh):
                    raise RuntimeError("selection_count_mismatch")
            results = await asyncio.gather(*(self._finish(job, result, deadline)
                                             for job, result in zip(fresh, answers)),
                                           return_exceptions=True)
            for job, result in zip(fresh, results):
                if isinstance(result, Exception):
                    self._selection_failed(job, result)
        except Exception as exc:
            for job in jobs:
                self._selection_failed(job, exc)

    def _selection_failed(self, job, exc):
        if not job.future.done():
            log("v2_selection_failure", category=type(exc).__name__)
            self.notify("page_source", job.payload["external_task_id"], job.payload["page_group_id"],
                        "selection_unavailable")
            self._done(job, private_error(503, "selection_unavailable"))

    async def _finish(self, job, answer, compute_deadline):
        status, selection = answer
        key, group = job.payload["external_task_id"], job.payload["page_group_id"]
        now = time.monotonic()
        if job.deadline <= now:
            self.notify("timeout", key, group, "request_deadline")
            self._done(job, private_error(503, "request_deadline"))
            return
        if status != 200:
            if (not self.closed and status == 503 and job.deadline > compute_deadline
                    and (now >= compute_deadline or selection == "selection_deadline")):
                # A same-key retry extended the budget after the read-only worker
                # started. No save was attempted; one new calculation serves all waiters.
                self.incoming.put_nowait(job)
                return
            self.notify("pool_authorization" if status == 403 else "page_source", key, group, str(selection))
            self._done(job, private_error(status, str(selection)))
            return
        try:
            status_body = await asyncio.to_thread(self.store.save, key, job.fingerprint, group,
                                                  job.payload["drama_ids"], selection)
        except Exception as exc:
            log("v2_receipt_failure", category=type(exc).__name__)
            self.notify("persistence", key, group, "receipt_save_failed")
            self._done(job, private_error(503, "receipt_unavailable"))
            return
        if status_body[0] == 200:
            self.recover("page_source", "persistence", "timeout", "saturation")
        elif status_body[0] == 409:
            self.notify("request_conflict", key, group, "idempotency_conflict")
        self._done(job, status_body)

    async def close(self):
        self.closed = True
        self.incoming.put_nowait(None)
        await self.dispatch
        if self.groups:
            await asyncio.gather(*tuple(self.groups), return_exceptions=True)
        if self.notifications:
            await asyncio.gather(*tuple(self.notifications), return_exceptions=True)
        if self.pools:
            await asyncio.to_thread(partial(self.pools.shutdown, wait=True))
