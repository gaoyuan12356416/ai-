"""Persistent, target-scoped daily schedules shared by organic publishers.

This module does not select media or publish. Callers own their transaction and
retain the native authorization, reservation and publication ledgers.
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Any, Callable, Iterable

POLICY = "target_midnight_v1"
SHANGHAI = dt.timezone(dt.timedelta(hours=8))


def local_day(now: dt.datetime | None = None) -> str:
    return (now or dt.datetime.now(dt.timezone.utc)).astimezone(SHANGHAI).date().isoformat()


def utc_at(day: str, time: str = "00:00") -> str:
    value = dt.datetime.fromisoformat(day + "T" + time).replace(tzinfo=SHANGHAI)
    return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure_schema(conn, *, now: dt.datetime | None = None, effective_date: str | None = None) -> None:
    # execute separately: executescript would commit a caller's transaction.
    conn.execute("CREATE TABLE IF NOT EXISTS social_random_policy (policy TEXT PRIMARY KEY, effective_date TEXT NOT NULL)")
    conn.execute("""CREATE TABLE IF NOT EXISTS social_random_day (
        source_key TEXT NOT NULL, local_date TEXT NOT NULL,
        config_version TEXT NOT NULL, config_json TEXT NOT NULL,
        targets_json TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY (source_key,local_date))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS social_random_slot (
        source_key TEXT NOT NULL, local_date TEXT NOT NULL,
        target_id TEXT NOT NULL, ordinal INTEGER NOT NULL, planned_at TEXT NOT NULL,
        native_kind TEXT NOT NULL DEFAULT '', native_id TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (source_key,local_date,target_id,ordinal))""")
    tomorrow = (dt.date.fromisoformat(local_day(now)) + dt.timedelta(days=1)).isoformat()
    effective = dt.date.fromisoformat(effective_date or tomorrow).isoformat()
    conn.execute("INSERT OR IGNORE INTO social_random_policy(policy,effective_date) VALUES(?,?)", (POLICY, effective))


def activation_date(conn) -> str:
    row = conn.execute("SELECT effective_date FROM social_random_policy WHERE policy=?", (POLICY,)).fetchone()
    return str(row[0]) if row else "9999-12-31"


def is_active(conn, day: str) -> bool:
    return day >= activation_date(conn)


def _decode(row) -> dict[str, Any] | None:
    if row is None:
        return None
    # Supports sqlite tuples and sqlite.Row without changing the connection.
    source, day, version, config, targets, created = tuple(row)
    return {"source_key": source, "local_date": day, "config_version": version,
            "config": json.loads(config), "targets": json.loads(targets),
            "created_at": created, "prepare_at": utc_at(day), "schedule_policy": POLICY}


def get_day_plan(conn, source_key: str, day: str) -> dict[str, Any] | None:
    return _decode(conn.execute("SELECT source_key,local_date,config_version,config_json,targets_json,created_at FROM social_random_day WHERE source_key=? AND local_date=?", (source_key, day)).fetchone())


def ensure_day_plan(conn, source_key: str, day: str, config_version: Any,
                    config: dict[str, Any], targets: Iterable[str],
                    time_factory: Callable[[str], Iterable[str]], *,
                    now: dt.datetime | None = None) -> dict[str, Any]:
    """Freeze one date/config snapshot; target IDs participate in each draw.

    time_factory must enforce the caller's existing count/window/spacing rules.
    Persisted plans always win, including after edits to the source config.
    """
    saved = get_day_plan(conn, source_key, day)
    if saved is not None:
        return saved
    frozen = []
    for target in dict.fromkeys(str(x) for x in targets):
        times = sorted(str(x) for x in time_factory(target))
        frozen.append({"target_id": target, "times": times, "slots": [
            {"ordinal": i, "publish_time": value, "scheduled_at": utc_at(day, value)}
            for i, value in enumerate(times)]})
    created = (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute("INSERT OR IGNORE INTO social_random_day(source_key,local_date,config_version,config_json,targets_json,created_at) VALUES(?,?,?,?,?,?)",
                 (source_key, day, str(config_version), json.dumps(config,ensure_ascii=False,sort_keys=True), json.dumps(frozen,ensure_ascii=False,sort_keys=True), created))
    saved = get_day_plan(conn, source_key, day)
    for target in saved["targets"]:
        for slot in target["slots"]:
            conn.execute("INSERT OR IGNORE INTO social_random_slot(source_key,local_date,target_id,ordinal,planned_at) VALUES(?,?,?,?,?)",
                         (source_key,day,target["target_id"],slot["ordinal"],slot["scheduled_at"]))
    return saved


def slot_binding(conn, source_key: str, day: str, target_id: str, ordinal: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT planned_at,native_kind,native_id FROM social_random_slot WHERE source_key=? AND local_date=? AND target_id=? AND ordinal=?", (source_key,day,str(target_id),int(ordinal))).fetchone()
    return {"planned_at":row[0],"native_kind":row[1],"native_id":row[2]} if row else None


def bind_slot(conn, source_key: str, day: str, target_id: str, ordinal: int, native_kind: str, native_id: Any) -> None:
    args=(source_key,day,str(target_id),int(ordinal))
    row=slot_binding(conn,*args)
    if row is None:
        raise ValueError("random_slot_not_planned")
    if row["native_id"] and (row["native_kind"],row["native_id"]) != (native_kind,str(native_id)):
        raise ValueError("random_slot_already_bound")
    conn.execute("UPDATE social_random_slot SET native_kind=?,native_id=? WHERE source_key=? AND local_date=? AND target_id=? AND ordinal=? AND native_id=''", (native_kind,str(native_id),*args))


def slot_key(day: str, target_id: str, ordinal: int) -> str:
    return "target:" + day + ":" + str(target_id) + ":" + str(ordinal)


def is_target_random(value: Any) -> bool:
    return isinstance(value, dict) and value.get("schedule_policy") == POLICY
