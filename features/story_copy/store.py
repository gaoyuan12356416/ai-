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
    generation_group_key,
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
RETRY_DELAY_SECONDS = 60
NEAR_PUBLISH_SECONDS = 300
FB_LOOKAHEAD = 8
COLLECT_SECONDS = 60
SCHEDULED_SINGLETON_WAIT_SECONDS = 300
X_AUTO_MAX_COLLECTION_SECONDS = 10 * 60
X_AUTO_GENERATION_LEAD_SECONDS = 15 * 60
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
CREATE TABLE IF NOT EXISTS retries(key TEXT PRIMARY KEY,first_batch INTEGER NOT NULL,first_reason TEXT NOT NULL,
 created REAL NOT NULL,retry_after REAL NOT NULL,retry_batch INTEGER,finished REAL,state TEXT NOT NULL,reason TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS alerts(event TEXT PRIMARY KEY,incident TEXT,body TEXT,created REAL,sent REAL,attempts INTEGER DEFAULT 0,next_try REAL DEFAULT 0,receipt TEXT);
CREATE TABLE IF NOT EXISTS alert_packets(id TEXT PRIMARY KEY,incident TEXT,events TEXT,body TEXT,delivered REAL);
CREATE TABLE IF NOT EXISTS packed_alerts(event TEXT PRIMARY KEY,packet TEXT);
CREATE TABLE IF NOT EXISTS alert_resolutions(event TEXT PRIMARY KEY,reason TEXT NOT NULL,at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS provider_backoff(id INTEGER PRIMARY KEY CHECK(id=1),until_at REAL NOT NULL,
 delay INTEGER NOT NULL,probe_batch INTEGER);
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
    existing = c.execute(
        """SELECT state,reason,product,lang,
        (coalesce(json_extract(payload,'$.title'),'')<>'' AND
         coalesce(json_extract(payload,'$.facts.series'),'')<>'') AS has_story,
        json_extract(payload,'$.publish_at') AS publish_at,
        coalesce(json_extract(payload,'$.urgent'),0) AS urgent,
        coalesce(json_extract(payload,'$.publish_priority'),0) AS priority FROM jobs WHERE key=?""",
        (task["key"],),
    ).fetchone()
    if existing:
        if (task["family"] == "fa" and task.get("title") and task["facts"].get("series")
                and all(group_key(task)) and not (existing["product"] and existing["lang"] and existing["has_story"])
                and (existing["state"] in ("pending", "retry_wait")
                     or (existing["state"], existing["reason"]) == ("failed", "insufficient_evidence"))):
            restore_fb_facts(c, task)
        # An intact snapshot and all accepted or finalized drafts stay frozen.
        priority = task.get("publish_priority", 0)
        due = task.get("publish_at")
        urgent = int(bool(task.get("urgent")))
        if existing["state"] in ("pending", "retry_wait") and (
            existing["publish_at"], existing["priority"], existing["urgent"]
        ) != (due, priority, urgent):
            c.execute(
                """UPDATE jobs SET payload=json_set(payload,'$.publish_at',?,'$.publish_priority',?,'$.urgent',json(?))
                WHERE key=? AND state IN ('pending','retry_wait')""",
                (due, priority, "true" if urgent else "false", task["key"]),
            )
        return True
    task = dict(task, facts=dict(task["facts"]))
    for row in c.execute(
        "SELECT * FROM reviewed_facts WHERE product=? AND content=? AND lang=? AND material_id=? ORDER BY source_ref",
        (*generation_group_key(task), str(task.get("material_id") or "")),
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
            *generation_group_key(task),
            dump(task),
            "manual" if manual else "pending",
            time.time(),
        ),
    )
    return True


def restore_fb_facts(c, task):
    row = c.execute(
        "SELECT payload FROM jobs WHERE key=? AND draft IS NULL AND final IS NULL", (task["key"],)
    ).fetchone()
    if not row:
        return
    frozen = json.loads(row["payload"])
    if not task.get("material_id") or any(
        frozen.get(k) != task.get(k) for k in ("family", "content", "material_id", "template_version", "publish_batch")
    ):
        return
    for field in ("product", "language", "title", "clip"):
        if frozen.get(field) and task.get(field) and frozen[field] != task[field]:
            return
        if task.get(field) and not frozen.get(field):
            frozen[field] = task[field]
    series = task["facts"]["series"]
    if frozen["facts"].get("series") and frozen["facts"]["series"] != series:
        return
    frozen["facts"]["series"] = series
    # A corrected input can generate once again. Keep the original failed
    # batches and retry history; repeated discovery sees a complete snapshot.
    c.execute("""UPDATE jobs SET product=?,lang=?,payload=?,
        batch=CASE WHEN state='failed' THEN NULL ELSE batch END,
        reason=CASE WHEN state='failed' THEN '' ELSE reason END,
        state=CASE WHEN state='failed' THEN 'pending' ELSE state END
        WHERE key=? AND payload=? AND final IS NULL AND draft IS NULL
        AND (state IN ('pending','retry_wait') OR (state='failed' AND reason='insufficient_evidence'))""",
        (frozen["product"], frozen["language"], dump(frozen), task["key"], row["payload"]))


def normal_claim_rows(c, now, prefer_imminent, reserved):
    priority = """CASE WHEN json_extract(payload,'$.publish_at')<=?
                  THEN coalesce(json_extract(payload,'$.publish_priority'),0) ELSE 0 END"""
    near = now + NEAR_PUBLISH_SECONDS
    row = c.execute(
        f"""SELECT product,content,lang,({priority}) * ? AS priority,
        min(coalesce(json_extract(payload,'$.publish_at'),created)) AS due,
        max(coalesce(json_extract(payload,'$.urgent'),0)) AS urgent,
        min(json_extract(payload,'$.publish_at')) AS first_publish
        FROM jobs WHERE state='pending' AND key NOT IN (SELECT value FROM json_each(?))
        GROUP BY product,content,lang,priority
        HAVING count(*)>=100 OR urgent=1 OR first_publish<=?
          OR (min(created)<=? - CASE WHEN count(*)=1 AND first_publish IS NOT NULL
                                    THEN ? ELSE ? END
              AND (min(CASE WHEN endpoint='x_tag_auto' THEN 1 ELSE 0 END)=0
                   OR first_publish IS NULL OR first_publish<=? OR min(created)<=?))
        ORDER BY priority DESC,due,min(created),product,content,lang LIMIT 1""",
        (near, prefer_imminent, reserved, near, now, SCHEDULED_SINGLETON_WAIT_SECONDS, COLLECT_SECONDS,
         now + X_AUTO_GENERATION_LEAD_SECONDS, now - X_AUTO_MAX_COLLECTION_SECONDS),
    ).fetchone()
    # Carry failures into the next ready queue, even if its drama/language differs.
    # With no new work, retry after one normal collection window instead of waiting forever.
    rows = c.execute(
        f"""SELECT j.key,j.payload,j.created,{priority} AS priority,
        coalesce(json_extract(j.payload,'$.publish_at'),j.created) AS due
        FROM jobs j JOIN retries r ON r.key=j.key
        WHERE j.state='retry_wait' AND r.state='pending' AND (r.retry_after<=? OR ?)
          AND j.key NOT IN (SELECT value FROM json_each(?))
        ORDER BY priority DESC,due,r.created,j.key LIMIT 100""",
        (near, now, row is not None, reserved),
    ).fetchall()
    if row:
        # The due-time band protects an imminent batch. Future drafts of the
        # same story and language can share a prompt across publication slots.
        due_soon = row["first_publish"] is not None and row["first_publish"] <= near
        if row["urgent"] or due_soon:
            schedule_filter = "coalesce(json_extract(payload,'$.publish_at'),created)<=?"
            cutoff = near if due_soon else max(now, row["due"]) + NEAR_PUBLISH_SECONDS
        else:
            schedule_filter = "(json_extract(payload,'$.publish_at') IS NULL OR json_extract(payload,'$.publish_at')>?)"
            cutoff = near
        rows += c.execute(
            f"""SELECT key,payload,created,{priority} AS priority,
            coalesce(json_extract(payload,'$.publish_at'),created) AS due
            FROM jobs WHERE state='pending' AND product=? AND content=? AND lang=?
              AND key NOT IN (SELECT value FROM json_each(?))
              AND {schedule_filter}
              AND ({priority})>=?
            ORDER BY priority DESC,due,created,key LIMIT 100""",
            (near, row["product"], row["content"], row["lang"], reserved,
             cutoff, near, row["priority"]),
        ).fetchall()
    rows.sort(key=lambda r: (-r["priority"], r["due"], r["created"], r["key"]))
    return rows, bool(row and row["priority"])


def claim(c, *, lane="shared", publish_order=None):
    c.execute("BEGIN IMMEDIATE")
    now = time.time()
    gate = c.execute("SELECT until_at,probe_batch FROM provider_backoff WHERE id=1").fetchone()
    if gate and (gate["until_at"] > now or c.execute(
            "SELECT 1 FROM batches WHERE id=? AND finished IS NULL", (gate["probe_batch"],)
    ).fetchone()):
        c.commit()
        return None, []
    order = dump(publish_order or [])
    ordered = []
    prefix = []
    if publish_order:
        ordered = c.execute("""
            SELECT j.key,j.payload,j.product,j.content,j.lang FROM json_each(?) q JOIN jobs j ON j.key=q.value
            LEFT JOIN retries r ON r.key=j.key
            WHERE j.endpoint='fb' AND j.final IS NULL AND
              (j.state='pending' OR (j.state='retry_wait' AND r.state='pending' AND r.retry_after<=?))
            ORDER BY q.key LIMIT ?
        """, (order, now, FB_LOOKAHEAD)).fetchall()
    if ordered:
        # Keep the next model request in dispatch order and within one language/drama.
        # Small lookahead batches let a worker recheck newly claimed videos promptly.
        head = ordered[0]
        for row in ordered:
            if any(row[k] != head[k] for k in ("product", "content", "lang")):
                break
            prefix.append(row)
    if ordered and (lane != "bulk" or gate):
        rows = prefix[:1] if gate or lane == "priority" else prefix
        imminent = True
    elif lane == "priority" and not gate:
        # Reserve this worker for a real deadline. Bulk workers (or the shared
        # worker in a single-worker deployment) own ordinary work.
        near = now + NEAR_PUBLISH_SECONDS
        rows = c.execute(
            """SELECT j.key,j.payload,json_extract(j.payload,'$.publish_at') AS due
               FROM jobs j LEFT JOIN retries r ON r.key=j.key
               WHERE (j.state='pending' OR (j.state='retry_wait' AND r.state='pending' AND r.retry_after<=?))
                 AND (due<=? OR json_extract(j.payload,'$.urgent')=1)
                 AND (? OR j.endpoint!='fb' OR due IS NULL OR due>?)
               ORDER BY coalesce(json_extract(j.payload,'$.publish_priority'),0) DESC,
                        coalesce(due,j.created),j.created,j.key LIMIT 1""",
            (now, near, publish_order is None, near),
        ).fetchall()
        imminent = bool(rows)
    else:
        prefer_imminent = bool(gate) or not c.execute(
            """SELECT 1 FROM cursors JOIN batches ON batches.id=cursors.value
            WHERE cursors.name='generation:imminent-batch' AND batches.finished IS NULL"""
        ).fetchone()
        reserved = order if ordered else "[]"
        rows, imminent = normal_claim_rows(c, now, prefer_imminent, reserved)
        if not rows and ordered:
            rows, imminent = prefix, True
        # Until FB offers its next video, keep imminent FB requests short on
        # the lookahead worker. Ordinary future work retains the full batch.
        limit = 1 if gate else 100
        if lane == "lookahead" and not ordered and not gate and rows:
            first = json.loads(rows[0]["payload"])
            if first["endpoint"] == "fb" and first.get("publish_at") is not None and first["publish_at"] <= now + NEAR_PUBLISH_SECONDS:
                limit = FB_LOOKAHEAD
        rows = rows[:limit]
    if not rows:
        c.commit()
        return None, []
    batch = c.execute(
        "INSERT INTO batches(created,requested) VALUES(?,?)", (now, len(rows))
    ).lastrowid
    if gate:
        c.execute("UPDATE provider_backoff SET probe_batch=? WHERE id=1", (batch,))
    if imminent:
        c.execute("INSERT OR REPLACE INTO cursors(name,value) VALUES('generation:imminent-batch',?)", (batch,))
    c.executemany(
        "UPDATE jobs SET state='generating',batch=? WHERE key=?",
        [(batch, r["key"]) for r in rows],
    )
    c.executemany(
        "UPDATE retries SET state='generating',retry_batch=? WHERE key=? AND state='pending'",
        [(batch, r["key"]) for r in rows],
    )
    c.commit()
    return batch, [json.loads(r["payload"]) for r in rows]


def record_rate_limit(c, batch, retry_after):
    """Advance the shared cooldown once per probe, even for simultaneous 429s."""
    now = time.time()
    c.execute("BEGIN IMMEDIATE")
    row = c.execute("SELECT * FROM provider_backoff WHERE id=1").fetchone()
    if row and row["until_at"] > now and row["probe_batch"] != batch:
        until = max(row["until_at"], now + retry_after)
        c.execute("UPDATE provider_backoff SET until_at=? WHERE id=1", (until,))
    else:
        delay = min(row["delay"] * 2, 300) if row else 30
        until = now + max(delay, retry_after)
        c.execute("INSERT OR REPLACE INTO provider_backoff VALUES(1,?,?,NULL)", (until, delay))
    c.commit()
    return until


def defer_provider_jobs(c, batch, keys):
    for key in keys:
        retry = c.execute(
            """UPDATE retries SET state='pending',retry_batch=NULL
            WHERE key=? AND retry_batch=? AND state='generating'""", (key, batch),
        ).rowcount
        c.execute(
            """UPDATE jobs SET state=?,batch=NULL WHERE key=? AND batch=? AND state='generating'""",
            ("retry_wait" if retry else "pending", key, batch),
        )


def fail(c, batch, key, reason):
    """Persist the first failure, or settle the single retry. Caller commits with its alert."""
    retry = c.execute("SELECT 1 FROM retries WHERE key=?", (key,)).fetchone()
    state = "failed" if retry else "retry_wait"
    changed = c.execute(
        "UPDATE jobs SET state=?,reason=? WHERE key=? AND batch=? AND state='generating'",
        (state, reason, key, batch),
    ).rowcount
    if not changed:
        return None  # A valid draft or native final receipt already settled this job.
    now = time.time()
    if retry:
        c.execute(
            "UPDATE retries SET state='failed',reason=?,finished=? WHERE key=? AND retry_batch=?",
            (reason, now, key, batch),
        )
    else:
        c.execute(
            "INSERT INTO retries(key,first_batch,first_reason,created,retry_after,state) VALUES(?,?,?,?,?,'pending')",
            (key, batch, reason, now, now + RETRY_DELAY_SECONDS),
        )
    return state


def accept(c, batch, task, item):
    options = click_options(task, item.get("hooks"))
    clean = validate(task, item, options)
    plot = normalized(clean["opening"] + clean["turn"])
    digest = signature(plot)
    c.execute("BEGIN IMMEDIATE")
    try:
        clean.update(options[0])
        changed = c.execute(
            "UPDATE jobs SET state='ready',draft=?,plot=?,hash=?,reason='' WHERE key=? AND batch=? AND state='generating'",
            (dump(clean), plot, digest, task["key"], batch),
        ).rowcount
        if changed:
            c.execute(
                "UPDATE retries SET state='succeeded',reason='',finished=? WHERE key=? AND retry_batch=?",
                (time.time(), task["key"], batch),
            )
        c.commit()
        return bool(changed)
    except Exception:
        c.rollback()
        raise
