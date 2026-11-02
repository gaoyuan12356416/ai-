"""Pull workers: one task per process, independent heartbeats, durable receipts."""
from __future__ import annotations

import contextlib
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import threading
import time
import uuid

from .client import CoordinatorClient, CoordinatorError
from .credentials import open_credentials
from .diagnostics import exception_failure_fields

CLAIM_FIELDS = ("job_id", "generation", "claim_token", "node_id", "worker_id")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class LeaseLost(RuntimeError):
    code = "pipeline_lease_lost"


class ReceiptOutbox:
    """Fsync before acknowledgements; no credentials are stored here."""
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE IF NOT EXISTS receipt_outbox (id TEXT PRIMARY KEY,route TEXT NOT NULL,payload TEXT NOT NULL,created_at REAL NOT NULL,acked_at REAL)")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    @contextlib.contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.execute("PRAGMA synchronous=FULL")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def put(self, route, payload, receipt_id=None):
        identity = receipt_id or str(payload.get("receipt_id") or payload.get("checkpoint_id") or uuid.uuid4().hex)
        encoded = canonical(payload)
        with self.connect() as conn:
            existing = conn.execute("SELECT route,payload FROM receipt_outbox WHERE id=?", (identity,)).fetchone()
            if existing and existing != (route, encoded):
                raise ValueError("receipt id reused with changed facts")
            conn.execute("INSERT OR IGNORE INTO receipt_outbox(id,route,payload,created_at) VALUES(?,?,?,?)", (identity, route, encoded, time.time()))
        return identity

    def flush(self, client, *, only=None):
        with self.connect() as conn:
            rows = conn.execute("SELECT id,route,payload FROM receipt_outbox WHERE acked_at IS NULL" + (" AND id=?" if only else "") + " ORDER BY created_at LIMIT 100", (only,) if only else ()).fetchall()
        blocked_claims = set()
        for identity, route, payload in rows:
            decoded = json.loads(payload)
            claim_key = (decoded.get("job_id"), decoded.get("generation"))
            if claim_key in blocked_claims:
                continue
            try:
                result = client.post(route, decoded)
            except CoordinatorError:
                if only:
                    raise
                blocked_claims.add(claim_key)
                continue  # Keep durable facts; another channel may still accept its receipt.
            if result.get("accepted") is False or result.get("ok") is False:
                if only:
                    return False
                blocked_claims.add(claim_key)
                continue
            with self.connect() as conn:
                conn.execute("UPDATE receipt_outbox SET acked_at=? WHERE id=? AND acked_at IS NULL", (time.time(), identity))
        with self.connect() as conn:
            remaining = conn.execute("SELECT count(*) FROM receipt_outbox WHERE acked_at IS NULL" + (" AND id=?" if only else ""), (only,) if only else ()).fetchone()[0]
        return remaining == 0


