"""Independent promotion scheduling facts. Native TT tasks live in this database too."""

import contextlib
import json
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

UTC = timezone.utc
BEIJING = timezone(timedelta(hours=8))
TASK_BASE = 4_000_000_000_000


def now():
    return datetime.now(UTC)


def iso(value):
    if not value.tzinfo:
        raise ValueError("预约时间需要时区")
    return value.astimezone(UTC).isoformat(timespec="seconds")


def dump(v):
    return json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def load(v):
    return json.loads(v or "{}")


@contextlib.contextmanager
def connection(path, *, write=False):
    uri = Path(path).resolve().as_uri() + ("?mode=rw" if write else "?mode=ro")
    db = sqlite3.connect(uri, uri=True, timeout=10)
    db.row_factory = sqlite3.Row
    try:
        db.execute("pragma foreign_keys=on")
        if write:
            db.execute("begin immediate")
        else:
            db.execute("pragma query_only=on")
        yield db
        if write:
            db.commit()
    finally:
        db.close()


DDL = """
CREATE TABLE IF NOT EXISTS ttp_day(template_id INTEGER NOT NULL,day TEXT NOT NULL,version INTEGER NOT NULL,
 slots TEXT NOT NULL,PRIMARY KEY(template_id,day));
CREATE TABLE IF NOT EXISTS ttp_job(
 id INTEGER PRIMARY KEY, op_key TEXT UNIQUE NOT NULL, request_hash TEXT NOT NULL,
 template_id INTEGER NOT NULL, version INTEGER NOT NULL, template_name TEXT NOT NULL,
 tenant TEXT NOT NULL, actor TEXT NOT NULL, trigger_type TEXT NOT NULL,
 opts TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ttp_slot(
 slot_key TEXT PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES ttp_job(id),
 account_id TEXT NOT NULL, scheduled_at TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'planned',
 reason TEXT NOT NULL DEFAULT '', material_id TEXT, choice TEXT NOT NULL DEFAULT '{}',
 account_json TEXT NOT NULL DEFAULT '{}', opts TEXT NOT NULL DEFAULT '{}',
 next_at TEXT NOT NULL DEFAULT '', changed_at TEXT NOT NULL, native_run_id INTEGER,
 UNIQUE(job_id,account_id), UNIQUE(material_id));
CREATE INDEX IF NOT EXISTS ttp_slot_due ON ttp_slot(state,next_at,scheduled_at);
CREATE INDEX IF NOT EXISTS ttp_slot_account ON ttp_slot(account_id,scheduled_at);
CREATE TABLE IF NOT EXISTS ttp_copy(
 slot_key TEXT PRIMARY KEY REFERENCES ttp_slot(slot_key), facts TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'waiting', attempts INTEGER NOT NULL DEFAULT 0,
 caption_body TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '',
 reason TEXT NOT NULL DEFAULT '', next_at TEXT NOT NULL DEFAULT '',
 lease TEXT NOT NULL DEFAULT '', lease_at TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS ttp_draft(
 slot_key TEXT PRIMARY KEY,template_id INTEGER NOT NULL,template_version INTEGER NOT NULL,actor_id TEXT NOT NULL,
 account_id TEXT NOT NULL,material_id TEXT NOT NULL,created_at TEXT NOT NULL,
 facts TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'waiting',attempts INTEGER NOT NULL DEFAULT 0,
 caption_body TEXT NOT NULL DEFAULT '',source TEXT NOT NULL DEFAULT '',reason TEXT NOT NULL DEFAULT '',
 next_at TEXT NOT NULL DEFAULT '',lease TEXT NOT NULL DEFAULT '',lease_at TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS ttp_public_receipt(
 task_id INTEGER PRIMARY KEY,account_id TEXT NOT NULL,creator_username TEXT NOT NULL DEFAULT '',
 publish_id TEXT NOT NULL DEFAULT '',video_ids TEXT NOT NULL DEFAULT '[]',
 attempts INTEGER NOT NULL DEFAULT 0,next_check TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS ttp_series(
 tenant TEXT NOT NULL, content_id TEXT NOT NULL, template_id INTEGER NOT NULL,
 account_id TEXT NOT NULL, episodes TEXT NOT NULL, PRIMARY KEY(tenant,content_id));
CREATE TABLE IF NOT EXISTS ttp_pool(
 template_id INTEGER NOT NULL, material_id TEXT NOT NULL, PRIMARY KEY(template_id,material_id));
CREATE VIEW IF NOT EXISTS tt_promotion_report_plan AS
 SELECT s.slot_key,t.id task_id,j.template_id,j.template_name,s.account_id,
 s.scheduled_at scheduled_at_utc,s.state slot_state,s.reason slot_reason,j.trigger_type,
 json_extract(v.config_json,'$.video_template') video_template
 FROM ttp_slot s JOIN ttp_job j ON j.id=s.job_id
 JOIN tt_auto_template_version v ON v.template_id=j.template_id AND v.version=j.version
 LEFT JOIN tt_auto_task t ON t.run_id=s.native_run_id AND t.account_id=s.account_id;
CREATE VIEW IF NOT EXISTS tt_promotion_report_task AS
 SELECT t.id,j.template_id,j.tenant template_tenant,tm.created_by_user_id template_creator_user_id,
 t.account_id,t.account_username,t.account_display_name,t.drama_language,
 coalesce(json_extract(t.selection_json,'$.material.drama_name'),
          json_extract(t.selection_json,'$.drama.name'),json_extract(s.choice,'$.drama_name')) drama_name,
 t.content_id,t.material_id,s.scheduled_at scheduled_at_utc,t.published_at_utc,t.status,
 json_extract(t.account_settings_json,'$.privacy_level') privacy_level,t.publish_id,t.unknown_outcome,
 json_extract(s.choice,'$.code') code,7000000000000000000+t.id route_queue_id,j.trigger_type,
 t.error_code,j.template_name,json_extract(v.config_json,'$.video_template') video_template
 FROM tt_auto_task t JOIN ttp_slot s ON s.native_run_id=t.run_id AND s.account_id=t.account_id
 JOIN ttp_job j ON s.job_id=j.id JOIN tt_auto_template tm ON tm.id=j.template_id
 JOIN tt_auto_template_version v ON v.template_id=j.template_id AND v.version=j.version;
"""


