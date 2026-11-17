"""Single CPU ledger. Publishers do bounded reads only; final receipts belong to source DBs."""

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from .model import (
    ENDPOINTS,
    click_options,
    group_key,
    normalized,
    signature,
    validate,
)

ROOT = Path(os.environ.get("STORY_COPY_ROOT", "/mnt/data-disk/story-copy"))
DB = ROOT / "captions.sqlite3"
SOURCES = {
    "fa": (
        "fb",
        "/mnt/data-disk/fb-auto-post-publisher/fb-auto-post.sqlite3",
        "fb_auto_task",
    ),
    "fv": (
        "fb",
        "/mnt/data-disk/fb-auto-post-publisher/fb-auto-post.sqlite3",
        "fb_video_task",
    ),
    "xm": ("x_material", "/var/lib/x-post-automation/accounts.sqlite3", "x_post_queue"),
    "xd": ("x_drama", "/var/lib/x-post-automation/accounts.sqlite3", "x_post_queue"),
    "ta": (
        "x_tag_auto",
        "/mnt/data-disk/x-tag-publisher/auto/x-auto-post.sqlite3",
        "x_auto_task",
    ),
}
TAG_POSTS = "/mnt/data-disk/x-tag-publisher/posts.sqlite3"
SOURCES["tm"] = ("x_material", TAG_POSTS, "x_post_queue")
SCHEMA = """
CREATE TABLE IF NOT EXISTS switches(id INTEGER PRIMARY KEY,endpoint TEXT,enabled INTEGER,marks TEXT,actor TEXT,at REAL);
CREATE INDEX IF NOT EXISTS switch_endpoint ON switches(endpoint,id DESC);
CREATE TABLE IF NOT EXISTS jobs(key TEXT PRIMARY KEY,endpoint TEXT,product TEXT,content TEXT,lang TEXT,payload TEXT,
 state TEXT,batch INTEGER,draft TEXT,plot TEXT,hash TEXT,reason TEXT DEFAULT '',created REAL,final TEXT,short TEXT);
CREATE INDEX IF NOT EXISTS jobs_pending ON jobs(state,product,content,lang,created);
CREATE INDEX IF NOT EXISTS jobs_exact ON jobs(product,content,lang,hash) WHERE hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS jobs_history ON jobs(product,content,lang,created DESC);
CREATE TABLE IF NOT EXISTS history(product TEXT,content TEXT,lang TEXT,hash TEXT,plot TEXT,at REAL,
 PRIMARY KEY(product,content,lang,hash));
CREATE TABLE IF NOT EXISTS batches(id INTEGER PRIMARY KEY,created REAL,finished REAL,requested INTEGER,
 accepted INTEGER DEFAULT 0,input_tokens INTEGER,output_tokens INTEGER,calls INTEGER DEFAULT 0,reason TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS alerts(event TEXT PRIMARY KEY,incident TEXT,body TEXT,created REAL,sent REAL,attempts INTEGER DEFAULT 0,next_try REAL DEFAULT 0,receipt TEXT);
CREATE TABLE IF NOT EXISTS alert_packets(id TEXT PRIMARY KEY,incident TEXT,events TEXT,body TEXT,delivered REAL);
CREATE TABLE IF NOT EXISTS packed_alerts(event TEXT PRIMARY KEY,packet TEXT);
CREATE TABLE IF NOT EXISTS cursors(name TEXT PRIMARY KEY,value INTEGER);
CREATE TABLE IF NOT EXISTS reviewed_facts(product TEXT,content TEXT,lang TEXT,material_id TEXT,episode INTEGER,
 source_ref TEXT,summary TEXT,PRIMARY KEY(product,content,lang,material_id,episode,source_ref));
"""
RECEIPTS = """CREATE TABLE IF NOT EXISTS story_copy_receipt(
 task_key TEXT PRIMARY KEY,endpoint TEXT NOT NULL,source TEXT NOT NULL,reason TEXT NOT NULL,
 final_text TEXT NOT NULL,short_url TEXT NOT NULL,batch INTEGER,created REAL NOT NULL)"""


def dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


@contextmanager
def connection(path=None, *, readonly=False, timeout=2):
    p = Path(path or DB)
    uri = p.resolve().as_uri() + "?mode=ro" if readonly else str(p)
    conn = sqlite3.connect(uri, uri=readonly, timeout=timeout)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        if not readonly:
            conn.commit()
    finally:
        conn.close()


def initialize():
    DB.parent.mkdir(parents=True, exist_ok=True)
    with connection() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(SCHEMA)
        # Keep fingerprints for audit, but allow identical copy on different tasks.
        # Retain the index name so older code can start without recreating uniqueness.
        c.execute("BEGIN IMMEDIATE")
        if any(r["name"] == "jobs_exact" and r["unique"] for r in c.execute("PRAGMA index_list(jobs)")):
            c.execute("DROP INDEX jobs_exact")
            c.execute("CREATE INDEX jobs_exact ON jobs(product,content,lang,hash) WHERE hash IS NOT NULL")