class ExecutionContext:
    def __init__(self, job, client, outbox, seal_key, *, heartbeat_seconds=15, monotonic=time.monotonic):
        self.job, self.client, self.outbox, self.seal_key = job, client, outbox, seal_key
        self.identity = {key: job.get("id") if key == "job_id" else job[key] for key in CLAIM_FIELDS}
        self.heartbeat_seconds = heartbeat_seconds
        self.monotonic = monotonic
        self.finished, self.lost = threading.Event(), threading.Event()
        self.write_started = False
        self.phase_name = "starting"
        self.phase_started = monotonic()
        self.metrics = {}
        self._thread = None
        self._operation_number = 0
        self.last_operation_id = ""
        self.rate_limit = {}

    def __enter__(self):
        self._thread = threading.Thread(target=self._heartbeat, name="pipeline-job-heartbeat", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *args):
        self.finished.set()
        if self._thread:
            self._thread.join(timeout=12)

    def _heartbeat(self):
        while not self.finished.wait(self.heartbeat_seconds):
            try:
                metrics = dict(self.metrics)
                metrics[self.phase_name + "_seconds"] = round(metrics.get(self.phase_name + "_seconds", 0) + self.monotonic() - self.phase_started, 3)
                result = self.client.post("jobs/heartbeat", {**self.identity, "phase": self.phase_name, "metrics": metrics})
                if result.get("accepted") is False or result.get("ok") is False:
                    self.lost.set()
                    return
            except CoordinatorError:
                # The current network request/render may finish. No subsequent API
                # operation can begin without contacting the authority again.
                self.lost.set()
                return

    def check(self):
        if self.lost.is_set():
            raise LeaseLost()

    def phase(self, name):
        now = self.monotonic()
        self.metrics[self.phase_name + "_seconds"] = round(self.metrics.get(self.phase_name + "_seconds", 0) + now - self.phase_started, 3)
        self.phase_name, self.phase_started = str(name), now

    def checkpoint(self, phase, receipt):
        # Exact upstream receipts must still be recorded after lease loss.
        checkpoint_id = hashlib.sha256(canonical({"job_id": self.identity["job_id"], "generation": self.identity["generation"], "phase": phase, "receipt": receipt}).encode()).hexdigest()
        payload = {**self.identity, "checkpoint_id": checkpoint_id, "phase": phase, "receipt": receipt}
        self.outbox.put("jobs/checkpoint", payload, checkpoint_id)
        if not self.outbox.flush(self.client, only=checkpoint_id):
            raise CoordinatorError("checkpoint_not_accepted", 409)

    @contextlib.contextmanager
    def operation(self, operation, request_payload, *, account_resource=None, write=True):
        self.check()
        self._operation_number += 1
        operation_id = "%s-%s-%s-%s" % (self.identity["job_id"], self.identity["generation"], self._operation_number, uuid.uuid4().hex[:12])
        self.last_operation_id = operation_id
        request_sha = hashlib.sha256(canonical(request_payload).encode()).hexdigest()
        resource = account_resource or self.job.get("payload", {}).get("account_resource")
        if not resource:
            raise ValueError("operation account resource missing")
        binding = {"job_id": self.identity["job_id"], "generation": self.identity["generation"], "node_id": self.identity["node_id"], "account_resource": resource, "operation_id": operation_id, "request_sha256": request_sha}
        response = self.client.post("jobs/begin-operation", {**self.identity, "operation_id": operation_id, "operation": operation, "request_sha256": request_sha, "account_resource": resource, "write": bool(write), "details": request_payload})
        if response.get("credentials_exhausted"):
            raise CoordinatorError("credentials_exhausted", 409)
        if response.get("accepted") is False or not response.get("credential_envelope"):
            raise CoordinatorError("operation_not_accepted", 409)
        self.write_started = self.write_started or bool(write)
        try:
            with open_credentials(response["credential_envelope"], self.seal_key, binding) as credentials:
                yield credentials
        except Exception as exc:
            details = getattr(exc, "details", {}) or {}
            if getattr(exc, "status", 0) == 429 or details.get("upstream_http_status") == 429:
                self.rate_limit = {"rate_limit_scope": "account", "account_resource": resource}
                if isinstance(getattr(exc, "retry_after_seconds", None), (int, float)):
                    self.rate_limit["retry_after_seconds"] = max(1, min(86400, int(exc.retry_after_seconds)))
            raise


def execute_claim(job, client, outbox, seal_key, executor):
    """No job-level automatic retry: the coordinator owns subsequent disposition."""
    context = ExecutionContext(job, client, outbox, seal_key)
    receipt_id = uuid.uuid4().hex
    with context:
        try:
            result = executor.execute(job, context)
            if context.rate_limit:
                result = {**result, **context.rate_limit}
            context.phase("completed")
            payload = {**context.identity, "receipt_id": receipt_id, "result": result, "metrics": context.metrics}
            route = "jobs/complete"
        except Exception as exc:
            code = str(getattr(exc, "code", "pipeline_execution_failed"))[:120]
            unknown = bool(getattr(exc, "unknown_outcome", getattr(exc, "unknown", context.write_started)))
            context.phase("failed")
            payload = {**context.identity, "receipt_id": receipt_id, "error_code": code, "unknown_outcome": unknown, "metrics": context.metrics, **context.rate_limit, **exception_failure_fields(exc)}
            route = "jobs/fail"
        outbox.put(route, payload, receipt_id)
        try:
            outbox.flush(client, only=receipt_id)
        except CoordinatorError:
            pass  # The next loop flushes exactly this receipt before claiming more.
    return payload


def process_worker(config, stage, index):
    from .executors import NodeExecutors
    stopping = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopping.set())
    worker_id = "%s-%s-%s-%s" % (config["node_id"], stage, index, uuid.uuid4().hex[:8])
    client = CoordinatorClient(config["coordinator_url"], config["node_token"])
    outbox = ReceiptOutbox(Path(config["state_root"]) / "receipts.sqlite3")
    executor = NodeExecutors(config)
    identity = {"node_id": config["node_id"], "worker_id": worker_id, "stage": stage, "pid": os.getpid()}
    idle_reason = "awaiting_assignment"
    stage_available = any(name in executor.capabilities(stage) for name in (executor.PREPARE if stage == "prepare" else executor.PUBLISH))
    while not stopping.is_set():
        try:
            pending_receipts = not outbox.flush(client)
            state = client.post("workers/heartbeat", {**identity, "status": "idle", "observation": {"idle_reason": idle_reason if stage_available else "tools_unavailable", "phase": "idle", "tool_status": "ready" if stage_available else "unconfigured"}})
            if not stage_available:
                stopping.wait(15)
                continue
            if state.get("drain") or state.get("enabled") is False:
                stopping.wait(5)
                continue
            response = client.post("jobs/claim", {**identity, "capabilities": executor.capabilities(stage)})
            job = response.get("job")
            if job:
                idle_reason = "awaiting_assignment"
                job = {**job, "node_id": config["node_id"], "worker_id": worker_id}
                execute_claim(job, client, outbox, config["seal_key"], executor)
            else:
                idle_reason = str(response.get("idle_reason") or ("receipt_reconciliation_pending" if pending_receipts else "no_eligible_task"))[:80]
                stopping.wait(2)
        except (CoordinatorError, OSError):
            idle_reason = "coordinator_unavailable"
            stopping.wait(5)
    try:
        client.post("workers/drain", identity)
    except CoordinatorError:
        pass


