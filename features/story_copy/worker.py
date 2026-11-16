"""CPU preparation runner. Only approved native publishers may write to social platforms."""

import argparse
import logging
import threading
import time

from . import alerts, generator, sources, store
from .model import canonical_lang, group_key, history_plot, product, signature

LOG = logging.getLogger(__name__)


def data_for(key, row):
    if key == "fv":
        task = sources.fb_task("fv", row)
    elif key == "fa":
        task = sources.fb_task("fa", row)
        cfg = sources.obj(row.get("config_json"))
        task["product"] = task["product"] or product(cfg.get("product"))
        task["language"] = task["language"] or canonical_lang(cfg.get("language"))
    else:
        task = {
            "product": "moboreels"
            if "{{m_url}}" in str(row.get("body_template") or "")
            else "dramawave",
            "content": row["content_id"],
            "language": row["material_language"],
            "title": row["drama_name"],
        }
    return task


def seed_history(c, task):
    key = group_key(task)
    queries = [
        (
            "xm",
            store.SOURCES["xm"][1],
            "SELECT q.content_id,q.material_language,q.drama_name,q.body_template,l.post_text text,l.updated_at stamp FROM x_post_queue q JOIN x_post_publish_log l ON l.queue_id=q.id WHERE q.content_id=? AND l.status='published'",
        ),
        (
            "ta",
            store.TAG_POSTS,
            "SELECT q.content_id,q.material_language,q.drama_name,q.body_template,l.post_text text,l.updated_at stamp FROM x_post_queue q JOIN x_post_publish_log l ON l.queue_id=q.id WHERE q.content_id=? AND l.status='published'",
        ),
        (
            "fv",
            store.SOURCES["fv"][1],
            "SELECT t.rowid _ord,t.*,t.caption text,t.updated_at_utc stamp FROM fb_video_task t WHERE t.content_id=? AND t.status='published'",
        ),
        (
            "fa",
            store.SOURCES["fa"][1],
            "SELECT t.*,r.config_json,t.message_text text,t.completed_at_utc stamp FROM fb_auto_task t JOIN fb_auto_run r ON r.id=t.run_id WHERE t.content_id=? AND t.status='published'",
        ),
    ]
    from datetime import datetime

    for family, path, sql in queries:
        with store.connection(path, readonly=True, timeout=0.3) as src:
            rows = src.execute(sql, (task["content"],))
            for raw in rows:
                row = dict(raw)
                meta = data_for(family, row)
                if group_key(meta) != key:
                    continue
                plot = history_plot(row["text"], meta["title"])
                if not plot:
                    continue
                try:
                    at = datetime.fromisoformat(
                        str(row["stamp"]).replace("Z", "+00:00")
                    ).timestamp()
                except (TypeError, ValueError):
                    at = 0
                c.execute(
                    "INSERT OR IGNORE INTO history VALUES(?,?,?,?,?,?)",
                    (*key, signature(plot), plot, at),
                )
        c.commit()


def alert_item(task, reason, outcome="到点使用现行文案"):
    return dict(
        key=task["key"],
        endpoint=task["endpoint"],
        title=task.get("title") or task["content"],
        language=task["language"],
        reason=reason,
        outcome=outcome,
    )


def generate_batch(c, batch, tasks, runner=generator.run, seed=True):
    errors = {}
    prior = []
    if seed:
        try:
            seed_history(c, tasks[0])
        except Exception as exc:
            LOG.warning(
                "history unavailable for batch %s: %s", batch, type(exc).__name__
            )
            for t in tasks:
                errors[t["key"]] = "caption_history_unavailable"
    key = group_key(tasks[0])
    for r in c.execute(
        "SELECT draft FROM jobs WHERE product=? AND content=? AND lang=? AND draft IS NOT NULL ORDER BY created DESC LIMIT 5",
        key,
    ):
        d = sources.obj(r[0])
        prior.append((d.get("opening", "") + " " + d.get("turn", ""))[:260])
    if len(prior) < 5:
        prior.extend(p[:220] for p in store.history(c, key)[: 5 - len(prior)])
    ready = []
    for t in tasks:
        if t.get("evidence_error"):
            errors[t["key"]] = t["evidence_error"]
        if (
            not all(group_key(t))
            or not t["facts"]
            or (t["endpoint"] == "x_drama" and not t.get("episode"))
        ):
            errors[t["key"]] = "insufficient_evidence"
        if t["key"] not in errors:
            ready.append(t)
    tokens = [None, None]
    calls = 0
    for chunk in generator.splits(ready, prior) if ready else ():
        by_key = {t["key"]: t for t in chunk}
        submitted = set()

        def emit(item):
            identity = item.get("id")
            if identity not in by_key or identity in submitted:
                return
            submitted.add(identity)
            try:
                if item.get("error"):
                    raise ValueError("insufficient_evidence")
                store.accept(c, batch, by_key[identity], item)
            except (ValueError, TypeError) as exc:
                errors[identity] = str(exc)[:120] or "invalid_output"

        usage = {}
        calls += 1
        try:
            runner(chunk, prior, emit, usage)
            missed = "missing_model_row"
        except Exception as exc:
            LOG.warning("caption batch %s provider %s", batch, type(exc).__name__)
            missed = (
                str(exc)
                if str(exc) in alerts.SERVICE_REASONS
                else "codex_generation_failed"
            )
        for k in by_key.keys() - submitted:
            errors[k] = missed
        for i, k in enumerate(("inputTokens", "outputTokens")):
            if k in usage:
                tokens[i] = (tokens[i] or 0) + usage[k]
    failed = []
    for t in tasks:
        reason = errors.get(t["key"], "missing_model_row")
        changed = c.execute(
            "UPDATE jobs SET state='failed',reason=? WHERE key=? AND batch=? AND state='generating'",
            (reason, t["key"], batch),
        ).rowcount
        if changed:
            failed.append(alert_item(t, reason))
    accepted = c.execute(
        "SELECT count(*) FROM jobs WHERE batch=? AND draft IS NOT NULL", (batch,)
    ).fetchone()[0]
    c.execute(
        "UPDATE batches SET finished=?,accepted=?,input_tokens=?,output_tokens=?,calls=? WHERE id=?",
        (time.time(), accepted, *tokens, calls, batch),
    )
    alerts.add(c, "batch:" + str(batch), failed)
    c.commit()


