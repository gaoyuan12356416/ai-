"""Durable, single-submit Tencent CI actions on the cloud node's private volume."""
import contextlib
import datetime as dt
import hashlib
import json
import sqlite3
import time
from pathlib import Path

from .executors import ExecutionError


class PendingReadback(Exception):
    """The provider may have accepted this exact action."""


class QueueUnavailable(PendingReadback):
    """The tenant queue or its credentials temporarily prevent further admission."""


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def records(value):
    return value if isinstance(value, list) else [value] if isinstance(value, dict) else []


class CloudLedger:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, exist_ok=True, parents=True)
        with self.connection() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript("""
                CREATE TABLE IF NOT EXISTS action (
                  identity TEXT PRIMARY KEY, digest TEXT NOT NULL, target TEXT NOT NULL, tag TEXT NOT NULL,
                  created REAL NOT NULL, provider_id TEXT NOT NULL DEFAULT '', state TEXT NOT NULL DEFAULT 'intent');
                CREATE TABLE IF NOT EXISTS work (
                  identity TEXT PRIMARY KEY, job TEXT NOT NULL, next_poll REAL NOT NULL DEFAULT 0,
                  receipt TEXT NOT NULL DEFAULT '', route TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS fb_input (
                  action TEXT PRIMARY KEY, object_key TEXT NOT NULL, seconds REAL NOT NULL);
            """)
            if "paid" not in {r[1] for r in c.execute("PRAGMA table_info(work)")}:
                c.execute("ALTER TABLE work ADD COLUMN paid INTEGER NOT NULL DEFAULT 0")

    @contextlib.contextmanager
    def connection(self):
        with contextlib.closing(sqlite3.connect(str(self.path), timeout=15)) as c:
            c.row_factory = sqlite3.Row
            with c:
                yield c

    def intent(self, identity, binding, target, tag, work_id=None):
        with self.connection() as c:
            result = c.execute("INSERT OR IGNORE INTO action(identity,digest,target,tag,created) VALUES(?,?,?,?,?)",
                               (identity, binding, target, tag, time.time()))
            row = dict(c.execute("SELECT * FROM action WHERE identity=?", (identity,)).fetchone())
            if (row["digest"], row["target"], row["tag"]) != (binding, target, tag):
                raise ExecutionError("cos_frozen_action_conflict", unknown=True)
            if work_id is not None:
                c.execute("UPDATE work SET paid=1 WHERE identity=?", (work_id,))
            return row, result.rowcount == 1

    def retry_rejected(self, identity):
        with self.connection() as c:
            return c.execute("UPDATE action SET state='intent',created=? WHERE identity=? AND state='rejected' AND provider_id=''",
                             (time.time(),identity)).rowcount == 1

    def update(self, identity, provider_id, state):
        with self.connection() as c:
            if not c.execute("UPDATE action SET provider_id=?,state=? WHERE identity=? AND (provider_id='' OR provider_id=?)",
                             (provider_id, state, identity, provider_id)).rowcount:
                raise ExecutionError("cos_provider_identity_conflict", unknown=True)

    def remember(self, job):
        identity = str(job["id"]) + ":" + str(job["generation"])
        with self.connection() as c:
            c.execute("INSERT INTO work(identity,job) VALUES(?,?) ON CONFLICT(identity) DO UPDATE SET job=excluded.job",
                      (identity, encoded(job)))
        return identity

    def due(self):
        with self.connection() as c:
            return [dict(r) for r in c.execute("SELECT * FROM work WHERE next_poll<=? ORDER BY next_poll,identity", (time.time(),))]

    def postpone(self, identity, seconds):
        with self.connection() as c:
            c.execute("UPDATE work SET next_poll=? WHERE identity=?", (time.time() + seconds, identity))

    def receipt(self, identity, route, payload):
        with self.connection() as c:
            c.execute("UPDATE work SET route=?,receipt=? WHERE identity=? AND receipt=''", (route, encoded(payload), identity))

    def forget(self, identity):
        with self.connection() as c:
            c.execute("DELETE FROM work WHERE identity=?", (identity,))


