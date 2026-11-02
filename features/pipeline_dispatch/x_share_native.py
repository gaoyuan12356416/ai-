"""The existing explicitly-triggered YouTube to X share ledger adapter."""
import hashlib
import json
import re
import secrets
import threading

from .adapters import AdapterError, obj


class XShareBridge:
    def __init__(self, runtime):
        self.runtime, self.service = runtime, runtime.youtube_shares_service()
        self.lock = threading.RLock()
        with self.service._connect(write=True) as c:
            c.execute("CREATE TABLE IF NOT EXISTS pipeline_share_claim(source_key TEXT NOT NULL,generation INTEGER NOT NULL,claim_hash TEXT NOT NULL,native_claim TEXT NOT NULL,descriptor_json TEXT NOT NULL,PRIMARY KEY(source_key,generation))")
            c.execute("CREATE TABLE IF NOT EXISTS pipeline_canonical_hold(source_key TEXT PRIMARY KEY,cutoff TEXT NOT NULL,reason TEXT NOT NULL)")

    @staticmethod
    def identity(job):
        if (job.get("channel") != "x" or job.get("family") != "youtube_share" or job.get("stage") != "publish"
            or job.get("source_key") != "x:youtube_share:" + str(job.get("source_id"))):
            raise AdapterError("share_identity_invalid")

    @staticmethod
    def digest(claim):
        token = str(claim.get("claim_token", ""))
        if len(token) < 32:
            raise AdapterError("share_claim_invalid")
        return hashlib.sha256(token.encode()).hexdigest()

    def item(self, identity):
        with self.service._connect() as c:
            row = c.execute("SELECT * FROM youtube_share_item WHERE id=?", (int(identity),)).fetchone()
            if not row:
                raise AdapterError("share_task_missing")
            item = dict(row)
            item["run"] = dict(c.execute("SELECT * FROM youtube_share_run WHERE id=?", (item["run_id"],)).fetchone())
            return item

    def owned(self, job, claim):
        self.identity(job)
        with self.service._connect() as c:
            row = c.execute("SELECT * FROM pipeline_share_claim WHERE source_key=? AND generation=?", (job["source_key"], job["generation"])).fetchone()
            latest = c.execute("SELECT MAX(generation) FROM pipeline_share_claim WHERE source_key=?", (job["source_key"],)).fetchone()[0]
        if not row or latest != job["generation"] or not secrets.compare_digest(row["claim_hash"], self.digest(claim)):
            raise AdapterError("share_claim_conflict")
        item = self.item(job["source_id"])
        item["claim_token"] = row["native_claim"]
        return item, obj(row["descriptor_json"])

    def verify(self, item):
        run = item["run"]
        actor = {"tenant_key": run["actor_tenant"], "user_id": run["actor_user"], "role": run["actor_role"]}
        self.service._fresh(run["created_at"], run["source_verified_at"])
        account = self.runtime.verify_account(item["account_id"], actor, run["scope"], preserve_transient_status=True, require_publish_approved=True)
        if account.get("status") != "active" or account.get("publish_approved") is not True or int(account.get("id", 0)) != int(item["account_id"]):
            raise AdapterError("x_account_not_publishable")
        return actor

    def reserve(self, job, claim):
        self.identity(job)
        item = self.item(job["source_id"])
        with self.service._connect() as c:
            if c.execute("SELECT 1 FROM pipeline_canonical_hold WHERE source_key=?", (job["source_key"],)).fetchone():
                raise AdapterError("canonical_backlog_paused")
            previous = c.execute("SELECT 1 FROM pipeline_share_claim WHERE source_key=? AND generation=?", (job["source_key"], job["generation"])).fetchone()
        if previous:
            return self.owned(job, claim)[1]
        self.verify(item)
        descriptor = {"kind": "x_share_publish", "account_resource": "x:account:" + str(item["account_id"]), "request": {"text": item["run"]["text"]}}
        native_claim = secrets.token_hex(16)
        with self.service._connect(write=True) as c:
            changed = c.execute("UPDATE youtube_share_item SET status='publishing',claim_token=?,claimed_at=? WHERE id=? AND status='queued' AND attempted_at IS NULL AND duplicate_of IS NULL", (native_claim, self.service.clock(), item["id"])).rowcount
            if changed != 1:
                raise AdapterError("share_existing_attempt_requires_reconciliation")
            c.execute("INSERT INTO pipeline_share_claim VALUES(?,?,?,?,?)", (job["source_key"], job["generation"], self.digest(claim), native_claim, json.dumps(descriptor)))
        return descriptor

    def begin(self, job, claim, operation):
        item, descriptor = self.owned(job, claim)
        details = operation.get("details", {})
        body = json.dumps({"text": descriptor["request"]["text"]}, ensure_ascii=False, separators=(",", ":")).encode()
        if details.get("method") != "POST" or details.get("path") != "/2/tweets" or details.get("body_sha256") != hashlib.sha256(body).hexdigest() or operation.get("account_resource") != descriptor["account_resource"]:
            raise AdapterError("share_operation_changed")
        actor = self.verify(item)
        if not self.service._begin_attempt(item):
            raise AdapterError("share_already_attempted")
        with self.runtime.publish_credentials(item["account_id"], actor, item["run"]["scope"]) as (_, token):
            return {"credentials": {"access_token": token}}

    def checkpoint(self, job, claim, phase, receipt):
        item, _ = self.owned(job, claim)
        if item["attempted_at"] is None:
            raise AdapterError("share_attempt_receipt_required")
        if phase == "x_share_published":
            identity = str(receipt.get("post_id", ""))
            if not re.fullmatch(r"[0-9]{1,30}", identity):
                raise AdapterError("share_receipt_invalid")
            if item["status"] == "published":
                if item["post_id"] != identity:
                    raise AdapterError("share_receipt_conflict")
                return {"accepted": True}
            self.finish(item, "published", "分享成功", http_status=receipt.get("http_status", 201), post_id=identity)
        elif phase == "x_share_rejected":
            status = receipt.get("http_status")
            if type(status) is not int or not 400 <= status < 500 or status == 408:
                raise AdapterError("share_rejection_invalid")
            if item["status"] == "published":
                raise AdapterError("share_receipt_conflict")
            if item["status"] != "failed":
                self.finish(item, "failed", "平台明确拒绝了分享请求", http_status=status)
        else:
            raise AdapterError("share_checkpoint_invalid")
        return {"accepted": True}

    def finish(self, item, status, message, *, http_status=None, post_id=""):
        # Exact late receipts may resolve a quarantined attempt. They never
        # authorize a second send, and they cannot update a different claim.
        with self.service._connect(write=True) as c:
            changed = c.execute("UPDATE youtube_share_item SET status=?,message=?,post_id=?,finished_at=?,claim_token='' WHERE id=? AND claim_token=? AND status IN ('publishing','unknown_outcome') AND attempted_at IS NOT NULL", (status, message, post_id, self.service.clock(), item["id"], item["claim_token"])).rowcount
            if changed != 1:
                raise AdapterError("share_receipt_state_conflict")
            c.execute("UPDATE youtube_share_attempt SET status=?,http_status=?,post_id=?,finished_at=? WHERE item_id=?", (status, http_status, post_id, self.service.clock(), item["id"]))

    def pause(self, job, cutoff):
        self.identity(job)
        with self.service._connect(write=True) as c:
            item = c.execute("SELECT * FROM youtube_share_item WHERE id=?", (int(job["source_id"]),)).fetchone()
            if not item or item["status"] != "queued" or item["attempted_at"] is not None or item["duplicate_of"] is not None or item["claim_token"] or item["post_id"]:
                raise AdapterError("canonical_task_not_untouched")
            previous = c.execute("SELECT cutoff FROM pipeline_canonical_hold WHERE source_key=?", (job["source_key"],)).fetchone()
            if previous and previous[0] != cutoff:
                raise AdapterError("canonical_hold_conflict")
            c.execute("INSERT OR IGNORE INTO pipeline_canonical_hold VALUES(?,?,'cutover_overdue')", (job["source_key"], cutoff))
        return {"paused": True, "source_key": job["source_key"]}

    def dispatch(self, action, payload):
        job, claim = payload.get("job", {}), payload.get("claim", {})
        self.identity(job)
        with self.lock:
            if action == "pause": return self.pause(job, payload.get("cutoff"))
            if action == "reserve": return self.reserve(job, claim)
            if action == "begin-operation": return self.begin(job, claim, payload.get("operation", {}))
            if action == "checkpoint": return self.checkpoint(job, claim, payload.get("phase"), payload.get("receipt", {}))
            if action == "reconcile":
                item = self.item(job["source_id"])
                return {"status": item["status"], "post_id": item["post_id"], "unknown_outcome": item["status"] == "unknown_outcome"}
            item, _ = self.owned(job, claim)
            if action == "complete":
                if item["status"] not in ("published", "failed"):
                    raise AdapterError("share_final_receipt_required")
                return {"status": item["status"], "post_id": item["post_id"]}
            if action == "fail":
                if item["status"] in ("published", "failed"):
                    return {"canonical_terminal": True}
                unknown = item["attempted_at"] is not None
                if unknown:
                    with self.service._connect(write=True) as c:
                        c.execute("UPDATE youtube_share_item SET status='unknown_outcome',message=? WHERE id=? AND claim_token=? AND status='publishing'", ("节点任务停止，保留回执供核对", item["id"], item["claim_token"]))
                else:
                    self.service._finish(item, "failed", "节点任务停止，保留回执供核对")
                return {"canonical_no_write": not unknown}
        raise AdapterError("share_action_invalid")


_INIT_LOCK = threading.Lock()


def dispatch_share(runtime, action, payload):
    with _INIT_LOCK:
        if not hasattr(runtime, "pipeline_share_bridge"):
            runtime.pipeline_share_bridge = XShareBridge(runtime)
    return runtime.pipeline_share_bridge.dispatch(action, payload)