def sync_receipts(c):
    paths = {store.SOURCES["fa"][1], store.SOURCES["xm"][1], store.TAG_POSTS}
    for path in paths:
        name = "receipt:" + str(path)
        cursor = c.execute("SELECT value FROM cursors WHERE name=?", (name,)).fetchone()
        with store.connection(path, readonly=True) as src:
            for raw in src.execute(
                "SELECT rowid _ord,* FROM story_copy_receipt WHERE rowid>? ORDER BY rowid LIMIT 1000",
                (cursor[0] if cursor else 0,),
            ):
                r = dict(raw)
                key = r["task_key"]
                job = c.execute("SELECT * FROM jobs WHERE key=?", (key,)).fetchone()
                task = sources.obj(job["payload"]) if job else None
                if not task:
                    prefix, kind, ident = key.split(":", 2)
                    if prefix == "fb":
                        family, table, field = (
                            ("fa", "fb_auto_task", "id")
                            if kind == "auto"
                            else ("fv", "fb_video_task", "task_id")
                        )
                        row = src.execute(
                            "SELECT rowid _ord,* FROM "
                            + table
                            + " WHERE "
                            + field
                            + "=?",
                            (ident,),
                        ).fetchone()
                        if row:
                            task = sources.fb_task(family, dict(row))
                    elif prefix == "tag" and kind == "auto":
                        with store.connection(
                            store.SOURCES["ta"][1], readonly=True
                        ) as tag:
                            row = tag.execute(
                                "SELECT * FROM x_auto_task WHERE id=?", (ident,)
                            ).fetchone()
                            if row:
                                task = sources.tag_task(dict(row))
                    else:
                        row = src.execute(
                            "SELECT * FROM x_post_queue WHERE id=?", (ident,)
                        ).fetchone()
                        if row:
                            task = sources.x_task(src, dict(row), tag=prefix == "tag")
                    if task and not store.enqueue(c, task):
                        task = None
                if task:
                    oldreason = job["reason"] if job else ""
                    c.execute(
                        "UPDATE jobs SET state=?,reason=?,final=?,short=? WHERE key=?",
                        (
                            r["source"],
                            r["reason"],
                            r["final_text"],
                            r["short_url"],
                            key,
                        ),
                    )
                    if r["source"] == "legacy" and not oldreason:
                        alerts.add(
                            c,
                            "receipt:" + key,
                            [alert_item(task, r["reason"], "已采用现行文案")],
                        )
                c.execute(
                    "INSERT OR REPLACE INTO cursors VALUES(?,?)", (name, r["_ord"])
                )
        c.commit()


def background(fn, delay):
    while True:
        try:
            with store.connection() as c:
                fn(c)
        except Exception:
            LOG.exception("story copy auxiliary cycle %s", fn.__name__)
        time.sleep(delay)


def restore(c):
    for (batch,) in c.execute(
        "SELECT id FROM batches WHERE finished IS NULL"
    ).fetchall():
        rows = c.execute(
            "SELECT payload FROM jobs WHERE batch=? AND state='generating'", (batch,)
        ).fetchall()
        c.execute(
            "UPDATE jobs SET state='failed',reason='worker_interrupted' WHERE batch=? AND state='generating'",
            (batch,),
        )
        c.execute(
            "UPDATE batches SET finished=?,accepted=(SELECT count(*) FROM jobs WHERE batch=? AND draft IS NOT NULL),reason='worker_interrupted' WHERE id=?",
            (time.time(), batch, batch),
        )
        alerts.add(
            c,
            "batch:" + str(batch),
            [alert_item(sources.obj(r[0]), "worker_interrupted") for r in rows],
        )
    c.commit()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--init", action="store_true")
    args = parser.parse_args()
    store.initialize()
    if args.init:
        return
    # Keep the WAL read index available to read-only native publishing identities.
    keeper = __import__("sqlite3").connect(str(store.DB))
    keeper.execute("SELECT count(*) FROM switches").fetchone()
    with store.connection() as c:
        restore(c)
    for fn, delay in ((sources.discover, 2), (sync_receipts, 3), (alerts.deliver, 6)):
        threading.Thread(target=background, args=(fn, delay), daemon=True).start()
    while True:
        try:
            with store.connection() as c:
                batch, tasks = store.claim(c)
                if tasks:
                    try:
                        generate_batch(c, batch, tasks)
                    except Exception:
                        c.rollback()
                        restore(c)
                        raise
            if not tasks:
                time.sleep(1)
        except Exception:
            LOG.exception("caption generation cycle")
            time.sleep(2)


if __name__ == "__main__":
    main()