def setting(endpoint):
    if endpoint not in ENDPOINTS:
        raise ValueError("invalid_endpoint")
    with connection(readonly=True) as c:
        row = c.execute(
            "SELECT enabled FROM switches WHERE endpoint=? ORDER BY id DESC LIMIT 1",
            (endpoint,),
        ).fetchone()
    return bool(row and row[0])


def change_setting(endpoint, enabled, actor):
    if endpoint not in ENDPOINTS or type(enabled) is not bool:
        raise ValueError("invalid_setting")
    with connection() as c:
        c.execute("BEGIN IMMEDIATE")
        old = c.execute(
            "SELECT enabled FROM switches WHERE endpoint=? ORDER BY id DESC LIMIT 1",
            (endpoint,),
        ).fetchone()
        if bool(old and old[0]) == enabled:
            return enabled
        marks = {}
        for family, (ep, path, table) in SOURCES.items():
            if ep == endpoint:
                with connection(path, readonly=True) as src:
                    marks[family] = src.execute(
                        "SELECT coalesce(max(rowid),0) FROM " + table
                    ).fetchone()[0]
        c.execute(
            "INSERT INTO switches(endpoint,enabled,marks,actor,at) VALUES(?,?,?,?,?)",
            (endpoint, enabled, dump(marks), actor, time.time()),
        )
    return enabled


def eligible(c, family, ordinal):
    ep = SOURCES[family][0]
    row = c.execute(
        "SELECT enabled FROM switches WHERE endpoint=? AND json_extract(marks,?)<? ORDER BY id DESC LIMIT 1",
        (ep, "$." + family, int(ordinal)),
    ).fetchone()
    return bool(row and row[0])


def enqueue(c, task):
    if not eligible(c, task["family"], task["ordinal"]):
        return False
    manual = task.get("manual")
    if c.execute("SELECT 1 FROM jobs WHERE key=?", (task["key"],)).fetchone():
        return True
    task = dict(task, facts=dict(task["facts"]))
    for row in c.execute(
        "SELECT * FROM reviewed_facts WHERE product=? AND content=? AND lang=? AND material_id=? ORDER BY source_ref",
        (*group_key(task), str(task.get("material_id") or "")),
    ):
        episode = int(row["episode"])
        if episode and task.get("episode") != episode:
            if task["endpoint"] == "x_drama":
                task["evidence_error"] = "episode_mismatch"
            continue
        kind = "episode" if episode else "clip"
        task["facts"][kind] = (
            task["facts"].get(kind, "") + " " + row["summary"]
        ).strip()[:3500]
        task.setdefault("evidence_sources", []).append(row["source_ref"])
    c.execute(
        "INSERT OR IGNORE INTO jobs(key,endpoint,product,content,lang,payload,state,created) VALUES(?,?,?,?,?,?,?,?)",
        (
            task["key"],
            task["endpoint"],
            *group_key(task),
            dump(task),
            "manual" if manual else "pending",
            time.time(),
        ),
    )
    return True


def claim(c):
    c.execute("BEGIN IMMEDIATE")
    row = c.execute(
        """SELECT product,content,lang FROM jobs WHERE state='pending' GROUP BY product,content,lang
        HAVING count(*)>=100 OR min(created)<=? OR max(coalesce(json_extract(payload,'$.urgent'),0))=1
        ORDER BY min(created) LIMIT 1""",
        (time.time() - 60,),
    ).fetchone()
    if not row:
        c.commit()
        return None, []
    rows = c.execute(
        "SELECT key,payload FROM jobs WHERE state='pending' AND product=? AND content=? AND lang=? ORDER BY created,key LIMIT 100",
        tuple(row),
    ).fetchall()
    batch = c.execute(
        "INSERT INTO batches(created,requested) VALUES(?,?)", (time.time(), len(rows))
    ).lastrowid
    c.executemany(
        "UPDATE jobs SET state='generating',batch=? WHERE key=?",
        [(batch, r["key"]) for r in rows],
    )
    c.commit()
    return batch, [json.loads(r["payload"]) for r in rows]


def accept(c, batch, task, item):
    options = click_options(task, item.get("hooks"))
    clean = validate(task, item, options)
    plot = normalized(clean["opening"] + clean["turn"])
    digest = signature(plot)
    c.execute("BEGIN IMMEDIATE")
    try:
        clean.update(options[0])
        changed = c.execute(
            "UPDATE jobs SET state='ready',draft=?,plot=?,hash=? WHERE key=? AND batch=? AND state='generating'",
            (dump(clean), plot, digest, task["key"], batch),
        ).rowcount
        c.commit()
        return bool(changed)
    except Exception:
        c.rollback()
        raise
