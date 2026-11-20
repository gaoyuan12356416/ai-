"""Explicit final-material selection and its durable, owner-scoped delivery ledger."""
import json
import logging
import re
import threading
import time
import uuid

from .runtime import Fault, check, owner, packed
from .youtube_material_io import MaterialIO, resolve_drama


LOG = logging.getLogger(__name__)
TABLE = "ai_youtube_final_material"
TASK = r"at_[0-9a-f]{32}"


def setup(rt):
    with rt.db(True) as db:
        db.execute(f"""CREATE TABLE IF NOT EXISTS {TABLE} (
            task_id TEXT PRIMARY KEY, owner TEXT NOT NULL, version INTEGER NOT NULL,
            sha256 TEXT NOT NULL, snapshot TEXT NOT NULL, state TEXT NOT NULL,
            material_id TEXT UNIQUE, receipt_id TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '', attempts INTEGER NOT NULL DEFAULT 0,
            copy_failures INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL DEFAULT 0,
            lease TEXT NOT NULL DEFAULT '', expires REAL NOT NULL DEFAULT 0,
            created REAL NOT NULL, submitted REAL NOT NULL DEFAULT 0)""")
        db.execute(f"CREATE INDEX IF NOT EXISTS ai_youtube_final_due ON {TABLE}(state,next_at)")


def unpack(row):
    return dict(json.loads(row["snapshot"]), task_id=row["task_id"], version=row["version"],
                state=row["state"], sha256=row["sha256"], material_id=row["material_id"] or "",
                receipt_id=row["receipt_id"], error=row["error"], attempts=row["attempts"],
                copy_failures=row["copy_failures"], submitted=row["submitted"], lease=row["lease"])


def dto(item):
    return {k: item[k] for k in ("task_id", "version", "sha256", "width", "height", "duration_ms",
                                  "state", "drama", "material_id", "error")}


def frozen_task(rt, actor, tid):
    with rt.db() as db:
        row = db.execute("SELECT * FROM tasks WHERE id=? AND owner=?", (tid, owner(actor))).fetchone()
    check(row is not None, "task_missing", "未找到当前账号的剪辑作品", 404)
    check(row["status"] == "completed", "material_not_ready", "请等待成片制作完成后再定稿", 409)
    unit = json.loads(row["unit_data"])
    regular = unit.get("youtube_regular")
    check(isinstance(regular, dict), "regular_video_required", "请选择 YouTube 普通视频模式制作的作品", 409)
    meta = json.loads(row["meta"])
    sha = str(meta.get("sha256") or "")
    check(re.fullmatch(r"[0-9a-f]{64}", sha), "material_unverified", "成片尚无有效的校验记录", 409)
    return dict(task_id=tid, version=row["version"], sha256=sha, width=meta["width"], height=meta["height"],
                duration_ms=meta["duration_ms"], state="draft", error="", material_id=""), unit


def association(rt, actor, unit):
    regular = unit["youtube_regular"]
    ids = list(dict.fromkeys(regular["body_source_ids"]))
    check(ids and len(ids) <= 300, "material_source_missing", "未找到正片素材", 409)
    with rt.db() as db:
        rows = db.execute("SELECT DISTINCT product,source_key FROM catalog_imports WHERE owner=? "
                          "AND asset_id IN (" + ",".join("?" for _ in ids) + ") AND status='ready'",
                          (owner(actor), *ids)).fetchall()
    check(len(rows) <= 1, "material_multiple_dramas", "正片关联了多部剧或不同语种，请先调整正片", 409)
    fixed = regular.get("drama") or {}
    if rows:
        row = rows[0]
        check(not fixed or (row["product"], row["source_key"]) ==
              (fixed.get("product"), fixed.get("source_key")), "material_drama_conflict",
              "正片来源与作品剧目不一致，请重新确认", 409)
        return dict(product=row["product"], source_key=row["source_key"],
                    title=fixed.get("title") or "", language=fixed.get("language") or "")
    if fixed.get("product") and fixed.get("source_key"):
        return {k: fixed.get(k, "") for k in ("product", "source_key", "title", "language")}
    return None


def observations(rt, actor, unit):
    """Only use observations fully visible in the FINAL cuts, never a full-show synopsis."""
    aid = unit["youtube_regular"].get("analysis_id")
    if not aid:
        return []
    from . import smart_api
    try:
        record = smart_api.receipt(rt, actor, aid)
    except (Fault, smart_api.SmartError):
        return []
    if record["status"] != "succeeded":
        return []
    by_source = {s["source_id"]: s["candidates"] for s in record["sources"]}
    result, seen = [], set()
    offset = 0
    for cut in unit["clips"]:
        sid, start, end = cut["source_id"], cut["start_ms"], cut["end_ms"]
        for point in by_source.get(sid, ()):
            left, right = point["start_ms"], point["end_ms"]
            key = sid, left, right
            if (point.get("engine") != "multimodal" or not start <= left < right <= end
                    or key in seen or not (point.get("reason") or point.get("transcript"))):
                continue
            seen.add(key)
            result.append(dict(source_id=sid, start_ms=left, end_ms=right, output_ms=offset+left-start,
                               description=str(point.get("reason") or "")[:400],
                               transcript=str(point.get("transcript") or "")[:400]))
        offset += end-start
    if len(result) > 120:
        return [result[round(i*(len(result)-1)/119)] for i in range(120)]
    return result


