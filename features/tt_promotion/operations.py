"""Read-only operational summaries for the independent TikTok promotion workspace."""

from collections import Counter
from datetime import date, timedelta
from types import SimpleNamespace
import re

from features.tt_auto_posts.legacy_reader import LEGACY_MATERIAL_TABLES
from .channels import ChannelError
from .pub_native import old_databases
from .pub_store import BEIJING, connection, dump, iso, load, now
from .publishing import clean_ids, day_slots, visible


BUCKETS = ("confirmed", "failed", "skipped", "review", "pending")
RESULT = """CASE
 WHEN t.unknown_outcome=1 OR t.status IN ('unknown','reconciling')
     OR (t.status='published' AND COALESCE(t.publish_id,'')='') THEN 'review'
 WHEN t.status='published' AND COALESCE(t.publish_id,'')<>'' THEN 'confirmed'
 WHEN COALESCE(t.status,s.state)='failed' THEN 'failed'
 WHEN COALESCE(t.status,s.state) IN ('skipped','missed','no_candidate','canceled','cancelled') THEN 'skipped'
 ELSE 'pending' END"""


def page_args(query):
    try:
        page = int(query.get("page", 1))
        size = int(query.get("page_size", 25))
    except (TypeError, ValueError):
        raise ChannelError(400, "分页参数无效") from None
    if not (1 <= page <= 1000000 and 1 <= size <= 100):
        raise ChannelError(400, "分页参数超出范围")
    return page, size


def date_bound(query, field):
    if not query.get(field):
        return None
    try:
        parsed = date.fromisoformat(query[field])
    except (ValueError, TypeError):
        raise ChannelError(400, "请输入有效日期") from None
    return parsed.isoformat()


def runs_query(actor, scope, filters):
    params = {"tenant": actor["tenant_key"], "accounts": dump(sorted(scope))}
    where = [
        "j.tenant=:tenant",
        """NOT EXISTS(
        SELECT 1 FROM ttp_slot hidden WHERE hidden.job_id=j.id AND hidden.account_id NOT IN
        (SELECT value FROM json_each(:accounts)))""",
    ]
    if filters.get("template_id"):
        template = str(filters["template_id"])
        if not re.fullmatch(r"[1-9][0-9]{0,17}", template):
            raise ChannelError(400, "模板号无效")
        where.append("j.template_id=:template")
        params["template"] = int(template)
    if filters.get("trigger_type"):
        if filters["trigger_type"] not in ("auto", "manual"):
            raise ChannelError(400, "触发方式无效")
        where.append("j.trigger_type=:trigger")
        params["trigger"] = filters["trigger_type"]
    start, end = date_bound(filters, "start"), date_bound(filters, "end")
    if start and end and start > end:
        raise ChannelError(400, "开始日期不能晚于结束日期")
    for value, sign, key in [(start, ">=", "start"), (end, "<=", "end")]:
        if value:
            where.append("date(j.created_at,'+8 hours')" + sign + ":" + key)
            params[key] = value
    match = []
    if filters.get("account_id"):
        params["account"] = str(filters["account_id"])
        match.append("account_id=:account")
    if filters.get("status"):
        if filters["status"] not in BUCKETS:
            raise ChannelError(400, "运行状态无效")
        params["status"] = filters["status"]
        match.append("bucket=:status")
    having = " HAVING MAX(CASE WHEN " + " AND ".join(match) + " THEN 1 ELSE 0 END)=1" if match else ""
    counts = ", ".join(f"SUM(bucket='{bucket}') AS {bucket}" for bucket in BUCKETS)
    sql = f"""WITH slots AS (
      SELECT j.id,j.template_id,j.template_name,j.version,j.trigger_type,j.created_at,
             s.account_id,s.scheduled_at,COALESCE(t.status,s.state) AS state,{RESULT} AS bucket
      FROM ttp_job j JOIN ttp_slot s ON s.job_id=j.id
      LEFT JOIN tt_auto_task t ON t.run_id=s.native_run_id AND t.account_id=s.account_id
      WHERE {" AND ".join(where)}
    ), grouped AS (
      SELECT id,template_id,template_name,version,trigger_type,created_at,
             MIN(scheduled_at) AS scheduled_at_utc,COUNT(*) AS expected,
             SUM(state IN ('planned','enqueued')) AS unstarted,{counts}
      FROM slots GROUP BY id{having}
    ) """
    return sql, params