def install(path):
    with connection(path, write=True) as db:
        for statement in DDL.split(";"):
            sql = statement.strip()
            if not sql:
                continue
            if sql.startswith("CREATE VIEW IF NOT EXISTS "):
                definition = sql.replace("CREATE VIEW IF NOT EXISTS ", "CREATE VIEW ", 1)
                view = definition.split()[2]
                current = db.execute(
                    "SELECT sql FROM sqlite_master WHERE type='view' AND name=?", (view,)
                ).fetchone()
                if current and current["sql"] != definition:
                    db.execute("DROP VIEW " + view)
            db.execute(sql)
        # The reserved business ID range also separates GPU jobs, four-code
        # broker keys, immutable redirect files and campaign IDs from legacy.
        row = db.execute("SELECT seq FROM sqlite_sequence WHERE name='tt_auto_task'").fetchone()
        if not row:
            if db.execute("SELECT 1 FROM tt_auto_task LIMIT 1").fetchone():
                raise ValueError("独立数据库中发现未分配新版号段的任务")
            db.execute("INSERT INTO sqlite_sequence(name,seq) VALUES('tt_auto_task',?)", (TASK_BASE,))
        elif row[0] < TASK_BASE:
            raise ValueError("任务数据库与新版号段不一致")


class PreviewStore:
    """Read-only publishing simulations and resumable progress, outside the publication ledger."""

    def __init__(self, app_path):
        self.path = Path(app_path).with_name("preview.sqlite3")
        with contextlib.closing(sqlite3.connect(self.path, timeout=10)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS preview(
                    id TEXT PRIMARY KEY, tenant TEXT NOT NULL, author TEXT NOT NULL,template INTEGER NOT NULL,
                    version INTEGER NOT NULL, schedule_version INTEGER NOT NULL, created_at TEXT NOT NULL,
                    inputs TEXT NOT NULL, scope TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'working',
                    error TEXT NOT NULL DEFAULT '', lease TEXT NOT NULL DEFAULT '', lease_at TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS preview_row(
                    preview TEXT NOT NULL REFERENCES preview(id) ON DELETE CASCADE,
                    pos INTEGER NOT NULL,account TEXT NOT NULL,at TEXT NOT NULL,checked INTEGER NOT NULL DEFAULT 0,
                    result TEXT,PRIMARY KEY(preview,pos));
            """)

    def create(self, key, t, version, actor, inputs, channels, pairs):
        with connection(self.path, write=True) as db:
            # Completed snapshots remain reviewable for 7 days. Run comparisons are separately frozen.
            db.execute(
                "DELETE FROM preview WHERE created_at<? AND (state<>'working' OR lease_at<?)",
                (iso(now() - timedelta(days=7)), iso(now() - timedelta(days=1))),
            )
            inserted = db.execute(
                """INSERT OR IGNORE INTO preview(id,tenant,author,template,version,schedule_version,
                     created_at,inputs,scope) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    key,
                    actor["tenant_key"],
                    actor["user_id"],
                    t.id,
                    t.version,
                    version,
                    iso(now()),
                    dump(inputs),
                    dump(channels),
                ),
            ).rowcount
            if inserted:
                db.executemany(
                    "INSERT INTO preview_row(preview,pos,account,at) VALUES(?,?,?,?)",
                    [(key, pos, a, at) for pos, (a, at) in enumerate(pairs)],
                )

    def get(self, key, t, actor, visible):
        with connection(self.path) as db:
            r = db.execute(
                "SELECT * FROM preview WHERE id=? AND template=? AND tenant=? AND author=?",
                (key, t.id, actor["tenant_key"], actor["user_id"]),
            ).fetchone()
        if not r or not set(load(r["scope"])) <= set(visible):
            return None
        return dict(r)

    def rows(self, key):
        with connection(self.path) as db:
            return [
                dict(r) for r in db.execute("SELECT * FROM preview_row WHERE preview=? ORDER BY pos", (key,))
            ]

    def claim(self, key, token):
        with connection(self.path, write=True) as db:
            return db.execute(
                """UPDATE preview SET lease=?,lease_at=? WHERE id=? AND state='working'
                                  AND (lease='' OR lease_at<?)""",
                (token, iso(now()), key, iso(now() - timedelta(minutes=5))),
            ).rowcount

    def write_row(self, key, token, pos, item):
        with connection(self.path, write=True) as db:
            if not db.execute(
                "UPDATE preview SET lease_at=? WHERE id=? AND lease=?", (iso(now()), key, token)
            ).rowcount:
                return False
            db.execute(
                "UPDATE preview_row SET checked=1,result=? WHERE preview=? AND pos=?",
                (dump(item) if item else None, key, pos),
            )
            return True

    def finish(self, key, token, error):
        with connection(self.path, write=True) as db:
            db.execute(
                "UPDATE preview SET state=?,error=?,lease='',lease_at='' WHERE id=? AND lease=?",
                ("failed" if error else "ready", error, key, token),
            )

    def page(self, key, offset, limit):
        with connection(self.path) as db:
            meta = db.execute(
                "SELECT state,error,schedule_version FROM preview WHERE id=?", (key,)
            ).fetchone()
            counts = dict(
                db.execute(
                    """SELECT coalesce(json_extract(result,'$.status'),'pending'),count(*)
                FROM preview_row WHERE preview=? GROUP BY 1""",
                    (key,),
                )
            )
            rows = db.execute(
                "SELECT * FROM preview_row WHERE preview=? ORDER BY pos LIMIT ? OFFSET ?",
                (key, limit, offset),
            ).fetchall()
        items = [
            load(r["result"])
            if r["result"]
            else dict(
                account_id=r["account"],
                scheduled_at_utc=r["at"],
                status="pending",
                conflicts=[],
                reason="正在核对候选",
            )
            for r in rows
        ]
        summary = {k: counts.get(k, 0) for k in ("eligible", "skipped", "pending")}
        return dict(
            preview_id=key,
            state=meta["state"],
            error=meta["error"],
            template_version=meta["schedule_version"],
            summary=summary,
            items=items,
            total=sum(counts.values()),
            offset=offset,
            limit=limit,
            computed=summary["eligible"] + summary["skipped"],
        )