class CloudJobs:
    def __init__(self, ledger, client, bucket, region, prefix, queue):
        self.ledger, self.client = ledger, client
        self.bucket, self.region, self.prefix, self.queue = bucket, region, prefix.rstrip("/"), queue

    def health(self):
        try:
            result = self.client.ci_get_media_queue(Bucket=self.bucket, QueueIds=self.queue)
            row = next(r for r in records(result.get("QueueList")) if r.get("QueueId") == self.queue)
            return {"queue_ready": row["State"] == "Active", "queue_limit": int(row["MaxConcurrent"])}
        except Exception:
            return {"queue_ready": False, "queue_limit": 0}

    def _matches(self, item, row):
        operation = item.get("Operation") or {}
        target = operation.get("Output") or {}
        return (item.get("Tag") == row["tag"] and item.get("QueueId") == self.queue
                and operation.get("UserData") == row["identity"] and target.get("Bucket") == self.bucket
                and target.get("Object") == row["target"] and target.get("Region", self.region) == self.region)

    def _find(self, row):
        window = dt.datetime.fromtimestamp(row["created"], dt.timezone.utc)
        fmt = lambda moment: moment.strftime("%Y-%m-%dT%H:%M:%SZ")
        token, seen = "", set()
        while True:
            result = self.client.ci_list_media_jobs(Bucket=self.bucket, QueueId=self.queue, Tag=row["tag"],
                       StartCreationTime=fmt(window - dt.timedelta(minutes=2)), EndCreationTime=fmt(min(
                           dt.datetime.now(dt.timezone.utc), window + dt.timedelta(minutes=10))),
                       Size=100, NextToken=token)
            for item in records(result.get("JobsDetail")):
                if self._matches(item, row):
                    return item
            token = result.get("NextToken")
            if not token or token in seen:
                return None
            seen.add(token)

    def run(self, identity, stage, tag, source_key, parameters, context):
        key = fingerprint([identity, stage])
        output = self.prefix + "/ci/" + key + ".mp4"
        operation = dict(parameters, UserData=key, Output={"Bucket": self.bucket, "Region": self.region, "Object": output})
        body = {"Tag": tag, "QueueId": self.queue, "Input": {"Object": source_key}, "Operation": operation, "CallBack": "no"}
        context.check()
        context.client.post("jobs/cos-intent", dict(context.identity, action_key=key))
        row, new = self.ledger.intent(key, fingerprint(body), output, tag,
                      str(context.identity.get("job_id")) + ":" + str(context.identity.get("generation")))
        if row["state"] == "rejected":
            if not self.health()["queue_ready"]:
                raise QueueUnavailable()
            new = self.ledger.retry_rejected(key)
        try:
            if new:
                try:
                    response = self.client.ci_create_media_jobs(Bucket=self.bucket, Jobs=body, ContentType="application/xml")
                except Exception as exc:
                    code = getattr(exc, "get_error_code", lambda: "")()
                    status_code = getattr(exc, "get_status_code", lambda: 0)()
                    if str(status_code) in {"403","429","503"} and code in {
                            "AccessDenied","SlowDown","RequestLimitExceeded","TooManyRequests","QueueFull","LimitExceeded"}:
                        self.ledger.update(key, "", "rejected")
                        raise QueueUnavailable() from None
                    raise
                item = records(response.get("JobsDetail"))[0]
                if not self._matches(item, row) or not item.get("JobId"):
                    raise PendingReadback()
                self.ledger.update(key, item["JobId"], item["State"])
            elif row["provider_id"]:
                item = {"JobId": row["provider_id"]}
            else:
                item = self._find(row)
                if not item:
                    # Unique output may have arrived beyond the job-list retention window.
                    # The caller must read and qualify the actual bytes before any receipt.
                    self.client.head_object(Bucket=self.bucket, Key=output)
                    return output, {"action": key, "job_id": "", "evidence": "unique_output"}
                self.ledger.update(key, item["JobId"], item["State"])
            while True:
                details = self.client.ci_get_media_jobs(Bucket=self.bucket, JobIDs=item["JobId"])
                item = records(details.get("JobsDetail"))[0]
                if not self._matches(item, row):
                    raise PendingReadback()
                self.ledger.update(key, item["JobId"], item["State"])
                if item["State"] not in {"Submitted", "Running"}:
                    break
                context.check()
                time.sleep(4)
        except (ExecutionError, PendingReadback):
            raise
        except Exception:
            # Includes submit timeout/connection loss. Never POST this identity again.
            raise PendingReadback() from None
        if item["State"] in {"Failed", "Cancel", "Cancelled"}:
            raise ExecutionError("cos_provider_failed")
        if item["State"] != "Success":
            raise PendingReadback()
        return output, {"action": key, "job_id": item["JobId"], "evidence": "provider_success"}
