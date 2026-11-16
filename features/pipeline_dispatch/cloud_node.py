"""Prepare-only COS node. Recovery of owned work is independent of new admission."""
import argparse
import concurrent.futures
import contextlib
import fcntl
import json
import os
import signal
import threading
import time
from pathlib import Path

from .client import CoordinatorClient, CoordinatorError
from .cloud_jobs import CloudLedger, PendingReadback, fingerprint
from .cloud_media import CloudExecutor
from .executors import ExecutionError
from .receipt_outbox import ReceiptOutbox
from .worker import ExecutionContext, LeaseLost


class CloudClient(CoordinatorClient):
    ROUTES = CoordinatorClient.ROUTES | {"jobs/cos-intent", "jobs/cos-park", "jobs/cos-resume"}


class CloudNode:
    def __init__(self, config):
        self.config = config
        state = Path(config["state_root"])
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.instance = (state / "node.lock").open("a")
        fcntl.flock(self.instance, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.ledger = CloudLedger(state / "actions.sqlite3")
        self.outbox = ReceiptOutbox(state / "receipts.sqlite3")
        self.executor = CloudExecutor(config, self.ledger)
        self.caps = self.executor.capabilities()
        credentials = json.loads(Path(config["node_config"]).read_text())
        if credentials["node_id"] != "cos-cloud":
            raise ValueError("COS enrollment required")
        self.client = CloudClient(config.get("coordinator_url", "http://127.0.0.1:18846"), credentials["node_token"], timeout=90)
        os.environ["PIPELINE_MEDIA_ALLOWED_HOSTS"] = ",".join(config["source_hosts"])
        self.stopping = threading.Event()
        self.backoff_until = 0
        self.next_register = 0
        self.target = 0
        self.futures = {}
        self.workers = concurrent.futures.ThreadPoolExecutor(max_workers=64)
        self.idle_reason = "awaiting_assignment"

    def work(self, row):
        local_id, job = row["identity"], json.loads(row["job"])
        client = self.client
        with ExecutionContext(job, client, self.outbox, "") as ctx:
            try:
                if row["receipt"]:
                    payload = json.loads(row["receipt"])
                    self.outbox.put(row["route"], payload, payload["receipt_id"])
                    try:
                        acknowledged = self.outbox.flush(client, only=payload["receipt_id"])
                    except CoordinatorError as exc:
                        if exc.code in {"claim_expired", "claim_not_running"} and row.get("paid"):
                            client.post("jobs/cos-resume", ctx.identity)
                        raise
                    if acknowledged:
                        self.ledger.forget(local_id)
                    else:
                        self.ledger.postpone(local_id, 15)
                    return
                try:
                    client.post("jobs/heartbeat", dict(ctx.identity, phase="cos_readback"))
                except CoordinatorError as exc:
                    if exc.code not in {"claim_expired", "claim_conflict"}:
                        raise
                    try:
                        resumed = client.post("jobs/cos-resume", ctx.identity)
                    except CoordinatorError as failed:
                        if failed.code in {"cos_intent_missing", "claim_conflict"} and not row.get("paid"):
                            self.ledger.forget(local_id)
                            return
                        raise
                    if resumed.get("terminal"):
                        self.ledger.forget(local_id)
                        return
                if job.get("reservation_pending"):
                    ctx.phase("selection")
                    reserved = client.post("jobs/reserve", ctx.identity)
                    if not reserved.get("job"):
                        self.ledger.forget(local_id)
                        return
                    job = ctx.job = reserved["job"]
                    self.ledger.remember(job)
                ctx.check()
                result = self.executor.execute(job, ctx)
                route, receipt = "jobs/complete", {"result":result}
            except (PendingReadback, LeaseLost):
                self.backoff_until = max(self.backoff_until, time.monotonic() + 30)
                with contextlib.suppress(CoordinatorError):
                    client.post("jobs/cos-park", ctx.identity)
                self.ledger.postpone(local_id, 30)
                return
            except CoordinatorError:
                self.ledger.postpone(local_id, 15)
                return
            except Exception as exc:
                # A platform negative result or invalid media is definitive; an
                # interrupted transfer/read must retain its original paid action.
                code = getattr(exc, "code", "")
                if (getattr(exc, "unknown", False) or not code or getattr(exc, "retryable", False)
                        or code in {"cos_session_expired","youtube_source_download_failed"}):
                    self.backoff_until = max(self.backoff_until, time.monotonic() + 30)
                    with contextlib.suppress(CoordinatorError):
                        client.post("jobs/cos-park", ctx.identity)
                    self.ledger.postpone(local_id, 30)
                    return
                route, receipt = "jobs/fail", {"error_code": str(code)[:120], "unknown_outcome":False}
            payload = dict(ctx.identity, receipt_id=fingerprint([local_id, route, receipt]), **receipt, metrics=ctx.metrics)
            self.ledger.receipt(local_id, route, payload)
            self.outbox.put(route, payload, payload["receipt_id"])
            if self.outbox.flush(client, only=payload["receipt_id"]):
                self.ledger.forget(local_id)
            else:
                self.ledger.postpone(local_id, 15)

    def tick(self):
        now = time.monotonic()
        for key, future in list(self.futures.items()):
            if future.done():
                del self.futures[key]
                if future.exception():
                    self.ledger.postpone(key, 15)
        if now >= self.next_register:
            metrics = self.executor.jobs.health()
            if now < self.backoff_until:
                metrics["queue_ready"] = False
            admitted = self.client.post("nodes/register", {"node_id":"cos-cloud","capabilities":self.caps,"metrics":metrics})
            self.target = min(metrics["queue_limit"],admitted["capacity"]["prepare"]["target"]) if metrics["queue_ready"] else 0
            self.next_register = now + 10
        for row in self.ledger.due():
            key = row["identity"]
            if key not in self.futures and len(self.futures) < 64:
                self.futures[key] = self.workers.submit(self.work, row)
        if len(self.futures) >= self.target or now < self.backoff_until:
            return
        with self.ledger.connection() as c:
            occupied = {json.loads(r[0])["worker_id"] for r in c.execute("SELECT job FROM work")}
        slot = 0
        while len(self.futures) < self.target:
            worker_id = "cos-prepare-" + str(slot)
            slot += 1
            if worker_id in occupied:
                continue
            worker = {"node_id":"cos-cloud","worker_id":worker_id,"stage":"prepare","pid":os.getpid()}
            self.client.post("workers/heartbeat", dict(worker, observation={"phase":"idle","idle_reason":self.idle_reason}))
            response = self.client.post("jobs/claim", dict(worker, reserve_separately=True))
            job = response.get("job")
            if not job:
                self.idle_reason = response.get("idle_reason","no_eligible_task")
                return
            key = self.ledger.remember(job)
            self.futures[key] = self.workers.submit(self.work, {"identity":key,"job":json.dumps(job),"receipt":""})

    def serve(self):
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: self.stopping.set())
        try:
            while not self.stopping.is_set():
                try:
                    self.tick()
                except (OSError, CoordinatorError):
                    self.next_register = 0
                self.stopping.wait(2)
        finally:
            self.workers.shutdown(wait=True)
            self.instance.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    options = parser.parse_args()
    os.umask(0o077)
    CloudNode(json.loads(Path(options.config).read_text())).serve()