def summary_row(row):
    item = dict(row)
    counts = {key: item.pop(key) for key in ("expected", *BUCKETS)}
    unstarted = item.pop("unstarted")
    if counts["review"]:
        status = "needs_review"
    elif unstarted == counts["expected"]:
        status = "planned"
    elif counts["pending"]:
        status = "running"
    elif counts["expected"] == counts["confirmed"]:
        status = "published"
    else:
        status = "completed_with_errors"
    return dict(item, count=counts["expected"], counts=counts, status=status)


def history(db, actor, scope, query):
    page, size = page_args(query)
    sql, params = runs_query(actor, scope, query)
    total = db.execute(sql + "SELECT COUNT(*) FROM grouped", params).fetchone()[0]
    rows = db.execute(
        sql + "SELECT * FROM grouped ORDER BY id DESC LIMIT :size OFFSET :skip",
        dict(params, size=size, skip=(page - 1) * size),
    )
    return dict(items=[summary_row(row) for row in rows], total=total, page=page, page_size=size)


def next_time(db, template, current):
    today = current.astimezone(BEIJING).date()
    cutoff = iso(current)
    for day in (today, today + timedelta(days=1)):
        day_key = day.isoformat()
        frozen = db.execute(
            "SELECT slots FROM ttp_day WHERE template_id=? AND day=?", (template.id, day_key)
        ).fetchone()
        if frozen:
            times = (at for _, at in load(frozen["slots"]))
        else:
            times = (
                at
                for channel in template.config["account_ids"]
                for at in day_slots(template, channel, day_key)
            )
        earliest = min((at for at in times if at >= cutoff), default=None)
        if earliest:
            return earliest
    return None


def templates(db, actor, scope):
    rows = db.execute("""SELECT t.id,t.enabled,t.updated_by_name,v.version,v.config_json
        FROM tt_auto_template t JOIN tt_auto_template_version v
        ON v.template_id=t.id AND v.version=t.current_version""")
    for row in rows:
        template = SimpleNamespace(
            id=row["id"],
            enabled=bool(row["enabled"]),
            operator_name=row["updated_by_name"],
            version=row["version"],
            config=load(row["config_json"]),
        )
        if visible(template, actor, scope):
            yield template


def template_stats(db, actor, scope):
    sql, params = runs_query(actor, scope, {})
    latest = {}
    running = {}
    for row in db.execute(
        sql
        + """SELECT g.*, r.running FROM grouped g JOIN
            (SELECT template_id, MAX(id) last_id,
                    MAX(review=0 AND pending>0 AND unstarted<expected) running
             FROM grouped GROUP BY template_id) r ON r.last_id=g.id""",
        params,
    ):
        result = summary_row(row)
        latest[row["template_id"]] = dict(id=row["id"], **result["counts"], status=result["status"],
                                          created_at=row["created_at"])
        running[row["template_id"]] = bool(row["running"])
    current = now()
    return {
        "items": [
            dict(
                template_id=t.id,
                operator_name=t.operator_name,
                running=running.get(t.id, False),
                next_at=next_time(db, t, current) if t.enabled else None,
                latest=latest.get(t.id),
            )
            for t in templates(db, actor, scope)
        ]
    }