class Workflow:
    def __init__(self, rt, catalog, io):
        self.rt, self.catalog, self.io = rt, catalog, io
        self.wake = threading.Event()
        setup(rt)

    def saved(self, actor, tid):
        with self.rt.db() as db:
            row = db.execute(f"SELECT * FROM {TABLE} WHERE task_id=? AND owner=?", (tid, owner(actor))).fetchone()
        return unpack(row) if row else None

    def status(self, actor, tid):
        previous = self.saved(actor, tid)
        if previous:
            return dto(previous)
        item, unit = frozen_task(self.rt, actor, tid)
        item["drama"] = association(self.rt, actor, unit)
        return dto(item)

    def finalize(self, actor, tid, body):
        previous = self.saved(actor, tid)
        if previous:
            drama = previous["drama"]
            supplied_country = body.get("country")
            check((not body.get("key") or body["key"] == drama["source_key"])
                  and (not body.get("product") or body["product"] == drama["product"])
                  and (not supplied_country or str(supplied_country).strip().upper() == drama["country"]),
                  "material_drama_changed", "该版本已按另一组归属提交", 409)
            return dto(previous)
        item, unit = frozen_task(self.rt, actor, tid)
        known = association(self.rt, actor, unit)
        product = body.get("product") or (known or {}).get("product")
        key = body.get("key") or (known or {}).get("source_key")
        country = body.get("country", "")
        check(isinstance(country, str) and len(country) <= 2,
              "material_country_invalid", "国家或地区应为两位代码")
        check(not known or (known["product"], known["source_key"]) == (product, key),
              "material_drama_conflict", "入库剧目须与正片关联的原剧一致", 409)
        drama = resolve_drama(self.catalog, product, key, country)
        path = self.rt.root/"outputs"/(tid+".mp4")
        check(path.is_file() and path.stat().st_size > 0, "material_file_missing", "成片文件暂不可读取", 409)
        snapshot = dict(drama=drama, request=self.io.request(item, drama, actor), size=path.stat().st_size,
                        evidence=observations(self.rt, actor, unit),
                        **{k: item[k] for k in ("width", "height", "duration_ms")})
        with self.rt.db(True) as db:
            db.execute(f"""INSERT INTO {TABLE}(task_id,owner,version,sha256,snapshot,state,created)
                VALUES(?,?,?,?,?,'queued',?) ON CONFLICT(task_id) DO NOTHING""",
                       (tid, owner(actor), item["version"], item["sha256"], packed(snapshot), time.time()))
            row = db.execute(f"SELECT * FROM {TABLE} WHERE task_id=? AND owner=?", (tid, owner(actor))).fetchone()
            check(row is not None, "material_missing", "定稿不存在", 404)
            saved = unpack(row)
            check((saved["drama"]["product"], saved["drama"]["source_key"], saved["drama"]["country"]) ==
                  (product, key, drama["country"]), "material_drama_changed", "该版本已按另一组归属提交", 409)
        self.wake.set()
        return dto(saved)

    def retry(self, actor, tid):
        with self.rt.db(True) as db:
            row = db.execute(f"SELECT * FROM {TABLE} WHERE task_id=? AND owner=?", (tid, owner(actor))).fetchone()
            check(row is not None, "material_missing", "定稿不存在", 404)
            if row["state"] == "failed":
                db.execute(f"UPDATE {TABLE} SET state='queued',copy_failures=0,next_at=0,error='' WHERE task_id=?", (tid,))
        self.wake.set()
        return self.status(actor, tid)

    def evidence(self, actor, mid):
        with self.rt.db() as db:
            row = db.execute(f"SELECT * FROM {TABLE} WHERE material_id=? AND state='ready' AND owner=?",
                             (mid, owner(actor))).fetchone()
        check(row is not None, "evidence_missing", "未找到当前账号的定稿素材", 404)
        item = unpack(row)
        return {**{k: item[k] for k in ("material_id", "task_id", "version", "sha256", "width",
                                         "height", "duration_ms", "evidence")},
                **{k: item["drama"][k] for k in ("product", "product_id", "content_id", "language")},
                "url": item["request"]["url"], "size_bytes": item["size"]}

    def claim(self):
        now = time.time()
        token = uuid.uuid4().hex
        with self.rt.db(True) as db:
            row = db.execute(f"""SELECT * FROM {TABLE} WHERE state IN ('queued','copying','checking','unknown')
                AND next_at<=? AND expires<=? ORDER BY next_at,created LIMIT 1""", (now, now)).fetchone()
            if not row:
                return None
            state = "copying" if row["state"] == "queued" else row["state"]
            db.execute(f"UPDATE {TABLE} SET lease=?,expires=?,state=? WHERE task_id=?",
                       (token, now+120, state, row["task_id"]))
            return dict(unpack(row), lease=token, state=state)

    def update(self, item, state, error="", delay=0, *, keep=False, **fields):
        # All keys passed here are internal column names. A late worker can never
        # cross the outbound POST boundary after its lease has been replaced.
        now = time.time()
        data = dict(fields, state=state, error=error, next_at=now+delay,
                    lease=item["lease"] if keep else "", expires=now+120 if keep else 0)
        with self.rt.db(True) as db:
            return db.execute(f"UPDATE {TABLE} SET "+",".join(k+"=?" for k in data)
                              +" WHERE task_id=? AND lease=?",
                              (*data.values(), item["task_id"], item["lease"])).rowcount == 1

    def reconcile(self, item):
        mid, rid, error = self.io.formal(item)
        if mid:
            self.update(item, "ready", material_id=mid, receipt_id=rid)
        else:
            state = "unknown" if (error or item["state"] == "unknown"
                                  or time.time()-item["submitted"] > 1800) else "checking"
            self.update(item, state, error or ("正在核对原入库回执" if state == "unknown" else ""), 60)

    def deliver(self, item):
        if item["state"] in ("checking", "unknown"):
            self.reconcile(item)
            return
        self.io.ensure_file(self.rt.root/"outputs"/(item["task_id"]+".mp4"), item)
        # A recovered/restored ledger must check existing formal records before
        # making its first remote write. Existing conflicting rows remain local.
        mid, rid, problem = self.io.formal(item)
        if mid:
            self.update(item, "ready", material_id=mid, receipt_id=rid)
            return
        if problem:
            self.update(item, "unknown", problem, 60)
            return
        when = time.time()
        if not self.update(item, "unknown", "正在核对素材接口回执", 60, keep=True,
                           attempts=item["attempts"]+1, submitted=when):
            return
        # From this point a network interruption is an unresolved remote write.
        # Only a documented, definitive rejection is eligible for user retry.
        item["state"], item["submitted"] = "unknown", when
        kind, rid, message = self.io.submit(item["request"])
        if kind == "failed":
            self.update(item, "failed", message)
        elif kind == "accepted":
            if self.update(item, "checking", keep=True, receipt_id=rid):
                self.reconcile(dict(item, state="checking", receipt_id=rid))
        else:
            self.update(item, "unknown", message or "正在核对原入库回执", 60)

    def tick(self):
        item = self.claim()
        if not item:
            return False
        stop = threading.Event()
        def heartbeat():
            while not stop.wait(30):
                try:
                    with self.rt.db(True) as db:
                        if not db.execute(f"UPDATE {TABLE} SET expires=? WHERE task_id=? AND lease=?",
                                          (time.time()+120, item["task_id"], item["lease"])).rowcount:
                            return
                except Exception:
                    LOG.warning("Final-material lease renewal delayed for %s", item["task_id"])
        thread = threading.Thread(target=heartbeat, name="youtube-final-heartbeat", daemon=True)
        thread.start()
        try:
            self.deliver(item)
        except Exception as exc:
            if item["state"] in ("checking", "unknown"):
                self.update(item, "unknown", "正在核对素材库原回执", 60)
            else:
                n = item["copy_failures"]+1
                permanent = getattr(exc, "status", None) in (400, 403, 404, 409, 422)
                retry = n < 3 and not permanent
                message = getattr(exc, "message", None) or "素材传输或校验暂未完成"
                self.update(item, "copying" if retry else "failed", message[:240], n*15,
                            copy_failures=n)
            LOG.warning("Final-material stage failed for %s (%s)", item["task_id"], type(exc).__name__)
        finally:
            stop.set()
            thread.join(1)
        return True

    def run(self):
        while True:
            try:
                if self.tick():
                    continue
            except Exception:
                LOG.exception("Final-material worker iteration failed")
            self.wake.wait(5)
            self.wake.clear()


def start(rt, catalog, platform):
    flow = Workflow(rt, catalog, MaterialIO(platform))
    rt.youtube_material = flow
    threading.Thread(target=flow.run, name="youtube-final-material", daemon=True).start()


def dispatch(rt, actor, verb, path, body, params):
    workflow = rt.youtube_material
    m = re.fullmatch(r"/youtube-material/tasks/("+TASK+r")(/finalize|/retry)?", path)
    if m:
        if verb == "GET" and not m[2]:
            return 200, {"item": workflow.status(actor, m[1])}
        if verb == "POST" and m[2] == "/finalize":
            return 200, {"item": workflow.finalize(actor, m[1], body)}
        if verb == "POST" and m[2] == "/retry":
            return 200, {"item": workflow.retry(actor, m[1])}
    m = re.fullmatch(r"/youtube-material/evidence/([1-9][0-9]{0,18})", path)
    if m and verb == "GET":
        return 200, {"item": workflow.evidence(actor, m[1])}
    raise Fault("not_found", "接口不存在", 404)
