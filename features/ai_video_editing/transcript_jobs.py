"""Durable, owner-scoped caption reads for the language dialog.

The legacy synchronous /language/generate response remains supported. The dialog
opts into jobs so a long recognition does not hold an HTTP connection open.
"""
import json
import logging
import re
import threading
import time

from .runtime import check, ident, owner, packed
from . import language_tools

LOG = logging.getLogger(__name__)
_threads = {}
_lock = threading.Lock()


def setup(rt):
    with rt.db(True) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS transcript_jobs(
            id TEXT PRIMARY KEY,owner TEXT NOT NULL,task TEXT NOT NULL,language TEXT NOT NULL,
            status TEXT NOT NULL,lease TEXT NOT NULL DEFAULT '',expires REAL NOT NULL DEFAULT 0,
            done INTEGER NOT NULL DEFAULT 0,total INTEGER NOT NULL DEFAULT 0,
            result TEXT NOT NULL DEFAULT '{}',error TEXT NOT NULL DEFAULT '',updated REAL NOT NULL,
            UNIQUE(owner,task,language))""")


def dto(row):
    return {"id": row["id"], "status": row["status"], "done": row["done"], "total": row["total"],
            "error": row["error"], "result": json.loads(row["result"]) if row["status"] == "completed" else None}


def receipt(rt, actor, jid):
    with rt.db() as db:
        row = db.execute("SELECT * FROM transcript_jobs WHERE id=? AND owner=?", (jid, owner(actor))).fetchone()
    check(row, "transcript_missing", "识别任务不存在或没有访问权限", 404)
    return dto(row)


def submit(rt, actor, body):
    check(isinstance(body.get("task_id"), str) and re.fullmatch(r"at_[a-f0-9]{32}", body["task_id"]),
          "language_media_required", "请先选定本项目已完成的作品")
    task = rt.list_records("tasks", actor, body.get("task_id"))["item"]
    check(task["status"] == "completed", "not_ready", "请先完成制作", 409)
    lang = body.get("source_language", "auto")
    check(lang == "auto" or lang in language_tools.LANGUAGES, "language_input", "源语种无效")
    who, now = owner(actor), time.time()
    with rt.db(True) as db:
        row = db.execute("SELECT * FROM transcript_jobs WHERE owner=? AND task=? AND language=?",
                         (who, task["id"], lang)).fetchone()
        if row and row["status"] == "failed":
            # A new explicit click retries this read-only job; never replay a render/publish.
            db.execute("UPDATE transcript_jobs SET status='queued',error='',done=0,total=0,expires=0,updated=? WHERE id=?",
                       (now, row["id"]))
        elif not row:
            jid = ident("tr")
            db.execute("INSERT INTO transcript_jobs(id,owner,task,language,status,updated) VALUES(?,?,?,?,'queued',?)",
                       (jid, who, task["id"], lang, now))
        row = db.execute("SELECT * FROM transcript_jobs WHERE owner=? AND task=? AND language=?",
                         (who, task["id"], lang)).fetchone()
    return dto(row)


def tick(rt):
    # Share the existing language capacity; busy is a wait, not a failed job.
    if not language_tools.TEXT_SLOTS.acquire(blocking=False):
        return False
    try:
        with rt.db(True) as db:
            row = db.execute("SELECT * FROM transcript_jobs WHERE status='queued' OR (status='running' AND expires<?) ORDER BY updated LIMIT 1",
                             (time.time(),)).fetchone()
            if not row:
                return False
            row = dict(row)
            lease = ident("lease")
            db.execute("UPDATE transcript_jobs SET status='running',lease=?,expires=?,done=0,total=0 WHERE id=?",
                       (lease, time.time()+180, row["id"]))
        def progress(done=None, total=None):
            with rt.db(True) as db:
                db.execute("UPDATE transcript_jobs SET expires=?,updated=?,done=COALESCE(?,done),total=COALESCE(?,total) WHERE id=? AND lease=?",
                           (time.time()+180, time.time(), done, total, row["id"], lease))
        stopped = threading.Event()
        def heartbeat():
            while not stopped.wait(30):
                try:
                    progress()
                except Exception:
                    LOG.exception("Caption job heartbeat failed")
        beat = threading.Thread(target=heartbeat, daemon=True)
        beat.start()
        try:
            from .language_media import transcribe
            # Resolve the immutable completed task again under its original owner.
            tenant, uid = json.loads(row["owner"])
            task = rt.list_records("tasks", {"tenant_key": tenant, "user_id": uid}, row["task"])["item"]
            check(task["status"] == "completed", "not_ready", "成片尚未就绪", 409)
            from .output_storage import local_file
            result = {"mode": "transcribe", **transcribe(local_file(rt,task['id']), row["language"],
                                                        temp_dir=rt.root, progress=progress)}
            status, error = "completed", ""
        except Exception as exc:
            LOG.exception("Caption job failed: %s", row["id"])
            result, status = {}, "failed"
            error = getattr(exc, "message", "字幕识别失败，请稍后重试")[:500]
        finally:
            stopped.set()
            beat.join(1)
        with rt.db(True) as db:
            db.execute("UPDATE transcript_jobs SET status=?,result=?,error=?,expires=0,updated=? WHERE id=? AND lease=?",
                       (status, packed(result), error, time.time(), row["id"], lease))
        return True
    finally:
        language_tools.TEXT_SLOTS.release()


def start(rt):
    setup(rt)
    with _lock:
        if rt.db_path in _threads:
            return
        def loop():
            while True:
                try:
                    tick(rt)
                except Exception:
                    LOG.exception("Caption dispatcher delayed")
                time.sleep(2)
        thread = threading.Thread(target=loop, name="independent-captions", daemon=True)
        _threads[rt.db_path] = thread
        thread.start()