def inventory(db, actor, scope, template_id, query, legacy_paths=None):
    if not any(t.id == template_id for t in templates(db, actor, scope)):
        raise ChannelError(404, "模板不存在或无权限")
    page, size = page_args(query)
    pool = {
        r[0]: dict(material_id=r[0], status="available", account_id=None, job_id=None)
        for r in db.execute(
            "SELECT material_id FROM ttp_pool WHERE template_id=? ORDER BY length(material_id),material_id",
            (template_id,),
        )
    }
    rank = {"available": 0, "occupied": 1, "published": 2, "review": 3}

    def mark(material, status, account=None, job=None):
        material = str(material)
        if material in pool and rank[status] > rank[pool[material]["status"]]:
            pool[material].update(status=status, account_id=account, job_id=job)

    def native_state(row):
        if (
            row["unknown_outcome"]
            or row["status"] in ("reconciling", "unknown")
            or (row["status"] == "published" and not row["publish_id"])
        ):
            return "review"
        return "published" if row["status"] == "published" and row["publish_id"] else "occupied"

    if pool:
        ids = dump(list(pool))
        selected = "(SELECT value FROM json_each(?))"
        rows = db.execute(
            f"""SELECT s.material_id,s.account_id,j.tenant,j.id,t.status,t.unknown_outcome,t.publish_id
            FROM ttp_slot s JOIN ttp_job j ON j.id=s.job_id
            LEFT JOIN tt_auto_task t ON t.run_id=s.native_run_id AND t.account_id=s.account_id
            WHERE s.material_id IN {selected}""",
            (ids,),
        )
        for row in rows:
            readable = row["tenant"] == actor["tenant_key"] and row["account_id"] in scope
            mark(
                row["material_id"],
                native_state(row),
                row["account_id"] if readable else None,
                row["id"] if readable else None,
            )
        auto, old_pool = legacy_paths or old_databases()
        with connection(auto) as old, connection(old_pool) as legacy:
            for row in old.execute(
                f"""SELECT m.material_id,t.status,t.unknown_outcome,t.publish_id
                    FROM tt_auto_material_ledger m LEFT JOIN tt_auto_task t ON t.id=m.task_id
                    WHERE m.material_id IN {selected}""",
                (ids,),
            ):
                mark(row["material_id"], native_state(row))
            for table in LEGACY_MATERIAL_TABLES:
                if table in ("tt_post_queue", "tt_post_direct_test"):
                    for row in legacy.execute(
                        f"SELECT material_id,status,publish_id,unknown_outcome FROM {table} WHERE material_id IN {selected}",
                        (ids,),
                    ):
                        mark(row["material_id"], native_state(row))
                else:
                    for row in legacy.execute(
                        f"SELECT DISTINCT material_id FROM {table} WHERE material_id IN {selected}", (ids,)
                    ):
                        mark(row[0], "occupied")
    items = list(pool.values())
    counts = Counter(x["status"] for x in items)
    status = query.get("status")
    if status:
        if status not in rank:
            raise ChannelError(400, "素材占用状态无效")
        items = [x for x in items if x["status"] == status]
    start = (page - 1) * size
    return dict(
        items=items[start : start + size],
        counts={key: counts[key] for key in rank},
        total=len(items),
        page=page,
        page_size=size,
    )


def add_inventory(database, template_id, args, actor, scope):
    ids = clean_ids(args.get("material_ids"), "素材池", maximum=1000)
    with connection(database, write=True) as db:
        template = next((t for t in templates(db, actor, scope) if t.id == template_id), None)
        if template is None:
            raise ChannelError(404, "模板不存在或无权限")
        if type(args.get("expected_version")) is not int or template.version != args["expected_version"]:
            raise ChannelError(409, "模板已更新，请刷新")
        before = db.total_changes
        db.executemany(
            "INSERT OR IGNORE INTO ttp_pool(template_id,material_id) VALUES(?,?)",
            ((template_id, material) for material in ids),
        )
        added = db.total_changes - before
        total = db.execute("SELECT count(*) FROM ttp_pool WHERE template_id=?", (template_id,)).fetchone()[
            0
        ]
    return dict(added=added, total=total)


def read(path, args, actor, scope, database):
    with connection(database) as db:
        db.execute("BEGIN")
        if path == "/history":
            return history(db, actor, scope, args)
        if path == "/stats/templates":
            return template_stats(db, actor, scope)
        return inventory(db, actor, scope, int(path.split("/")[2]), args)