def configuration(environ=None):
    env = os.environ if environ is None else environ
    saved = {}
    if env.get("PIPELINE_NODE_CONFIG"):
        saved = json.loads(Path(env["PIPELINE_NODE_CONFIG"]).read_text(encoding="utf-8"))
    node_id = str(env.get("PIPELINE_NODE_ID") or saved.get("node_id") or "").strip()
    root = str(env.get("PIPELINE_NODE_STATE_ROOT", "")).strip()
    if not node_id or not Path(root).is_absolute():
        raise ValueError("node id and absolute state root are required")
    backend = str(env.get("PIPELINE_RENDER_BACKEND", "cpu"))
    if backend not in {"cpu", "gpu"}:
        raise ValueError("invalid renderer backend")
    return {"node_id": node_id, "state_root": root, "backend": backend,
            "coordinator_url": env.get("PIPELINE_COORDINATOR_URL", "http://127.0.0.1:18846"),
            "node_token": env.get("PIPELINE_NODE_TOKEN") or saved.get("node_token", ""), "seal_key": env.get("PIPELINE_CREDENTIAL_SEAL_KEY") or saved.get("seal_key", ""),
            "prepare_max": max(1, min(int(env.get("PIPELINE_PREPARE_MAX", "4" if backend == "cpu" else "2")), 64)),
            "publish_max": max(1, min(int(env.get("PIPELINE_PUBLISH_MAX", "4")), 64)),
            "prepare_initial": 2 if backend == "cpu" else 1, "publish_initial": 2}


