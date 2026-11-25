"""A small native story-copy consumer; drafts belong to the promotion task."""

import argparse
import os
import sqlite3
import time
from contextlib import closing
import uuid
from datetime import timedelta

from .pub_store import connection, dump, iso, load, now


def draft_facts(slot, channel, choice, template, *, episode=None):
    title = choice["drama_name"]
    facts = {k: v for k, v in [("title", title), ("material", choice.get("description"))] if v}
    if episode:
        facts["episode"] = str(episode)
    return dict(
        episode=episode,
        key=slot,
        endpoint="tt_auto",
        language=channel["drama_language"],
        title=title,
        content=choice["content_id"],
        product="DramaWave",
        facts=facts,
        caption_template=template,
    )


def submit(db, slot, channel, choice, config, manual, *, episode=None):
    if manual:
        state, body, source = "final", manual, "manual"
    elif config.get("ai_copy"):
        state, body, source = "waiting", "", ""
    else:
        state, body, source = "final", config["caption_template"], "template"
    facts = draft_facts(slot, channel, choice, config["caption_template"], episode=episode)
    db.execute(
        "INSERT INTO ttp_copy(slot_key,facts,state,caption_body,source) VALUES(?,?,?,?,?)",
        (slot, dump(facts), state, body, source),
    )


def fallback(db, slot, reason, *, table="ttp_copy"):
    db.execute(
        f"""UPDATE {table} SET state='final',caption_body=json_extract(facts,'$.caption_template'),
               source='template',reason=?,lease='' WHERE slot_key=? AND state<>'final'""",
        (reason, slot),
    )


def generate(facts):
    from features.story_copy import generator, model, store

    # Use the provider's existing backoff window. This module only reads it.
    if store.DB.is_file():
        with closing(
            sqlite3.connect(store.DB.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
        ) as shared:
            if shared.execute("SELECT 1 FROM sqlite_master WHERE name='provider_backoff'").fetchone():
                delay = shared.execute("SELECT until_at FROM provider_backoff WHERE id=1").fetchone()
                if delay and delay[0] > time.time():
                    raise RuntimeError("provider_rate_limited")
    drafts = []

    def accept(item):
        checked = model.validate(facts, item)
        drafts.append(
            checked["opening"] + "\n" + checked["turn"] + "\n" + " ".join("#" + s for s in checked["tags"])
        )

    generator.run([facts], accept, {})
    if not drafts:
        raise ValueError("no_valid_draft")
    base = facts["caption_template"]
    if "{desc}" in base:
        text = base.replace("{desc}", drafts[0])
    else:
        text = drafts[0] + "\n" + base
    if len(text.encode("utf-16-le")) // 2 > 2000:
        raise ValueError("generated_caption_too_long")
    return text


def process_one(path, provider=generate, *, table="ttp_copy"):
    if table not in ("ttp_copy", "ttp_draft"):
        raise ValueError("invalid_queue")
    start = now()
    token = uuid.uuid4().hex
    with connection(path, write=True) as db:
        row = db.execute(
            f"""SELECT * FROM {table} WHERE (state='waiting' AND next_at<=?)
                      OR (state='generating' AND lease_at<?) ORDER BY rowid LIMIT 1""",
            (iso(start), iso(start - timedelta(minutes=8))),
        ).fetchone()
        if not row:
            return False
        if row["attempts"] >= 2:
            fallback(db, row["slot_key"], "文案生成超时", table=table)
            return True
        db.execute(
            f"UPDATE {table} SET state='generating',lease=?,lease_at=?,attempts=attempts+1 WHERE slot_key=?",
            (token, iso(start), row["slot_key"]),
        )
    try:
        body = provider(load(row["facts"]))
    except Exception as error:
        with connection(path, write=True) as db:
            current = db.execute(
                f"SELECT * FROM {table} WHERE slot_key=? AND lease=?", (row["slot_key"], token)
            ).fetchone()
            if current:
                if current["attempts"] >= 2:
                    reason = (
                        "AI 服务限流，采用模板稿"
                        if str(error) == "provider_rate_limited"
                        else "AI 生成失败：" + type(error).__name__
                    )
                    fallback(db, row["slot_key"], reason, table=table)
                else:
                    db.execute(
                        f"UPDATE {table} SET state='waiting',lease='',next_at=? WHERE slot_key=?",
                        (iso(now() + timedelta(seconds=20)), row["slot_key"]),
                    )
    else:
        with connection(path, write=True) as db:
            db.execute(
                f"""UPDATE {table} SET state='final',caption_body=?,source='ai',lease=''
                       WHERE slot_key=? AND state='generating' AND lease=?""",
                (body, row["slot_key"], token),
            )
    return True


if __name__ == "__main__":
    argparse.ArgumentParser().parse_args()
    path = os.environ.get("TT_PROMOTION_DB", "/mnt/data-disk/tt-promotion/promotion.sqlite3")
    if os.path.isfile(path):
        for _ in range(4):
            handled = process_one(path)
            previewed = process_one(path, table="ttp_draft")
            if not (handled or previewed):
                break
