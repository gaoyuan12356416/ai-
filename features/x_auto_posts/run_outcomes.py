"""One disjoint account-task summary for templates, run records and run details."""
from collections import defaultdict
from contextlib import closing
from pathlib import Path
import sqlite3

from .core import _task_from_row

TERMINAL = ("published", "skipped", "no_candidate", "failed", "canceled")
WAITING = ("unknown", "waiting_release", "historical_pause", "account_blocked",
           "waiting_result", "retry_wait", "ready", "pending")
OPEN = {"running", "queued"}
DISPLAY_FILTERS = set(WAITING) | {"needs_review", "mixed_wait"}


def field(row, name):
    return row.get(name) if isinstance(row, dict) else getattr(row, name, None)


def category(task, holds, now):
    status = field(task, "status")
    if field(task, "unknown_outcome") or status == "unknown":
        return "unknown"
    if status in TERMINAL:
        return status
    hint = holds.get(field(task, "id"))
    if hint in {"preparing","publishing"}:
        return "active"
    if hint:
        return hint
    if status in {"ready", "pending", "retry_wait"} and (field(task, "scheduled_at_utc") or "") > now:
        return "waiting_release"
    if status in {"retry_wait", "reconciling"} and field(task, "execution_queue_id"):
        return "waiting_result"
    return status if status in WAITING else "active"


def summary(run, tasks, holds, now):
    counts = dict.fromkeys(TERMINAL + WAITING + ("active",), 0)
    due, progress = [], []
    for task in tasks:
        group = category(task, holds, now)
        counts[group] += 1
        if group == "waiting_release":
            due.append(field(task, "scheduled_at_utc"))
        for key in ("selected_at_utc", "reserved_at_utc", "bridged_at_utc", "published_at_utc", "finished_at_utc"):
            stamp = field(task, key)
            if stamp:
                progress.append(stamp)
    waiting = [key for key in WAITING if counts[key]]
    unfinished = sum(counts[k] for k in WAITING) + counts["active"]
    display = run.status
    if display in OPEN and unfinished:
        if counts["active"]:
            display = "running"
        elif len(waiting) == 1:
            display = {"unknown": "needs_review", "pending": "queued"}.get(waiting[0], waiting[0])
        else:
            display = "mixed_wait"
    return dict(task_count=sum(counts.values()), task_counts=counts, display_status=display,
                published_task_count=counts["published"], unfinished_task_count=unfinished,
                failed_task_count=counts["failed"] + counts["canceled"],
                completed_task_count=sum(counts[k] for k in ("published", "skipped", "no_candidate")),
                attention_task_count=sum(counts[k] for k in ("unknown", "failed", "canceled")),
                next_task_at_utc=min(due, default=""), last_progress_at_utc=max(progress, default=""))


def read(store, runs, now):
    summaries, holds, _ = _read(store,runs,now,include_tasks=False)
    return summaries, holds


def detail(store, run, now):
    summaries, holds, tasks = _read(store,[run],now,include_tasks=True)
    return summaries[run.id], holds, tasks


def _read(store, runs, now, *, include_tasks):
    runs = {r.id: r for r in runs}
    records = []
    if not runs:
        return {}, {}, records
    if not getattr(store, "db_path", None):
        result = {}
        for rid, run in runs.items():
            tasks = store.list_tasks(run_id=rid)
            result[rid] = summary(run, tasks, {}, now)
            if include_tasks:
                records.extend(tasks)
        return result, {}, records
    groups, holds = defaultdict(list), {}
    with closing(sqlite3.connect(Path(store.db_path).resolve().as_uri()+"?mode=ro", uri=True, timeout=3)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE name IN ('pipeline_canonical_hold','x_tag_task_wait','x_tag_task_activity')")}
        seen = list(runs)
        for start in range(0, len(seen), 400):
            part = seen[start:start+400]
            marks = ",".join("?" for _ in part)
            fields = "t.*" if include_tasks else (
                "t.id,t.run_id,t.status,t.unknown_outcome,t.execution_queue_id,t.scheduled_at_utc,"
                "t.selected_at_utc,t.reserved_at_utc,t.bridged_at_utc,t.published_at_utc,t.finished_at_utc")
            sql = "SELECT "+fields+" FROM x_auto_task t WHERE t.run_id IN ("+marks+") ORDER BY t.id"
            for row in conn.execute(sql, part):
                groups[row["run_id"]].append(dict(row))
                if include_tasks:
                    records.append(_task_from_row(row))
            if "x_tag_task_activity" in tables:
                for row in conn.execute("SELECT s.task_id,s.stage FROM x_tag_task_activity s JOIN x_auto_task t ON t.id=s.task_id AND t.execution_queue_id=s.queue_id WHERE t.run_id IN ("+marks+") AND julianday(s.lease_until)>julianday(?)", [*part,now]):
                    holds[row[0]] = row[1]
            if "x_tag_task_wait" in tables:
                for row in conn.execute("SELECT w.task_id FROM x_tag_task_wait w JOIN x_auto_task t ON t.id=w.task_id AND t.execution_queue_id=w.queue_id WHERE t.run_id IN ("+marks+")", part):
                    holds[row[0]] = "account_blocked"
            if "pipeline_canonical_hold" in tables:
                for row in conn.execute("SELECT t.id FROM x_auto_task t JOIN pipeline_canonical_hold h ON h.source_key='x:auto:'||t.id WHERE t.run_id IN ("+marks+") AND h.reason='historical_preparation_only'", part):
                    holds[row[0]] = "historical_pause"
    return {i: summary(run, groups[i], holds, now) for i, run in runs.items()}, holds, records
