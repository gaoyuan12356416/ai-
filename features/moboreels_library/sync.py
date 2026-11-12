"""Durable, resumable Moboreels catalog synchronization."""
import concurrent.futures
from contextlib import contextmanager
import datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid

from .catalog import equal_business, merge_drama


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    if hasattr(os, "O_DIRECTORY"):
        descriptor = os.open(str(path.parent), os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class SyncError(RuntimeError):
    pass


@contextmanager
def _process_lock(path):
    stream = open(path, "a+b")
    acquired = False
    try:
        try:
            import fcntl
        except ImportError:
            import msvcrt
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                acquired = True
            except OSError:
                pass
        else:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                pass
        yield acquired
    finally:
        if acquired:
            try:
                if "fcntl" in locals():
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                else:
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        stream.close()


class SyncManager:
    def __init__(self, settings, db, provider, recover=True):
        self.settings, self.db, self.provider = settings, db, provider
        self.root = Path(settings.data_dir)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.snapshots = self.root / "sync-runs"
        self.snapshots.mkdir(exist_ok=True, mode=0o700)
        self.db_path = self.root / "sync.sqlite3"
        self._launch_lock = threading.Lock()
        self._thread = None
        with self._transaction() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS sync_run (run_id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        if recover:
            with self._transaction() as connection:
                jobs = self._jobs(connection)
            active = next((j for j in jobs if j["status"] in ("queued", "running")), None)
            if active:
                self._launch(active["run_id"])

    @contextmanager
    def _transaction(self):
        connection = sqlite3.connect(str(self.db_path), timeout=15)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _jobs(connection):
        rows = connection.execute("SELECT payload FROM sync_run ORDER BY rowid DESC").fetchall()
        return [json.loads(row[0]) for row in rows]

    def _update(self, run_id, **changes):
        with self._transaction() as connection:
            row = connection.execute("SELECT payload FROM sync_run WHERE run_id=?", (run_id,)).fetchone()
            if not row:
                raise SyncError("同步任务不存在")
            job = json.loads(row[0])
            job.update(changes)
            job["updated_at"] = utc_now()
            connection.execute("UPDATE sync_run SET payload=? WHERE run_id=?", (json.dumps(job, ensure_ascii=False), run_id))
        return job

    def get(self, run_id):
        with self._transaction() as connection:
            row = connection.execute("SELECT payload FROM sync_run WHERE run_id=?", (str(run_id),)).fetchone()
        return json.loads(row[0]) if row else None

    def latest(self):
        with self._transaction() as connection:
            row = connection.execute("SELECT payload FROM sync_run ORDER BY rowid DESC LIMIT 1").fetchone()
        return json.loads(row[0]) if row else None

    def last_success_at(self):
        with self._transaction() as connection:
            jobs = self._jobs(connection)
        return next((job.get("finished_at") for job in jobs if job["status"] == "succeeded"), None)

    def start(self, trigger="manual", actor=None):
        if trigger not in ("manual", "scheduled"):
            raise ValueError("同步触发类型无效")
        actor = actor or {}
        safe_actor = {key: str(actor.get(key, "")) for key in ("tenant_key", "user_id", "name")}
        with self._transaction() as connection:
            jobs = self._jobs(connection)
            job = next((j for j in jobs if j["status"] in ("queued", "running")), None)
            if not job:
                previous = jobs[0] if jobs else None
                if previous and previous["status"] == "failed" and previous.get("stage") in ("writing", "verifying") and previous.get("resumable", True) and (self.snapshots / previous["run_id"] / "source-complete.json").exists():
                    job = dict(previous)
                    job.update(status="queued", error=None, finished_at=None,
                               resume_count=int(job.get("resume_count", 0)) + 1,
                               resumed_at=utc_now(), resume_trigger=trigger, resume_actor=safe_actor)
                    connection.execute("UPDATE sync_run SET payload=? WHERE run_id=?", (json.dumps(job, ensure_ascii=False), job["run_id"]))
                else:
                    run_id = uuid.uuid4().hex
                    job = {"id": run_id, "run_id": run_id, "status": "queued", "stage": "fetching",
                           "trigger": trigger, "actor": safe_actor, "created_at": utc_now(),
                           "started_at": None, "finished_at": None, "updated_at": utc_now(),
                           "total": 0, "fetched_pages": 0, "total_pages": 0, "processed": 0,
                           "verified": 0, "new_count": 0, "updated_count": 0, "listed_count": 0,
                           "delisted_count": 0, "resume_count": 0, "error": None}
                    connection.execute("INSERT INTO sync_run(run_id,payload) VALUES(?,?)", (run_id, json.dumps(job, ensure_ascii=False)))
        self._launch(job["run_id"])
        return job

    def _launch(self, run_id):
        with self._launch_lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._worker_loop, args=(run_id,), name="moboreels-sync", daemon=True)
            self._thread.start()

    def _worker_loop(self, run_id):
        while True:
            executed = self._run(run_id)
            # A new request can arrive after the previous job reaches a terminal state
            # but before its thread exits. Drain that queued job instead of stranding it.
            with self._launch_lock:
                with self._transaction() as connection:
                    active = next((j for j in self._jobs(connection) if j["status"] in ("queued", "running")), None)
                if executed and active and (active["run_id"] != run_id or active["status"] == "queued"):
                    run_id = active["run_id"]
                else:
                    self._thread = None
                    return

    def wait(self, run_id, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self.get(run_id)
            if job and job["status"] in ("succeeded", "failed"):
                thread = self._thread
                if thread and thread is not threading.current_thread():
                    thread.join(max(0, deadline - time.monotonic()))
                return job
            time.sleep(0.02)
        return self.get(run_id)

    def _run(self, run_id):
        with _process_lock(self.root / "sync.lock") as acquired:
            if not acquired:
                return False
            job = self.get(run_id)
            if not job or job["status"] not in ("queued", "running"):
                return False
            resume_count = int(job.get("resume_count", 0)) + (1 if job["status"] == "running" else 0)
            self._update(run_id, status="running", started_at=job.get("started_at") or utc_now(),
                         finished_at=None, error=None, resume_count=resume_count)
            directory = self.snapshots / run_id
            directory.mkdir(exist_ok=True, mode=0o700)
            try:
                if (directory / "plan.json").exists():
                    plan = json.loads((directory / "plan.json").read_text(encoding="utf-8"))
                else:
                    source = self._collect(run_id, directory)
                    self._update(run_id, stage="writing")
                    plan = self._plan(run_id, source)
                    _save_json(directory / "plan.json", plan)
                self._apply(run_id, plan)
                self._verify(run_id, plan)
                self._update(run_id, status="succeeded", stage="completed", error=None,
                             finished_at=utc_now(), processed=len(plan["records"]), verified=len(plan["records"]))
            except Exception as exc:
                stage = (self.get(run_id) or {}).get("stage", "fetching")
                error = str(exc) if isinstance(exc, SyncError) else "同步%s阶段失败（%s），可点击立即更新重试" % (stage, type(exc).__name__)
                resumable = (directory / "source-complete.json").exists() and not isinstance(exc, ValueError)
                self._update(run_id, status="failed", error=error, finished_at=utc_now(), resumable=resumable)
            finally:
                self.db.close_writer()
            return True

    @staticmethod
    def _valid_page(data):
        if not isinstance(data, dict) or isinstance(data.get("total"), bool):
            raise SyncError("来源分页格式异常，未更新剧库")
        try:
            total = int(data["total"])
        except (KeyError, ValueError, TypeError):
            raise SyncError("来源分页总数无效，未更新剧库")
        if total < 0 or not isinstance(data.get("list"), list):
            raise SyncError("来源分页内容无效，未更新剧库")
        return total

    def _collect(self, run_id, directory):
        self._update(run_id, stage="fetching")
        complete_path = directory / "source-complete.json"

        def page(number):
            path = directory / ("page-%05d.json" % number)
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
            else:
                data = self.provider.get_page(number, page_size=100)
                self._valid_page(data)
                _save_json(path, data)
            return number, data

        _, first = page(1)
        total = self._valid_page(first)
        page_count = max(1, (total + 99) // 100)
        results = {1: first}
        self._update(run_id, total=total, total_pages=page_count, fetched_pages=1)
        # A complete frozen snapshot is always reused; never silently re-fetch it during recovery.
        if complete_path.exists():
            for number in range(2, page_count + 1):
                path = directory / ("page-%05d.json" % number)
                if not path.exists():
                    raise SyncError("同步快照缺少分页，未更新剧库")
                results[number] = json.loads(path.read_text(encoding="utf-8"))
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
                futures = [executor.submit(page, number) for number in range(2, page_count + 1)]
                for future in concurrent.futures.as_completed(futures):
                    number, data = future.result()
                    results[number] = data
                    self._update(run_id, fetched_pages=len(results))
        source = []
        for number in range(1, page_count + 1):
            data = results[number]
            if self._valid_page(data) != total:
                raise SyncError("来源目录在取数期间发生变化，本轮未更新剧库，请重新同步")
            expected = min(100, max(0, total - (number - 1) * 100))
            if len(data["list"]) != expected:
                raise SyncError("来源分页记录不完整，本轮未更新剧库")
            source.extend(data["list"])
        identities = []
        for row in source:
            if not isinstance(row, dict) or not str(row.get("story_id", "")).strip():
                raise SyncError("来源存在无效剧目ID，本轮未更新剧库")
            identities.append(str(row["story_id"]))
        if len(source) != total or len(set(identities)) != total:
            raise SyncError("来源存在重复或缺失剧目，本轮未更新剧库")
        digest = hashlib.sha256(json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        if complete_path.exists():
            manifest = json.loads(complete_path.read_text(encoding="utf-8"))
            if manifest.get("sha256") != digest:
                raise SyncError("已保存的同步快照校验失败，未更新剧库")
        else:
            _save_json(complete_path, {"total": total, "pages": page_count, "sha256": digest, "completed_at": utc_now()})
        self._update(run_id, fetched_pages=page_count, source_complete=True)
        return source

    def _plan(self, run_id, source):
        # Build every target row before issuing any writes, so schema errors cannot partially import.
        existing = {str(row["story_id"]): row for row in self.db.iter_catalog_rows()}
        records = []
        counts = dict(new_count=0, updated_count=0, listed_count=0, delisted_count=0)
        for raw in source:
            old = existing.get(str(raw["story_id"]))
            desired = merge_drama(old, raw)
            new = old is None
            changed = new or not equal_business(old, desired)
            kind = "new" if new else "updated" if changed else "unchanged"
            records.append({"kind": kind, "record": desired})
            counts["new_count"] += int(new)
            counts["updated_count"] += int(changed and not new)
            if old:
                counts["listed_count"] += int(old.get("is_show") != 1 and desired["is_show"] == 1)
                counts["delisted_count"] += int(old.get("is_show") != 0 and desired["is_show"] == 0)
        self._update(run_id, **counts)
        return {"run_id": run_id, "seen_at": utc_now(), "records": records, "counts": counts}

    @staticmethod
    def _matches(row, desired, run_id):
        return bool(row and equal_business(row, desired) and row.get("last_sync_run_id") == run_id)

    def _reconcile_write(self, desired, run_id, seen_at, touch=False):
        """After an unknown write, inspect the replica before any repeat SQL."""
        pending = list(desired)
        for attempt in range(3):
            if not pending:
                return
            try:
                if touch:
                    self.db.touch_catalog([r["story_id"] for r in pending], run_id, seen_at)
                else:
                    self.db.apply_catalog_batch(pending, run_id, seen_at)
                return
            except Exception:
                self.db.close_writer()
                previous = None
                # Wait for a stable replica observation; committed rows are never replayed.
                for check in range(5):
                    observed = self.db.read_catalog_ids([r["story_id"] for r in pending])
                    missing = [r for r in pending if not self._matches(observed.get(r["story_id"]), r, run_id)]
                    signature = tuple(r["story_id"] for r in missing)
                    if not missing:
                        return
                    if previous == signature:
                        pending = missing
                        break
                    previous = signature
                    time.sleep(2)
                else:
                    raise SyncError("数据库写入结果仍待核对；已保留快照，可稍后继续同步")
                if attempt == 2:
                    raise SyncError("数据库写入多次失败；已保留已完成进度，可稍后继续同步")
                time.sleep(2)

    def _apply(self, run_id, plan):
        self._update(run_id, stage="writing", processed=0, **plan["counts"])
        processed = 0
        records = plan["records"]
        for offset in range(0, len(records), 500):
            chunk = records[offset:offset + 500]
            current = self.db.read_catalog_ids([r["record"]["story_id"] for r in chunk])
            changed, unchanged = [], []
            for item in chunk:
                desired = item["record"]
                old = current.get(desired["story_id"])
                if self._matches(old, desired, run_id):
                    processed += 1
                elif old and equal_business(old, desired):
                    unchanged.append(desired)
                else:
                    changed.append(desired)
            for start in range(0, len(changed), 20):
                batch = changed[start:start + 20]
                self._reconcile_write(batch, run_id, plan["seen_at"])
                processed += len(batch)
                self._update(run_id, processed=processed)
            if unchanged:
                self._reconcile_write(unchanged, run_id, plan["seen_at"], touch=True)
                processed += len(unchanged)
            self._update(run_id, processed=processed)
            self.db.close_writer()

    def _verify(self, run_id, plan):
        self._update(run_id, stage="verifying", verified=0)
        verified = 0
        records = [r["record"] for r in plan["records"]]
        for offset in range(0, len(records), 500):
            chunk = records[offset:offset + 500]
            for attempt in range(5):
                observed = self.db.read_catalog_ids([r["story_id"] for r in chunk])
                if all(self._matches(observed.get(r["story_id"]), r, run_id) for r in chunk):
                    break
                if attempt == 4:
                    raise SyncError("写入后的数据读回核验未通过；已保留快照，可继续核对补齐")
                time.sleep(2)
            verified += len(chunk)
            self._update(run_id, verified=verified)