def hardware_inventory():
    inventory = {"cpu_cores": os.cpu_count() or 1, "ram_bytes": 0, "gpus": [], "encoders": []}
    try:
        meminfo = Path("/proc/meminfo").read_text()
        inventory["ram_bytes"] = int(next(line.split()[1] for line in meminfo.splitlines() if line.startswith("MemTotal:"))) * 1024
    except (OSError, StopIteration, ValueError):
        pass
    try:
        result = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5, check=True)
        for line in result.stdout.splitlines():
            name, memory = line.rsplit(",", 1)
            inventory["gpus"].append({"name": name.strip(), "memory_bytes": int(memory.strip()) * 1024**2})
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    try:
        result = subprocess.run([os.environ.get("PIPELINE_FFMPEG_BIN", "ffmpeg"), "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=10, check=True)
        for encoder in ("libx264", "h264_nvenc", "hevc_nvenc"):
            if any(encoder in line.split() for line in result.stdout.splitlines()):
                inventory["encoders"].append(encoder)
    except (OSError, subprocess.SubprocessError):
        pass
    inventory.update(cpu_count=inventory["cpu_cores"], gpu_count=len(inventory["gpus"]))
    return inventory


class ResourceSampler:
    def __init__(self):
        self.previous_cpu = None

    def sample(self):
        result = {}
        try:
            values = [int(x) for x in Path("/proc/stat").read_text().splitlines()[0].split()[1:]]
            total, idle = sum(values[:8]), values[3] + values[4]
            if self.previous_cpu and total > self.previous_cpu[0]:
                result["cpu_percent"] = round(100 * (1 - (idle - self.previous_cpu[1]) / (total - self.previous_cpu[0])), 2)
            self.previous_cpu = (total, idle)
            memory = {line.split(":")[0]: int(line.split()[1]) for line in Path("/proc/meminfo").read_text().splitlines() if line.startswith(("MemTotal:", "MemAvailable:"))}
            result["memory_available_fraction"] = memory["MemAvailable"] / memory["MemTotal"]
            result["memory_total_bytes"] = memory["MemTotal"] * 1024
            result["memory_used_bytes"] = (memory["MemTotal"] - memory["MemAvailable"]) * 1024
        except (OSError, ValueError, KeyError, IndexError):
            pass
        try:
            output = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu,utilization.encoder,memory.free,memory.total", "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5, check=True)
            gpus = [[float(value.strip()) for value in line.split(",")] for line in output.stdout.splitlines()]
            if gpus:
                result.update(gpu_percent=max(row[0] for row in gpus), encoder_percent=max(row[1] for row in gpus), gpu_memory_available_fraction=min(row[2] / row[3] for row in gpus))
                result.update(gpu_memory_total_bytes=int(sum(row[3] for row in gpus) * 1024**2), gpu_memory_used_bytes=int(sum(row[3] - row[2] for row in gpus) * 1024**2))
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        return result


def enroll_node(destination, environ=None):
    """One-use enrollment token is read from environment/private file, never argv."""
    env = os.environ if environ is None else environ
    token = str(env.get("PIPELINE_ENROLLMENT_TOKEN") or "")
    if env.get("PIPELINE_ENROLLMENT_TOKEN_FILE"):
        token = Path(env["PIPELINE_ENROLLMENT_TOKEN_FILE"]).read_text().strip()
    node_id = str(env.get("PIPELINE_NODE_ID") or "").strip()
    if not node_id:
        raise ValueError("PIPELINE_NODE_ID is required")
    target = Path(destination)
    if not target.is_absolute() or target.exists():
        raise ValueError("enrollment destination must be a new absolute private file")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    client = CoordinatorClient(env.get("PIPELINE_COORDINATOR_URL", "http://127.0.0.1:18846"), token)
    result = client.post("nodes/enroll", {"node_id": node_id, "inventory": hardware_inventory()})
    credential = {"node_id": node_id, "node_token": result.get("node_token") or result.get("bearer_token"), "seal_key": result.get("seal_key") or result.get("encryption_key")}
    if not credential["node_token"] or not credential["seal_key"]:
        raise CoordinatorError("enrollment_response_invalid", 502)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(credential, handle, separators=(",", ":"))
        handle.flush()
        os.fsync(handle.fileno())
    return {"node_id": node_id, "saved": True}


def run_node(config=None):
    """OS processes track the authority's active target; shrink drains claims."""
    config = configuration() if config is None else config
    client = CoordinatorClient(config["coordinator_url"], config["node_token"])
    from .executors import NodeExecutors
    executor = NodeExecutors(config)
    inventory = {**hardware_inventory(), "backend": config["backend"], "hostname": os.uname().nodename if hasattr(os, "uname") else config["node_id"]}
    sampler = ResourceSampler()
    registration = {"node_id": config["node_id"], "inventory": inventory, "prepare_initial": config["prepare_initial"], "prepare_max": config["prepare_max"], "publish_initial": config["publish_initial"], "publish_max": config["publish_max"], "capabilities": executor.capabilities()}
    admitted = client.post("nodes/register", {**registration, "metrics": {**inventory, **sampler.sample()}})
    desired = {stage: min(64, max(0, int(admitted.get("capacity", {}).get(stage, {}).get("target", 0)))) for stage in ("prepare", "publish")}
    sampled_at = time.monotonic()
    stopping = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopping.set())
    context = multiprocessing.get_context("spawn")
    children = []
    for stage in ("prepare", "publish"):
        for index in range(desired[stage]):
            process = context.Process(target=process_worker, args=(config, stage, index), name="pipeline-%s-%s" % (stage, index))
            process.start()
            children.append((stage, index, process))
    while not stopping.wait(5):
        if time.monotonic() - sampled_at >= 15:
            try:
                admitted = client.post("nodes/register", {**registration, "metrics": {**inventory, **sampler.sample()}})
                desired = {stage: min(64, max(0, int(admitted.get("capacity", {}).get(stage, {}).get("target", desired[stage])))) for stage in ("prepare", "publish")}
            except CoordinatorError:
                pass
            sampled_at = time.monotonic()
        for position, (stage, index, process) in enumerate(children):
            if index >= desired[stage]:
                if process.is_alive():
                    os.kill(process.pid, signal.SIGTERM)
                else:
                    process.join()
                continue
            if not process.is_alive():
                process.join()
                replacement = context.Process(target=process_worker, args=(config, stage, index), name="pipeline-%s-%s" % (stage, index))
                replacement.start()
                children[position] = (stage, index, replacement)
        for stage in ("prepare", "publish"):
            indices = {index for child_stage, index, _ in children if child_stage == stage}
            for index in range(desired[stage]):
                if index not in indices:
                    process = context.Process(target=process_worker, args=(config, stage, index), name="pipeline-%s-%s" % (stage, index))
                    process.start()
                    children.append((stage, index, process))
    # SIGTERM is a drain request in workers, never an immediate termination.
    for _, _, process in children:
        if process.is_alive():
            os.kill(process.pid, signal.SIGTERM)
    for _, _, process in children:
        process.join()

