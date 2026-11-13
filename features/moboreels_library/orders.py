"""Durable Moboreels financial snapshots. Source dates have no confirmed timezone."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import threading
import time
import uuid

import requests

from .db import Database, _json_expr, _literal, _utc_datetime
from .promotions import identity
from .provider import ProviderClient
from .sync import _process_lock, _save_json, utc_now


ORDER_TABLE = "ads_ai.cdreader_koc_order"
AD_TABLE = "ads_ai.cdreader_koc_ad_revenue"
AD_DAY_TABLE = "ads_ai.cdreader_koc_ad_day_snapshot"
ORDER_COLUMNS = ("row_key", "institution_user_id", "project_type", "ref_order_id", "source_create_time",
                 "source_date", "code", "story_id", "story_name", "amount_usd", "source_project_type",
                 "source_status_text", "coo_order_status", "coo_order_status_raw", "star_user_id",
                 "real_star_user_id", "classification", "classification_reason", "raw_json")
AD_COLUMNS = ("row_key", "institution_user_id", "project_type", "snapshot_id", "ad_time", "code", "amount_cents",
              "amount_usd", "raw_json")
TIME_COLUMNS = ("first_seen_at", "last_seen_at", "last_sync_run_id")
DAY_MS = 86400000


class ReportError(RuntimeError):
    pass


def _text(value, field, maximum, required=False):
    if value is None:
        value = ""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("统计字段 %s 格式无效" % field)
    value = str(value)
    if len(value) > maximum or (required and not value.strip()):
        raise ValueError("统计字段 %s 长度无效" % field)
    return value


def _amount(value):
    try:
        if isinstance(value, bool) or value is None:
            raise ValueError()
        result = Decimal(str(value))
        if not result.is_finite() or abs(result) >= Decimal("1e16") or result.as_tuple().exponent < -8:
            raise ValueError()
        return format(result, "f")
    except (ValueError, InvalidOperation):
        raise ValueError("统计金额格式或精度无效") from None


def _key(*values):
    return hashlib.sha256(json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def normalize_order(raw, institution_user_id, recharge_ids=(), refund_ids=()):
    if not isinstance(raw, dict):
        raise ValueError("订单记录必须为对象")
    ref = _text(raw.get("ref_order_id"), "ref_order_id", 255, True)
    source_institution = raw.get("institution_user_id")
    if source_institution not in (None, "", institution_user_id):
        raise ValueError("订单机构与当前配置不匹配")
    source_time = _text(raw.get("create_time"), "create_time", 64)
    source_date = None
    try:
        parsed = datetime.fromisoformat(source_time.replace(" ", "T", 1))
        # The documented value is a naive source wall clock, never a Beijing/UTC day.
        if parsed.tzinfo is None and len(source_time) >= 19:
            source_date = parsed.date().isoformat()
    except ValueError:
        pass
    source_state = raw.get("coo_order_status")
    state = int(source_state) if not isinstance(source_state, bool) and str(source_state).lstrip("-").isdigit() else None
    if state is not None and not -2147483648 <= state <= 2147483647:
        state = None
    amount = _amount(raw.get("amount"))
    recharge, refund = ref in recharge_ids, ref in refund_ids
    classification, reason = -1, "status_not_matched"
    if recharge and refund:
        reason = "status_filter_conflict"
    elif state != 1:
        reason = "unknown_order_state"
    elif raw.get("project_type") not in (2, "2", "短剧"):
        reason = "unknown_business_type"
    elif source_date is None:
        reason = "unknown_source_time"
    elif recharge and Decimal(amount) < 0:
        reason = "unexpected_recharge_sign"
    elif recharge or refund:
        classification, reason = (0 if recharge else 1), "confirmed_by_status_filter"
    return {
        "row_key": _key(institution_user_id, 2, ref), "institution_user_id": institution_user_id,
        "project_type": 2, "ref_order_id": ref, "source_create_time": source_time, "source_date": source_date,
        "code": _text(raw.get("code"), "code", 64), "story_id": _text(raw.get("story_id"), "story_id", 64),
        "story_name": _text(raw.get("story_name"), "story_name", 65535), "amount_usd": amount,
        "source_project_type": json.dumps(raw.get("project_type"), ensure_ascii=False),
        "source_status_text": _text(raw.get("status_text"), "status_text", 255), "coo_order_status": state,
        "coo_order_status_raw": json.dumps(source_state, ensure_ascii=False),
        "star_user_id": _text(raw.get("star_user_id"), "star_user_id", 255),
        "real_star_user_id": _text(raw.get("real_star_user_id"), "real_star_user_id", 128),
        "classification": classification, "classification_reason": reason, "raw_json": deepcopy(raw),
    }


def get_order_page(provider, begin_time, end_time, status, page_index, page_size=100):
    if not (isinstance(begin_time, int) and isinstance(end_time, int)
            and 10 ** 12 <= begin_time < end_time < 10 ** 13 and end_time - begin_time < 31 * DAY_MS):
        raise ValueError("订单查询必须为13位毫秒时间戳，且范围小于31天")
    if status not in (-1, 0, 1) or page_index < 1 or not 1 <= page_size <= 100:
        raise ValueError("订单分页或状态无效")
    body = {"begin_time": begin_time, "end_time": end_time, "status": status, "project_type": 2,
            "page_index": page_index, "page_size": page_size}
    for attempt in range(3):
        try:
            response = provider._post("/api/story/getorder", body)
            if response.status_code == 429 or response.status_code >= 500:
                raise requests.RequestException("temporary response")
            payload = response.json(parse_float=str)
            if response.status_code != 200 or not isinstance(payload, dict) or payload.get("code") != 200:
                raise ReportError("订单接口拒绝请求")
            data = payload.get("data")
            if not isinstance(data, dict) or not isinstance(data.get("list"), list):
                raise ReportError("订单接口返回格式异常")
            return data
        except (requests.RequestException, ValueError):
            if attempt == 2:
                raise ReportError("订单接口暂不可用") from None
            time.sleep(attempt + 1)


def _canonical(value):
    if isinstance(value, str):
        value = json.loads(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _same_order_observation(left, right):
    # Each status query is a separate read. Never combine a previous amount or
    # attribution with a newer filter membership after an order changes state.
    fields = ("institution_user_id", "story_id", "code", "real_star_user_id",
              "star_user_id", "coo_order_status", "status_text", "project_type", "create_time")
    return (Decimal(_amount(left.get("amount"))) == Decimal(_amount(right.get("amount")))
            and _canonical({key: left.get(key) for key in fields})
            == _canonical({key: right.get(key) for key in fields}))


class ReportStore:
    """Fixed financial tables; reuse the gated reader and scoped transactional writer machinery."""
    def __init__(self, settings, db=None):
        self.settings = settings
        self.reader = db or Database(settings)
        self.writer = Database(settings)

    @staticmethod
    def table(kind):
        if kind == "orders":
            return ORDER_TABLE, ORDER_COLUMNS
        if kind == "ad":
            return AD_TABLE, AD_COLUMNS
        raise ValueError("统计数据类型无效")

    def scope(self):
        return "institution_user_id=%s AND project_type=2" % _literal(self.settings.institution_user_id)

    def read_records(self, kind, keys):
        table, columns = self.table(kind)
        if not keys:
            return {}
        if len(keys) > 500:
            raise ValueError("统计读取批次最多500条")
        expression = "JSON_OBJECT(" + ",".join("'%s',%s" % (name, "CAST(`%s` AS CHAR)" % name if name.startswith("amount_") else "`%s`" % name)
                                              for name in columns + TIME_COLUMNS) + ")"
        rows = self.reader._read("SELECT %s FROM %s WHERE %s AND row_key IN (%s);" % (
            expression, table, self.scope(), ",".join(_literal(key) for key in keys)))
        return {row["row_key"]: row for row in rows}

    @staticmethod
    def same(kind, observed, desired, run_id=None):
        if not observed or (run_id is not None and observed.get("last_sync_run_id") != run_id):
            return False
        _, columns = ReportStore.table(kind)
        for name in columns:
            if name == "raw_json":
                if _canonical(observed.get(name)) != _canonical(desired[name]):
                    return False
            elif name.startswith("amount_"):
                if Decimal(str(observed.get(name))) != Decimal(str(desired[name])):
                    return False
            elif observed.get(name) != desired.get(name):
                return False
        return True

    def write_records(self, kind, records, run_id, seen_at):
        table, business_columns = self.table(kind)
        if not records:
            return
        if len(records) > 20:
            raise ValueError("统计写入事务最多20条")
        columns = business_columns + TIME_COLUMNS
        values = []
        for record in records:
            if record.get("institution_user_id") != self.settings.institution_user_id or record.get("project_type") != 2:
                raise ValueError("统计机构或业务类型不匹配")
            row = dict(record)
            row.update(first_seen_at=_utc_datetime(seen_at), last_seen_at=_utc_datetime(seen_at), last_sync_run_id=run_id)
            row["raw_json"] = json.dumps(row["raw_json"], ensure_ascii=False, separators=(",", ":"))
            values.append(tuple(row.get(name) for name in columns))
        updates = [name for name in columns if name not in ("row_key", "institution_user_id", "project_type", "first_seen_at")]
        sql = "INSERT INTO %s (%s) VALUES (%s) ON DUPLICATE KEY UPDATE %s" % (
            table, ",".join("`%s`" % name for name in columns), ",".join(["%s"] * len(columns)),
            ",".join("`%s`=VALUES(`%s`)" % (name, name) for name in updates))
        self.writer._write(sql, values, many=True, row_count=len(values))

    def touch_records(self, kind, keys, run_id, seen_at):
        table, _ = self.table(kind)
        if not keys:
            return
        if len(keys) > 500:
            raise ValueError("统计标记更新批次最多500条")
        sql = "UPDATE %s SET last_seen_at=%%s,last_sync_run_id=%%s WHERE institution_user_id=%%s AND project_type=2 AND row_key IN (%s)" % (
            table, ",".join(["%s"] * len(keys)))
        self.writer._write(sql, [_utc_datetime(seen_at), run_id, self.settings.institution_user_id] + list(keys), row_count=len(keys))

    def close_writer(self):
        self.writer.close_writer()

    def publish_ad_day(self, day, run_id, row_count, source_hash, finished_at):
        # Only called after the entire immutable version was read back successfully.
        sql = "INSERT INTO %s (institution_user_id,project_type,ad_time,snapshot_id,row_count,source_hash,completed_at) VALUES (%%s,2,%%s,%%s,%%s,%%s,%%s) ON DUPLICATE KEY UPDATE snapshot_id=VALUES(snapshot_id),row_count=VALUES(row_count),source_hash=VALUES(source_hash),completed_at=VALUES(completed_at)" % AD_DAY_TABLE
        values = (self.settings.institution_user_id, day, run_id, row_count, source_hash, _utc_datetime(finished_at))
        for attempt in range(3):
            try:
                self.writer._write(sql, values)
            except Exception:
                self.close_writer()
            # An acknowledged pointer also needs replica verification before a job succeeds.
            for check in range(3):
                rows = self.reader._read("SELECT JSON_OBJECT('snapshot_id',snapshot_id,'row_count',row_count,'source_hash',source_hash) FROM %s WHERE %s AND ad_time=%s;" % (AD_DAY_TABLE, self.scope(), _literal(day)))
                if rows and rows[0] == {"snapshot_id": run_id, "row_count": row_count, "source_hash": source_hash}:
                    return
                if check < 2:
                    time.sleep(2)
            if attempt < 2:
                time.sleep(2)
        raise ReportError("广告日快照切换结果待核对，历史有效版本保留")

    def query(self, params=None, star_id=None):
        return _query_report(self, params or {}, star_id)


def _period(params=None):
    params = params or {}
    today = datetime.now(timezone.utc).date()
    yesterday = today - timedelta(days=1)
    start = date.fromisoformat(str(params.get("date_from") or (yesterday - timedelta(days=30)).isoformat()))
    end = date.fromisoformat(str(params.get("date_to") or yesterday.isoformat()))
    if not start <= end or (end - start).days >= 31 or end > today:
        raise ValueError("统计日期范围必须为31天内且不晚于今天")
    return start, end


class Reports:
    """One durable job coordinates orders and independent daily ad snapshots."""
    def __init__(self, settings, db=None, provider=None, store=None, recover=True):
        self.settings = settings
        self.store = store or ReportStore(settings, db)
        self.provider = provider or ProviderClient(settings)
        self.root = settings.data_dir
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.snapshots = self.root / "report-runs"
        self.snapshots.mkdir(exist_ok=True, mode=0o700)
        self.db_path = self.root / "reports.sqlite3"
        self._lock = threading.Lock()
        self._thread = None
        with self._transaction() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS report_run(run_id TEXT PRIMARY KEY,payload TEXT NOT NULL)")
        if recover:
            active = next((job for job in self._jobs() if job["status"] in ("queued", "running")), None)
            if active:
                self._launch(active["run_id"])

    @contextmanager
    def _transaction(self):
        connection = sqlite3.connect(str(self.db_path), timeout=15)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _jobs(self, connection=None):
        if connection is None:
            with self._transaction() as connection:
                return self._jobs(connection)
        return [json.loads(row[0]) for row in connection.execute("SELECT payload FROM report_run ORDER BY rowid DESC")]

    def get(self, run_id):
        with self._transaction() as connection:
            row = connection.execute("SELECT payload FROM report_run WHERE run_id=?", (str(run_id),)).fetchone()
        return json.loads(row[0]) if row else None

    def latest(self):
        jobs = self._jobs()
        return jobs[0] if jobs else None

    def _update(self, run_id, **changes):
        with self._transaction() as connection:
            row = connection.execute("SELECT payload FROM report_run WHERE run_id=?", (run_id,)).fetchone()
            job = json.loads(row[0])
            job.update(changes)
            job["updated_at"] = utc_now()
            connection.execute("UPDATE report_run SET payload=? WHERE run_id=?", (json.dumps(job, ensure_ascii=False), run_id))
        return job

    def start(self, params=None, trigger="manual", actor=None):
        if trigger not in ("manual", "scheduled"):
            raise ValueError("统计同步触发类型无效")
        start, end = _period(params)
        now_ms = int(time.time() * 1000)
        # Match the UI/source-date window for both scheduled and explicit runs.
        # A rolling 31-day interval would omit the first selected day's opening hours.
        end_ms = min(now_ms, int(datetime.combine(end + timedelta(days=1), datetime.min.time(), timezone.utc).timestamp() * 1000) - 1)
        begin_ms = int(datetime.combine(start, datetime.min.time(), timezone.utc).timestamp() * 1000)
        dates = [(start + timedelta(days=index)).isoformat() for index in range((end - start).days + 1)
                 if start + timedelta(days=index) < datetime.now(timezone.utc).date()]
        with self._transaction() as connection:
            jobs = self._jobs(connection)
            job = next((row for row in jobs if row["status"] in ("queued", "running")), None)
            previous = jobs[0] if jobs else None
            if job is None and previous and previous["status"] == "failed" and previous["date_from"] == start.isoformat() and previous["date_to"] == end.isoformat():
                job = previous
                job.update(status="queued", error=None, finished_at=None, resume_count=job.get("resume_count", 0) + 1)
                for part in job["parts"]:
                    if part["status"] != "succeeded":
                        part.update(status="queued", error=None)
                        complete = self.snapshots / job["run_id"] / (part["name"] + "-complete.json")
                        if not complete.exists():
                            part["fetch_attempt"] = part.get("fetch_attempt", 0) + 1
                connection.execute("UPDATE report_run SET payload=? WHERE run_id=?", (json.dumps(job, ensure_ascii=False), job["run_id"]))
            elif job is None:
                run_id = uuid.uuid4().hex
                parts = [{"name": "orders", "kind": "orders", "status": "queued", "total": 0, "processed": 0, "verified": 0}]
                parts += [{"name": "ad-" + day, "kind": "ad", "date": day, "status": "queued", "total": 0, "processed": 0, "verified": 0} for day in dates]
                job = {"id": run_id, "run_id": run_id, "status": "queued", "stage": "fetching", "trigger": trigger,
                       "date_from": start.isoformat(), "date_to": end.isoformat(), "begin_time": begin_ms, "end_time": end_ms,
                       "query_clock": "UTC", "source_timezone": "unspecified", "parts": parts, "created_at": utc_now(),
                       "updated_at": utc_now(), "finished_at": None, "error": None, "resume_count": 0,
                       "total": 0, "processed": 0, "verified": 0, "completed_parts": 0, "total_parts": len(parts)}
                connection.execute("INSERT INTO report_run VALUES(?,?)", (run_id, json.dumps(job, ensure_ascii=False)))
        self._launch(job["run_id"])
        return job

    def _launch(self, run_id):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._worker, args=(run_id,), daemon=True, name="moboreels-reports")
            self._thread.start()

    def _worker(self, run_id):
        while True:
            executed = self._run(run_id)
            with self._lock:
                active = next((j for j in self._jobs() if j["status"] in ("queued", "running")), None)
                if executed and active and (active["run_id"] != run_id or active["status"] == "queued"):
                    run_id = active["run_id"]
                else:
                    self._thread = None
                    return

    def wait(self, run_id, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self.get(run_id)
            if job and job["status"] in ("succeeded", "failed"):
                thread = self._thread
                if thread and thread is not threading.current_thread():
                    thread.join(max(0, deadline - time.monotonic()))
                return job
            time.sleep(0.02)
        return self.get(run_id)

    def _save_part(self, run_id, index, part, stage=None):
        job = self.get(run_id)
        job["parts"][index] = deepcopy(part)
        changes = {"parts": job["parts"], "total": sum(p.get("total", 0) for p in job["parts"]),
                   "processed": sum(p.get("processed", 0) for p in job["parts"]),
                   "verified": sum(p.get("verified", 0) for p in job["parts"]),
                   "completed_parts": sum(p["status"] == "succeeded" for p in job["parts"]),
                   "fetched_pages": part.get("fetched_pages", 0), "total_pages": part.get("total_pages", 0)}
        if stage:
            changes["stage"] = stage
        return self._update(run_id, **changes)

    def _collect(self, directory, label, fetch, on_progress):
        def page(number):
            path = directory / (label + "-%05d.json" % number)
            if path.exists():
                value = json.loads(path.read_text(encoding="utf-8"))
            else:
                value = fetch(number)
                _save_json(path, value)
            return value

        first = page(1)
        total = first.get("total")
        if isinstance(total, bool) or not isinstance(total, int) or total < 0:
            raise ReportError("统计分页总数无效")
        pages = max(1, math.ceil(total / 100))
        rows = []
        for number in range(1, pages + 1):
            value = first if number == 1 else page(number)
            expected = min(100, max(0, total - (number - 1) * 100))
            if value.get("total") != total or not isinstance(value.get("list"), list) or len(value["list"]) != expected:
                raise ReportError("统计分页不完整或总数发生变化，未写入此分项")
            rows.extend(value["list"])
            on_progress(label, number, pages)
        return rows

    def _records(self, run, index, part, directory):
        plan_path = directory / (part["name"] + "-complete.json")
        if plan_path.exists():
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            records = plan["records"]
            if plan["sha256"] != _key(records):
                raise ReportError("统计快照校验失败")
            return records

        def progress(label, current, pages):
            part.update(fetch_part=label, fetched_pages=current, total_pages=pages)
            self._save_part(run["run_id"], index, part, "fetching")

        if part["kind"] == "orders":
            snapshots = {}
            for status in (-1, 0, 1):
                rows = self._collect(directory, "orders-status-%s-attempt-%s" % (status, part.get("fetch_attempt", 0)),
                                     lambda page, status=status: get_order_page(self.provider, run["begin_time"], run["end_time"], status, page), progress)
                keys = [_text(row.get("ref_order_id"), "ref_order_id", 255, True) for row in rows]
                if len(set(keys)) != len(keys):
                    raise ReportError("订单分页出现重复外部订单号，未写入此分项")
                snapshots[status] = (rows, set(keys))
            all_ids = snapshots[-1][1]
            if not snapshots[0][1].issubset(all_ids) or not snapshots[1][1].issubset(all_ids):
                raise ReportError("订单状态筛选与完整目录不一致，未写入此分项")
            all_rows = {_text(row.get("ref_order_id"), "ref_order_id", 255, True): row for row in snapshots[-1][0]}
            for status in (0, 1):
                for row in snapshots[status][0]:
                    ref = _text(row.get("ref_order_id"), "ref_order_id", 255, True)
                    if not _same_order_observation(all_rows[ref], row):
                        raise ReportError("订单状态快照期间金额、归属或状态发生变化，未写入此分项；继续同步将重新取数")
            records = [normalize_order(row, self.settings.institution_user_id, snapshots[0][1], snapshots[1][1]) for row in snapshots[-1][0]]
        else:
            from .ad_revenue import get_ad_revenue_page, normalize_ad_revenue
            rows = self._collect(directory, part["name"] + "-attempt-%s" % part.get("fetch_attempt", 0),
                                 lambda page: get_ad_revenue_page(self.provider, part["date"], page), progress)
            records = []
            for raw in rows:
                row = normalize_ad_revenue(raw, part["date"])
                _text(row["code"], "code", 64, True)
                for name, scale in (("amount_cents", 12), ("amount_usd", 14)):
                    whole, _, fractional = row[name].lstrip("-").partition(".")
                    if len(whole.lstrip("0")) > 38 - scale or len(fractional.rstrip("0")) > scale:
                        raise ValueError("广告金额超出数据库精度，已保留原始快照")
                row.update(institution_user_id=self.settings.institution_user_id,
                           snapshot_id=run["run_id"],
                           row_key=_key(self.settings.institution_user_id, 2, row["ad_time"], row["code"], run["run_id"]))
                records.append(row)
        keys = [row["row_key"] for row in records]
        if len(keys) != len(set(keys)):
            raise ReportError("统计分页出现重复记录，未写入此分项")
        _save_json(plan_path, {"sha256": _key(records), "records": records, "completed_at": utc_now()})
        return records

    def _write(self, kind, records, run_id, seen_at, touch=False):
        pending = list(records)
        for attempt in range(3):
            try:
                if touch:
                    self.store.touch_records(kind, [r["row_key"] for r in pending], run_id, seen_at)
                else:
                    self.store.write_records(kind, pending, run_id, seen_at)
                return
            except Exception:
                self.store.close_writer()
                previous = None
                for check in range(5):
                    observed = self.store.read_records(kind, [r["row_key"] for r in pending])
                    missing = [row for row in pending if not self.store.same(kind, observed.get(row["row_key"]), row, run_id)]
                    if not missing:
                        return
                    signature = tuple(row["row_key"] for row in missing)
                    if previous == signature:
                        pending = missing
                        break
                    previous = signature
                    time.sleep(2)
                else:
                    raise ReportError("统计写入结果仍待核对，已保留完整快照")
                if attempt == 2:
                    raise ReportError("统计写入失败，已保留完成进度和快照")
                time.sleep(2)

    def _apply(self, run, index, part, records):
        kind, run_id = part["kind"], run["run_id"]
        seen_at = part.setdefault("seen_at", utc_now())
        part.update(total=len(records), status="running")
        self._save_part(run_id, index, part, "writing")
        for offset in range(0, len(records), 500):
            batch = records[offset:offset + 500]
            existing = self.store.read_records(kind, [row["row_key"] for row in batch])
            changed, unchanged = [], []
            for row in batch:
                old = existing.get(row["row_key"])
                if self.store.same(kind, old, row, run_id):
                    continue
                (unchanged if self.store.same(kind, old, row) else changed).append(row)
            for begin in range(0, len(changed), 20):
                self._write(kind, changed[begin:begin + 20], run_id, seen_at)
            if unchanged:
                self._write(kind, unchanged, run_id, seen_at, touch=True)
            part["processed"] = offset + len(batch)
            self._save_part(run_id, index, part, "writing")
        self._save_part(run_id, index, part, "verifying")
        for offset in range(0, len(records), 500):
            batch = records[offset:offset + 500]
            observed = self.store.read_records(kind, [row["row_key"] for row in batch])
            if any(not self.store.same(kind, observed.get(row["row_key"]), row, run_id) for row in batch):
                raise ReportError("统计读回核验不一致，未标记成功")
            part["verified"] = offset + len(batch)
            self._save_part(run_id, index, part, "verifying")
        if kind == "ad":
            self.store.publish_ad_day(part["date"], run_id, len(records), _key(records), utc_now())

    def _run(self, run_id):
        with _process_lock(self.root / "reports.lock") as acquired:
            if not acquired:
                return False
            run = self.get(run_id)
            if not run or run["status"] not in ("queued", "running"):
                return False
            self._update(run_id, status="running", started_at=run.get("started_at") or utc_now(), error=None)
            directory = self.snapshots / run_id
            directory.mkdir(exist_ok=True, mode=0o700)
            for index, part in enumerate(run["parts"]):
                if part["status"] == "succeeded":
                    continue
                try:
                    part.update(status="running", error=None)
                    self._save_part(run_id, index, part, "fetching")
                    records = self._records(run, index, part, directory)
                    self._apply(run, index, part, records)
                    part.update(status="succeeded", error=None)
                except Exception as exc:
                    part.update(status="failed", error=str(exc) if isinstance(exc, ReportError) else "统计分项失败（%s）" % type(exc).__name__)
                finally:
                    self.store.close_writer()
                self._save_part(run_id, index, part)
            final = self.get(run_id)
            failed = any(part["status"] != "succeeded" for part in final["parts"])
            self._update(run_id, status="failed" if failed else "succeeded", stage="completed",
                         error="部分统计分项未完成，可继续同步；已成功分项保持有效" if failed else None, finished_at=utc_now())
            return True

    def query(self, params, actor, all_users=False):
        star_id = None if all_users else identity(self.settings.institution_user_id, actor)[0]
        result = self.store.query(params, star_id=star_id)
        result["scope"] = {"kind": "institution" if all_users else "owner", "label": "机构全部" if all_users else "本人推广"}
        result["last_success_at"] = self.last_success_at()
        return result

    def last_success_at(self):
        return next((job.get("finished_at") for job in self._jobs() if job["status"] == "succeeded"), None)


MONEY_METRICS = ("order_recharge_usd", "order_refund_usd", "order_net_usd", "advertising_revenue_usd",
                 "total_revenue_usd", "unclassified_order_usd", "unmatched_attribution_usd")
COUNT_METRICS = ("order_recharge_count", "order_refund_count", "unclassified_order_count",
                 "unmatched_order_count", "unmatched_ad_count", "undated_order_count")


def _query_report(store, params, star_id):
    with localcontext() as context:
        context.prec = 65
        return _query_report_precise(store, params, star_id)


def _query_report_precise(store, params, star_id):
    start, end = _period(params)
    try:
        page, size = int(params.get("page_index", 1)), int(params.get("page_size", 50))
    except (ValueError, TypeError):
        raise ValueError("报表分页参数无效")
    if not 1 <= page <= 1000000 or not 1 <= size <= 100:
        raise ValueError("报表分页参数无效")
    group_by = params.get("group_by", "promotion")
    if group_by not in ("day", "story", "promotion"):
        raise ValueError("统计分组参数无效")
    query = _text(params.get("q"), "q", 200).strip()
    code = _text(params.get("code"), "code", 64)
    story = _text(params.get("story_id"), "story_id", 64)
    period = " BETWEEN %s AND %s" % (_literal(start.isoformat()), _literal(end.isoformat()))
    order_where = store.scope() + " AND (source_date" + period + " OR source_date IS NULL)"
    ad_where = "a." + store.scope().replace(" AND project_type", " AND a.project_type") + " AND a.ad_time" + period
    if code:
        order_where += " AND code=" + _literal(code)
        ad_where += " AND a.code=" + _literal(code)
    if star_id is not None:
        order_where += " AND real_star_user_id=" + _literal(star_id)
    if story:
        order_where += " AND story_id=" + _literal(story)
    order_sql = """SELECT JSON_OBJECT('_kind','order','date',source_date,'code',code,'story_id',story_id,
        'story_name',MAX(story_name),'real_star_user_id',real_star_user_id,'classification',classification,
        'classification_reason',classification_reason,'source_status_text',source_status_text,
        'coo_order_status_raw',coo_order_status_raw,'source_project_type',source_project_type,
        'count',COUNT(*),'amount_usd',CAST(SUM(amount_usd) AS CHAR),
        'absolute_amount_usd',CAST(SUM(ABS(amount_usd)) AS CHAR),
        'undated_count',SUM(source_date IS NULL)) FROM %s WHERE %s
        GROUP BY source_date,code,story_id,real_star_user_id,classification,classification_reason,
        source_status_text,coo_order_status_raw,source_project_type;""" % (ORDER_TABLE, order_where)
    ad_sql = """SELECT JSON_OBJECT('_kind','ad','date',a.ad_time,'code',a.code,'count',COUNT(*),
        'amount_usd',CAST(SUM(a.amount_usd) AS CHAR)) FROM %s a INNER JOIN %s s
        ON s.institution_user_id=a.institution_user_id AND s.project_type=a.project_type
        AND s.ad_time=a.ad_time AND s.snapshot_id=a.snapshot_id WHERE %s GROUP BY a.ad_time,a.code;""" % (AD_TABLE, AD_DAY_TABLE, ad_where)
    coverage_sql = "SELECT JSON_OBJECT('_kind','coverage','ad_data_as_of',MAX(ad_time),'complete_days',COUNT(*)) FROM %s WHERE %s AND ad_time%s;" % (AD_DAY_TABLE, store.scope(), period)
    source = store.reader._read(order_sql + "\n" + ad_sql + "\n" + coverage_sql)
    codes = sorted({row["code"] for row in source if row.get("code")})
    mappings = {}
    for offset in range(0, len(codes), 500):
        names = ("id", "star_user_id", "story_id", "koc_code", "request_id", "star_name", "tenant_key", "user_id")
        rows = store.reader._read("SELECT %s FROM ads_ai.cdreader_koc_promotion WHERE %s AND status='succeeded' AND koc_code IN (%s);" % (
            _json_expr(names), store.scope(), ",".join(_literal(value) for value in codes[offset:offset + 500])))
        for row in rows:
            mappings.setdefault(row["koc_code"], []).append(row)
    story_names = {row["story_id"]: row.get("story_name", "") for row in source if row.get("story_id")}
    missing_names = sorted({row["story_id"] for rows in mappings.values() for row in rows if row["story_id"] not in story_names})
    for offset in range(0, len(missing_names), 500):
        rows = store.reader._read("SELECT JSON_OBJECT('story_id',story_id,'story_name',story_name) FROM ads_ai.cdreader_koc_drama WHERE %s AND story_id IN (%s);" % (
            store.scope(), ",".join(_literal(value) for value in missing_names[offset:offset + 500])))
        story_names.update({row["story_id"]: row["story_name"] for row in rows})
    groups = {}

    def item_for(day, code_value, story_id, owner_id, mapping):
        if story and story_id != story:
            return None
        if query and query not in code_value and query not in story_id:
            return None
        key = (day,) if group_by == "day" else (story_id,) if group_by == "story" else (code_value, story_id, owner_id)
        if key not in groups:
            item = {"date": day if group_by == "day" else None,
                    "koc_code": code_value if group_by == "promotion" else "",
                    "story_id": story_id if group_by != "day" else "",
                    "story_name": story_names.get(story_id, "") if group_by != "day" else "",
                    "real_star_user_id": owner_id if group_by == "promotion" else "",
                    "promotion_key": None, "promotion_request_id": None, "promotion_record_id": None, "upstream_promotion_id": None,
                    "owner_name": None, "attribution_status": "matched" if mapping else "unmatched", "unknown_states": []}
            item.update({name: Decimal(0) for name in MONEY_METRICS})
            item.update({name: 0 for name in COUNT_METRICS})
            if mapping and group_by == "promotion":
                text = "%s:%s:2:%s" % (store.settings.institution_user_id, mapping["star_user_id"], mapping["story_id"])
                item.update(promotion_key=hashlib.sha256(text.encode()).hexdigest(), promotion_request_id=mapping["request_id"],
                            promotion_record_id=mapping.get("id"), owner_name=mapping["star_name"])
            groups[key] = item
        elif not mapping:
            groups[key]["attribution_status"] = "contains_unmatched"
        return groups[key]

    for row in source:
        if row["_kind"] == "coverage":
            continue
        matches = mappings.get(row["code"], [])
        if row["_kind"] == "ad":
            mapping = matches[0] if len(matches) == 1 else None
            if star_id is not None and (mapping is None or mapping["star_user_id"] != star_id):
                continue
            item = item_for(row.get("date"), row["code"], mapping["story_id"] if mapping else "", mapping["star_user_id"] if mapping else "", mapping)
            if item is None:
                continue
            amount = Decimal(str(row["amount_usd"]))
            item["advertising_revenue_usd"] += amount
            if mapping is None:
                item["unmatched_ad_count"] += int(row["count"])
                item["unmatched_attribution_usd"] += amount
                if len(matches) > 1:
                    item["attribution_status"] = "ambiguous"
        else:
            own = [m for m in matches if m["star_user_id"] == row["real_star_user_id"] and m["story_id"] == row["story_id"]]
            mapping = own[0] if len(own) == 1 else None
            item = item_for(row.get("date"), row["code"], row["story_id"], row["real_star_user_id"], mapping)
            if item is None:
                continue
            item["undated_order_count"] += int(row.get("undated_count") or 0)
            known_amount = Decimal(0)
            if row["classification"] == 0:
                item["order_recharge_count"] += int(row["count"])
                known_amount = Decimal(str(row["amount_usd"]))
                item["order_recharge_usd"] += known_amount
            elif row["classification"] == 1:
                item["order_refund_count"] += int(row["count"])
                refund = Decimal(str(row["absolute_amount_usd"]))
                item["order_refund_usd"] += refund
                known_amount = -refund
            else:
                item["unclassified_order_count"] += int(row["count"])
                item["unclassified_order_usd"] += Decimal(str(row["amount_usd"]))
                state = {key: row.get(key) for key in ("source_status_text", "coo_order_status_raw", "source_project_type", "classification_reason")}
                if state not in item["unknown_states"]:
                    item["unknown_states"].append(state)
            if mapping is None:
                item["unmatched_order_count"] += int(row["count"])
                item["unmatched_attribution_usd"] += known_amount
    summary = {name: Decimal(0) for name in MONEY_METRICS}
    summary.update({name: 0 for name in COUNT_METRICS})
    result = []
    for item in groups.values():
        item["order_net_usd"] = item["order_recharge_usd"] - item["order_refund_usd"]
        item["total_revenue_usd"] = item["order_net_usd"] + item["advertising_revenue_usd"]
        for name in MONEY_METRICS + COUNT_METRICS:
            summary[name] += item[name]
        for name in MONEY_METRICS:
            item[name] = format(item[name], "f")
        result.append(item)
    result.sort(key=lambda item: (item.get("date") or "", item["story_id"], item["koc_code"], item["real_star_user_id"]))
    for name in MONEY_METRICS:
        summary[name] = format(summary[name], "f")
    coverage = next((row for row in source if row["_kind"] == "coverage"), {})
    return {"list": result[(page - 1) * size:page * size], "total": len(result), "page_index": page,
            "page_size": size, "summary": summary, "group_by": group_by,
            "date_from": start.isoformat(), "date_to": end.isoformat(), "source_timezone": "unspecified",
            "currency": "USD", "ad_data_as_of": coverage.get("ad_data_as_of"), "complete_ad_days": coverage.get("complete_days", 0),
            "net_definition": "按数值状态过滤核对的充值金额减退款金额；不含未分类订单，不代表平台佣金",
            "attribution_note": "广告按来源日期和口令唯一汇总，只匹配本地成功推广记录；不会按多个短链重复分摊",
            "undated_note": "时间格式未明确的订单单独列为未分类，不参与已分类收入"}
