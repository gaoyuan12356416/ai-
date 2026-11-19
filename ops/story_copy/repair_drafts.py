"""Audited CTA refresh and explicit failed-caption regeneration; never publishes."""

import argparse
import json
import os
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from features.story_copy import generator, model, store


def frozen(task):
    native = store.TAG_POSTS if task["family"] == "ta" else store.SOURCES[task["family"]][1]
    with store.connection(native, readonly=True, timeout=0.4) as src:
        return bool(src.execute(
            "SELECT 1 FROM story_copy_receipt WHERE task_key=?", (task["key"],)
        ).fetchone())


def save_candidate(c, before, draft, batch=None):
    task = json.loads(before["payload"])
    if before["state"] not in ("ready", "failed") or before["final"] is not None or frozen(task):
        return False
    # The native publisher owns the immutable final text. Its receipt wins even
    # if it is written between this read and this optional preparation update.
    c.execute("BEGIN IMMEDIATE")
    try:
        match = " WHERE key=? AND state=? AND draft IS ? AND batch IS ? AND payload=? AND final IS NULL"
        args = (before["key"], before["state"], before["draft"], before["batch"], before["payload"])
        if batch is None:
            changed = c.execute("UPDATE jobs SET draft=?" + match, (store.dump(draft), *args)).rowcount
        else:
            plot = model.normalized(draft["opening"] + draft["turn"])
            changed = c.execute(
                "UPDATE jobs SET state='ready',draft=?,plot=?,hash=?,reason='',batch=?" + match,
                (store.dump(draft), plot, model.signature(plot), batch, *args),
            ).rowcount
        c.commit()
        return bool(changed)
    except Exception:
        c.rollback()
        raise


def audit_write(audit, entry):
    audit.write(store.dump(dict(at=time.time(), **entry)) + "\n")
    audit.flush()
    os.fsync(audit.fileno())


def refresh(rows, audit, hooks=None):
    outcomes = Counter()
    with store.connection() as c:
        for before in rows:
            task = json.loads(before["payload"])
            draft = json.loads(before["draft"])
            hook = (hooks or {}).get(before["key"], draft.get("click_hook"))
            try:
                revised = dict(draft, **model.click_options(task, [hook])[0])
            except ValueError:
                outcomes["needs_hook_generation"] += 1
                continue
            if revised == draft:
                outcomes["unchanged"] += 1
                continue
            audit_write(audit, dict(action="refresh_cta", before=before, draft=revised))
            try:
                outcome = "updated" if save_candidate(c, before, revised) else "frozen_or_changed"
            except sqlite3.OperationalError as exc:
                if not any(word in str(exc).lower() for word in ("busy", "locked")):
                    raise
                outcome = "deferred"
            outcomes[outcome] += 1
            audit_write(audit, dict(key=before["key"], outcome=outcome))
    return outcomes


def regenerate(rows, audit):
    by_key = {r["key"]: r for r in rows}
    tasks = [json.loads(r["payload"]) for r in rows]
    outcomes = Counter()
    for chunk in generator.splits(tasks):
        accepted, usage = {}, {}

        def emit(item):
            task = next((t for t in chunk if t["key"] == item.get("id")), None)
            if task is not None and task["key"] not in accepted:
                if item.get("error"):
                    raise ValueError(str(item["error"]))
                accepted[task["key"]] = model.validate(task, item)

        started = time.time()
        generator.run(chunk, emit, usage)
        if len(accepted) != len(chunk):
            raise RuntimeError("regeneration_missing_rows")
        audit_write(audit, dict(action="regenerate", before=[by_key[t["key"]] for t in chunk], drafts=accepted, usage=usage))
        with store.connection() as c:
            batch = c.execute(
                "INSERT INTO batches(created,finished,requested,input_tokens,output_tokens,calls,reason) VALUES(?,?,?,?,?,1,'operator_regeneration')",
                (started, time.time(), len(chunk), usage.get("inputTokens"), usage.get("outputTokens")),
            ).lastrowid
            c.commit()
            saved = 0
            for key, draft in accepted.items():
                changed = save_candidate(c, by_key[key], draft, batch)
                saved += int(changed)
                outcome = "repaired" if changed else "frozen_or_changed"
                outcomes[outcome] += 1
                audit_write(audit, dict(key=key, batch=batch, outcome=outcome))
            c.execute("UPDATE batches SET accepted=? WHERE id=?", (saved, batch))
    return outcomes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("refresh-cta", "regenerate"))
    parser.add_argument("--key", action="append", default=[])
    parser.add_argument("--audit", type=Path)
    parser.add_argument("--restore-hooks-from", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.action == "regenerate" and not args.key:
        parser.error("regeneration requires explicit task keys")
    with store.connection(readonly=True) as c:
        if args.action == "refresh-cta":
            rows = [dict(r) for r in c.execute("SELECT * FROM jobs WHERE state='ready' AND final IS NULL AND coalesce(json_extract(draft,'$.cta_style'),'')!='watch_full_now_hook' ORDER BY created,key")]
        else:
            rows = []
            for key in dict.fromkeys(args.key):
                row = c.execute("SELECT * FROM jobs WHERE key=? AND state='failed' AND final IS NULL", (key,)).fetchone()
                if row is None or frozen(json.loads(row["payload"])):
                    raise RuntimeError("task is no longer an unfrozen failed caption: " + key)
                rows.append(dict(row))
    print(store.dump(dict(action=args.action, candidates=len(rows), apply=args.apply)), flush=True)
    if not args.apply:
        return
    if args.audit is None:
        parser.error("--apply requires --audit")
    hooks = {}
    if args.restore_hooks_from:
        for line in args.restore_hooks_from.read_text(encoding="utf-8").splitlines():
            entry = json.loads(line)
            before = entry.get("before")
            if entry.get("action") == "refresh_cta" and isinstance(before, dict):
                draft = json.loads(before["draft"])
                if draft.get("click_hook") and draft.get("cta_style") != "watch_full_now":
                    hooks[before["key"]] = draft["click_hook"]
    with args.audit.open("x", encoding="utf-8") as audit:
        args.audit.chmod(0o600)
        result = refresh(rows, audit, hooks) if args.action == "refresh-cta" else regenerate(rows, audit)
    print(store.dump(result), flush=True)


if __name__ == "__main__":
    main()
