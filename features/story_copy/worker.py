"""CPU preparation runner. Only approved native publishers may write to social platforms."""

import argparse
import logging
import os
import threading
import time

from . import alerts, generator, sources, store
from .model import click_prefix, group_key, signature

LOG = logging.getLogger(__name__)


def alert_item(task, reason, outcome="到点使用现行文案"):
    return dict(
        key=task["key"],
        endpoint=task["endpoint"],
        title=task.get("title") or task["content"],
        language=task["language"],
        reason=reason,
        outcome=outcome,
        publish_batch=task.get("publish_batch", ""),
    )


def generate_batch(c, batch, tasks, runner=generator.run):
    errors = {}
    ready = []
    for t in tasks:
        try:
            click_prefix(t["language"])
        except ValueError as exc:
            errors[t["key"]] = str(exc)
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
    provider_errors = []
    deferred = set()
    for chunk in generator.splits(ready) if ready else ():
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

        missed = "missing_model_row"
        for model in (None, generator.CAPACITY_FALLBACK_MODEL):
            gate = c.execute("SELECT until_at,probe_batch FROM provider_backoff WHERE id=1").fetchone()
            if gate and (gate["until_at"] > time.time() or gate["probe_batch"] != batch):
                deferred.update(by_key.keys() - submitted)
                break
            request = chunk
            if model:
                # Receipts or streamed drafts can settle rows while the primary is running.
                active = {r[0] for r in c.execute(
                    """SELECT key FROM jobs WHERE key IN (SELECT value FROM json_each(?))
                    AND batch=? AND state='generating' AND final IS NULL""",
                    (store.dump(list(by_key)), batch),
                )}
                request = [t for t in chunk if t["key"] in active and t["key"] not in submitted]
                if not request:
                    break
                fallback = dict(model=model, keys=[t["key"] for t in request], outcome="attempted")
                primary_error["fallback"] = fallback
                LOG.info("caption batch %s capacity fallback model=%s tasks=%s", batch, model, len(request))
            usage = {}
            calls += 1
            try:
                if model:
                    runner(request, emit, usage, model=model)
                    fallback.update(outcome="completed", observed_model=usage.get("model"))
                else:
                    runner(request, emit, usage)
                if gate:
                    c.execute("DELETE FROM provider_backoff WHERE id=1 AND probe_batch=?", (batch,))
                    c.commit()
                break
            except Exception as exc:
                is_provider = isinstance(exc, generator.ProviderError)
                detail = exc.details if is_provider else {"exception": type(exc).__name__}
                reason = str(exc) if is_provider else "codex_generation_failed"
                error = dict(keys=[t["key"] for t in request], reason=reason, details=detail,
                             model=usage.get("model") or model or "default")
                provider_errors.append(error)
                LOG.warning("caption batch %s provider model=%s details=%s", batch, error["model"], store.dump(detail))
                if not model and is_provider and exc.at_capacity and usage.get("model") != generator.CAPACITY_FALLBACK_MODEL:
                    primary_error = error
                    continue
                if model:
                    fallback.update(outcome="failed", observed_model=usage.get("model"))
                if is_provider and exc.rate_limited:
                    until = store.record_rate_limit(c, batch, exc.retry_after)
                    LOG.warning("caption provider cooldown until=%.0f batch=%s", until, batch)
                    deferred.update(by_key.keys() - submitted)
                missed = str(exc) if str(exc) in alerts.SERVICE_REASONS else "codex_generation_failed"
                break
            finally:
                for i, k in enumerate(("inputTokens", "outputTokens")):
                    if k in usage:
                        tokens[i] = (tokens[i] or 0) + usage[k]
        for k in by_key.keys() - submitted:
            errors[k] = missed
    store.defer_provider_jobs(c, batch, deferred)
    failed = []
    for t in tasks:
        if t["key"] in deferred:
            continue
        reason = errors.get(t["key"], "missing_model_row")
        if store.fail(c, batch, t["key"], reason) == "failed":
            failed.append(alert_item(t, reason, "重试一次仍失败，到点使用现行文案"))
    accepted = c.execute(
        "SELECT count(*) FROM jobs WHERE batch=? AND draft IS NOT NULL", (batch,)
    ).fetchone()[0]
    c.execute(
        "UPDATE batches SET finished=?,accepted=?,input_tokens=?,output_tokens=?,calls=?,reason=? WHERE id=?",
        (time.time(), accepted, *tokens, calls, store.dump(provider_errors) if provider_errors else "", batch),
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
                    retry = c.execute(
                        "SELECT 1 FROM retries WHERE key=?", (key,)
                    ).fetchone()
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
                    # A native receipt freezes the copy. Cancel pending retries,
                    # preserving the first failure and never replaying publication.
                    c.execute(
                        "UPDATE retries SET state='finalized',reason=?,finished=? WHERE key=? AND state IN ('pending','generating')",
                        (r["reason"], time.time(), key),
                    )
                    if r["source"] == "legacy" and not oldreason and not retry:
                        alerts.add(
                            c,
                            "receipt-batch:"
                            + str(
                                r["batch"]
                                or (
                                    str(int(r["created"] // 60))
                                    + "-"
                                    + signature("|".join(group_key(task)))[:12]
                                )
                            ),
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


def restore(c, batch_id=None):
    # Startup may recover all abandoned batches. A live worker may recover only its own.
    for (batch,) in c.execute(
        "SELECT id FROM batches WHERE finished IS NULL AND (? IS NULL OR id=?)",
        (batch_id, batch_id),
    ).fetchall():
        rows = c.execute(
            "SELECT payload FROM jobs WHERE batch=? AND state='generating'", (batch,)
        ).fetchall()
        if c.execute("SELECT 1 FROM provider_backoff WHERE id=1").fetchone():
            # A crash may occur after recording a 429 and before finishing its batch.
            store.defer_provider_jobs(c, batch, [sources.obj(r[0])["key"] for r in rows])
            rows = []
        failed = []
        for row in rows:
            task = sources.obj(row[0])
            if store.fail(c, batch, task["key"], "worker_interrupted") == "failed":
                failed.append(alert_item(task, "worker_interrupted", "重试一次仍失败，到点使用现行文案"))
        c.execute(
            "UPDATE batches SET finished=?,accepted=(SELECT count(*) FROM jobs WHERE batch=? AND draft IS NOT NULL),reason='worker_interrupted' WHERE id=?",
            (time.time(), batch, batch),
        )
        alerts.add(
            c,
            "batch:" + str(batch),
            failed,
        )
    c.commit()


def generation_cycle(runner=generator.run):
    with store.connection() as c:
        batch, tasks = store.claim(c)
        if tasks:
            LOG.info("caption batch %s starting %s tasks", batch, len(tasks))
            try:
                generate_batch(c, batch, tasks, runner)
            except Exception:
                c.rollback()
                restore(c, batch)
                raise
            LOG.info("caption batch %s finished", batch)
    return bool(tasks)


def generation_loop():
    while True:
        try:
            if not generation_cycle():
                time.sleep(1)
        except Exception:
            LOG.exception("caption generation cycle")
            time.sleep(2)


def concurrency():
    try:
        count = int(os.environ.get("STORY_COPY_CONCURRENCY", "3"))
        if count > 0:
            return count
    except ValueError:
        pass
    LOG.warning("invalid STORY_COPY_CONCURRENCY; using 3")
    return 3


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--init", action="store_true")
    args = parser.parse_args()
    store.initialize()
    if args.init:
        return
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # Keep the WAL read index available to read-only native publishing identities.
    keeper = __import__("sqlite3").connect(str(store.DB))
    keeper.execute("SELECT count(*) FROM switches").fetchone()
    with store.connection() as c:
        restore(c)
        # Populate deadlines on existing unfinished jobs before the first claims.
        sources.discover(c)
    for fn, delay in ((sources.discover, 2), (sync_receipts, 3), (alerts.deliver, 6)):
        threading.Thread(target=background, args=(fn, delay), daemon=True).start()
    count = concurrency()
    LOG.info("caption generation workers=%s priority=publish_time", count)
    for i in range(1, count):
        threading.Thread(target=generation_loop, name="caption-" + str(i), daemon=True).start()
    generation_loop()


if __name__ == "__main__":
    main()
