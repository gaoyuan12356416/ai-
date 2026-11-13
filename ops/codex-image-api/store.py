"""Durable batch queue. No image or credential bytes are stored in SQLite."""
import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager


class ApiError(Exception):
    def __init__(self, code, message, http_status=400):
        super().__init__(message)
        self.code, self.message, self.http_status = code, message, http_status


def normalize(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list) or not payload["items"]:
        raise ApiError("invalid_request", "items 必须是非空数组")
    if set(payload) - {"request_id", "items"}:
        raise ApiError("invalid_request", "请求包含未知字段")
    request_id = payload.get("request_id")
    if request_id is not None and (not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 200):
        raise ApiError("invalid_request", "request_id 必须为 1 到 200 字符的字符串")
    items = []
    for i, item in enumerate(payload["items"]):
        if not isinstance(item, dict) or set(item) != {"image_urls", "prompt"}:
            raise ApiError("invalid_request", "items[%d] 必须包含 image_urls 和 prompt" % i)
        prompt, urls = item["prompt"], item["image_urls"]
        if not isinstance(prompt, str) or not prompt.strip():
            raise ApiError("invalid_request", "items[%d].prompt 必须是非空字符串" % i)
        if not isinstance(urls, list) or not urls or any(not isinstance(u, str) or not u.strip() for u in urls):
            raise ApiError("invalid_request", "items[%d].image_urls 必须是非空字符串数组" % i)
        items.append({"prompt": prompt, "image_urls": [u.strip() for u in urls]})
    canonical = json.dumps(items, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return request_id, items, hashlib.sha256(canonical.encode()).hexdigest()


class Store:
    def __init__(self, path):
        self.path = str(path)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def initialize(self):
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY, principal TEXT NOT NULL,
                    request_id TEXT, payload_hash TEXT NOT NULL,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    UNIQUE(principal,request_id)
                );
                CREATE TABLE IF NOT EXISTS images (
                    task_id TEXT NOT NULL REFERENCES tasks(task_id), ordinal INTEGER NOT NULL,
                    item_index INTEGER NOT NULL, image_index INTEGER NOT NULL,
                    image_url TEXT NOT NULL, prompt TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'queued', stage TEXT NOT NULL DEFAULT 'queued',
                    attempt_id TEXT, cos_url TEXT, error_json TEXT, updated_at REAL NOT NULL,
                    PRIMARY KEY(task_id,ordinal)
                );
                CREATE INDEX IF NOT EXISTS images_queue ON images(state,task_id,ordinal);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, task_id TEXT NOT NULL, ordinal INTEGER,
                    event TEXT NOT NULL, detail TEXT, created_at REAL NOT NULL
                );
            """)

    def submit(self, principal, payload):
        request_id, items, digest = normalize(payload)
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if request_id is not None:
                old = db.execute("SELECT * FROM tasks WHERE principal=? AND request_id=?", (principal, request_id)).fetchone()
                if old:
                    if old["payload_hash"] != digest:
                        raise ApiError("idempotency_conflict", "request_id 已用于不同的请求内容", 409)
                    return old["task_id"], False
            task_id = "ig_" + uuid.uuid4().hex
            db.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?)", (task_id, principal, request_id, digest, now, now))
            ordinal = 0
            for i, item in enumerate(items):
                for j, url in enumerate(item["image_urls"]):
                    db.execute("INSERT INTO images(task_id,ordinal,item_index,image_index,image_url,prompt,updated_at) VALUES(?,?,?,?,?,?,?)", (task_id, ordinal, i, j, url, item["prompt"], now))
                    ordinal += 1
            db.execute("INSERT INTO events(task_id,event,created_at) VALUES(?,?,?)", (task_id, "accepted", now))
        return task_id, True

    def query(self, principal, task_id):
        with self.connect() as db:
            db.execute("BEGIN")
            task = db.execute("SELECT * FROM tasks WHERE task_id=? AND principal=?", (task_id, principal)).fetchone()
            if not task:
                raise ApiError("task_not_found", "任务不存在", 404)
            images = db.execute("SELECT * FROM images WHERE task_id=? ORDER BY ordinal", (task_id,)).fetchall()
        total = len(images)
        succeeded = sum(i["state"] == "succeeded" for i in images)
        failed = sum(i["state"] == "failed" for i in images)
        completed = succeeded + failed
        done = completed == total
        if done:
            status = "succeeded" if succeeded == total else "failed" if failed == total else "partial_failed"
        else:
            status = "queued" if all(i["state"] == "queued" for i in images) else "running"
        results = None
        if done:
            results = [{"item_index": i["item_index"], "image_index": i["image_index"], "status": i["state"], "cos_url": i["cos_url"], "error": json.loads(i["error_json"]) if i["error_json"] else None} for i in images]
        return dict(task_id=task_id, status=status, done=done, total=total, completed=completed, succeeded=succeeded, failed=failed, results=results)

    def next_image(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            # Finish or reconcile the previous claim before accepting another one.
            row = db.execute("SELECT * FROM images WHERE state='running' ORDER BY updated_at LIMIT 1").fetchone()
            if row:
                return dict(row), False
            row = db.execute("SELECT i.* FROM images i JOIN tasks t USING(task_id) WHERE i.state='queued' ORDER BY t.created_at,i.ordinal LIMIT 1").fetchone()
            if not row:
                return None, False
            attempt = uuid.uuid4().hex
            db.execute("UPDATE images SET state='running',stage='preparing',attempt_id=?,updated_at=? WHERE task_id=? AND ordinal=? AND state='queued'", (attempt, time.time(), row["task_id"], row["ordinal"]))
            result = dict(row)
            result.update(state="running", stage="preparing", attempt_id=attempt)
            return result, True

    def update(self, row, stage, state="running", cos_url=None, error=None):
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute("UPDATE images SET stage=?,state=?,cos_url=?,error_json=?,updated_at=? WHERE task_id=? AND ordinal=? AND attempt_id=? AND state='running'", (stage, state, cos_url, json.dumps(error, ensure_ascii=False) if error else None, now, row["task_id"], row["ordinal"], row["attempt_id"]))
            if cursor.rowcount != 1:
                raise RuntimeError("claim no longer belongs to this attempt")
            db.execute("UPDATE tasks SET updated_at=? WHERE task_id=?", (now, row["task_id"]))
            db.execute("INSERT INTO events(task_id,ordinal,event,detail,created_at) VALUES(?,?,?,?,?)", (row["task_id"], row["ordinal"], stage, json.dumps(error, ensure_ascii=False) if error else None, now))
