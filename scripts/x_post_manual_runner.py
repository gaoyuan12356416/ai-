#!/usr/bin/env python3
"""Process one durable operator-requested X material publish batch.

The browser only creates a frozen request. This runner performs every source,
account, compliance and media check before atomically reserving queue rows, then
publishes those rows one at a time through the existing token-owning sidecar.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from features.x_posts.selector import (  # noqa: E402
    select_manual_candidates,
    shanghai_now,
)
from features.x_posts.service import (  # noqa: E402
    download_media,
    probe_media,
    redact_text,
)
from scripts.x_post_daily_runner import (  # noqa: E402
    SidecarClient,
    SidecarError,
    _preflight_candidates,
    _safe_account,
    process_lock,
)
from scripts.x_post_schedule_runner import (  # noqa: E402
    ScheduleConfig,
    _open_source_connection,
    _publish_frozen_queues,
    _repair_client,
)


DEFAULT_CLAIM_PATH = "/internal/posts/manual-runs/claim"
DEFAULT_PLAN_PATH = "/internal/posts/manual-plan"
DEFAULT_FAILURE_PATH = "/internal/posts/manual-runs/record-failure"
MAX_MANUAL_BATCH_SIZE = 50
_RUN_STATUSES = {
    "queued",
    "running",
    "completed",
    "completed_with_errors",
    "needs_review",
    "stopped",
    "failed_preflight",
}
_QUEUE_STATUSES = {
    "queued",
    "reserved",
    "publishing",
    "published",
    "failed",
}


class ManualRunError(RuntimeError):
    def __init__(self, message, code="x_post_manual_failed"):
        self.code = str(code or "x_post_manual_failed")[:64]
        super().__init__(redact_text(message, 240))


def _positive_id(value, label):
    if (
        isinstance(value, bool)
        or not str(value or "").isdigit()
        or int(value) <= 0
    ):
        raise SidecarError(
            "x_post_manual_invalid_response",
            "%s is invalid" % label,
            502,
        )
    return int(value)


def _manual_identity(raw):
    if not isinstance(raw, dict):
        raise SidecarError(
            "x_post_manual_invalid_response",
            "Manual run response is invalid",
            502,
        )
    run_id = _positive_id(raw.get("id"), "manual run ID")
    account_ids = raw.get("account_ids")
    material_ids = raw.get("material_ids")
    expected_count = raw.get("expected_count")
    if (
        not isinstance(account_ids, list)
        or not isinstance(material_ids, list)
        or not 1 <= len(account_ids) <= MAX_MANUAL_BATCH_SIZE
        or len(account_ids) != len(material_ids)
        or len(set(account_ids)) != len(account_ids)
        or any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or value <= 0
            for value in account_ids
        )
        or any(
            not isinstance(value, str)
            or not re.fullmatch(r"[1-9][0-9]*", value)
            for value in material_ids
        )
        or len(set(material_ids)) != len(material_ids)
        or isinstance(expected_count, bool)
        or not isinstance(expected_count, int)
        or expected_count != len(account_ids)
        or raw.get("status") not in _RUN_STATUSES
        or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", str(raw.get("run_date", "")))
        or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", str(raw.get("source_date", "")))
        or not isinstance(raw.get("body_template"), str)
        or not raw.get("body_template")
    ):
        raise SidecarError(
            "x_post_manual_invalid_response",
            "Manual run identity is inconsistent",
            502,
        )
    item = dict(raw)
    item["id"] = run_id
    item["account_ids"] = list(account_ids)
    item["material_ids"] = list(material_ids)
    item["queues"] = _manual_queues(
        raw.get("queues", []),
        run_id,
        account_ids,
    )
    return item


def _manual_queues(raw_queues, run_id, account_ids):
    if not isinstance(raw_queues, list) or len(raw_queues) > len(account_ids):
        raise SidecarError(
            "x_post_manual_invalid_response",
            "Manual queue response is invalid",
            502,
        )
    queues = []
    seen_ids = set()
    seen_accounts = set()
    previous_rank = 0
    for raw in raw_queues:
        if not isinstance(raw, dict):
            raise SidecarError(
                "x_post_manual_invalid_response",
                "Manual queue identity is invalid",
                502,
            )
        queue_id = _positive_id(raw.get("id"), "queue ID")
        queue_run_id = _positive_id(
            raw.get("manual_run_id"),
            "queue manual run ID",
        )
        account_id = _positive_id(raw.get("account_id"), "queue account ID")
        rank = _positive_id(raw.get("candidate_rank"), "candidate rank")
        status = str(raw.get("status", "") or "")
        unknown = raw.get("unknown_outcome", False)
        if (
            queue_run_id != run_id
            or queue_id in seen_ids
            or account_id in seen_accounts
            or account_id not in account_ids
            or rank <= previous_rank
            or status not in _QUEUE_STATUSES
            or not isinstance(unknown, bool)
        ):
            raise SidecarError(
                "x_post_manual_invalid_response",
                "Manual queue identity is inconsistent",
                502,
            )
        seen_ids.add(queue_id)
        seen_accounts.add(account_id)
        previous_rank = rank
        queues.append(
            {
                "id": queue_id,
                "account_id": account_id,
                "candidate_rank": rank,
                "status": status,
                "unknown_outcome": unknown,
            }
        )
    if queues and [item["account_id"] for item in queues] != account_ids:
        raise SidecarError(
            "x_post_manual_invalid_response",
            "Manual queue account order is inconsistent",
            502,
        )
    return queues


class ManualSidecarClient(SidecarClient):
    def claim(self, path=DEFAULT_CLAIM_PATH):
        result = self.post(path, {})
        item = result.get("item") if isinstance(result, dict) else None
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("found"), bool)
        ):
            raise SidecarError(
                "x_post_manual_invalid_response",
                "Manual claim response is invalid",
                502,
            )
        if not item["found"]:
            if item.get("run") is not None:
                raise SidecarError(
                    "x_post_manual_invalid_response",
                    "Empty manual claim response is inconsistent",
                    502,
                )
            return None
        return _manual_identity(item.get("run"))

    def create_plan(self, run_id, candidates, path=DEFAULT_PLAN_PATH):
        result = self.post(
            path,
            {"run_id": int(run_id), "candidates": list(candidates)},
            write_may_have_happened=True,
        )
        item = result.get("item") if isinstance(result, dict) else None
        run = _manual_identity(item)
        if run["id"] != int(run_id) or len(run["queues"]) != len(candidates):
            raise SidecarError(
                "x_post_manual_invalid_response",
                "Created manual plan does not match the request",
                502,
                unknown_outcome=True,
            )
        return run

    def record_failure(
        self,
        run_id,
        code,
        message,
        path=DEFAULT_FAILURE_PATH,
    ):
        result = self.post(
            path,
            {
                "run_id": int(run_id),
                "error_code": str(code or "x_post_manual_preflight_failed")[:64],
                "error_message": redact_text(message, 240),
            },
            write_may_have_happened=True,
        )
        item = result.get("item") if isinstance(result, dict) else None
        run = _manual_identity(item)
        if run["id"] != int(run_id) or run["status"] != "failed_preflight":
            raise SidecarError(
                "x_post_manual_invalid_response",
                "Manual preflight failure response is inconsistent",
                502,
                unknown_outcome=True,
            )
        return run


def _failure_fields(exc):
    code = str(getattr(exc, "code", "") or "x_post_manual_preflight_failed")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", code):
        code = "x_post_manual_preflight_failed"
    return code, redact_text(str(exc), 240)


def _record_failure_best_effort(sidecar, run_id, exc):
    code, message = _failure_fields(exc)
    try:
        sidecar.record_failure(run_id, code, message)
        return True
    except Exception:
        return False


def _verify_accounts(sidecar, account_ids):
    accounts = [
        _safe_account(sidecar.verify_account(account_id))
        for account_id in account_ids
    ]
    if [int(item["id"]) for item in accounts] != list(account_ids):
        raise ManualRunError(
            "Verified accounts do not match the frozen manual request",
            "x_post_manual_account_mismatch",
        )
    return accounts


def _manual_candidates(
    config,
    run,
    accounts,
    *,
    connection_factory=None,
    downloader=download_media,
    prober=probe_media,
    repair_client=None,
    now=None,
):
    connection = _open_source_connection(config, connection_factory)
    try:
        candidates, selector_rejections = select_manual_candidates(
            connection,
            run["material_ids"],
            run["source_date"],
            limit=len(run["material_ids"]),
            schema=config.mysql_database,
            now=now,
        )
    finally:
        close = getattr(connection, "close", None)
        if callable(close):
            close()
    if selector_rejections or len(candidates) != len(run["material_ids"]):
        first = selector_rejections[0] if selector_rejections else {}
        raise ManualRunError(
            first.get("error_message")
            or "Not every selected material passed source and compliance checks",
            first.get("error_code") or "x_post_manual_source_preflight_failed",
        )
    for candidate in candidates:
        candidate["body_template"] = run["body_template"]
        candidate["source_type"] = "material"
        candidate["source_date"] = run["source_date"]
    timestamp = max(1, int(shanghai_now(now).timestamp()))
    planned, preflight_rejections = _preflight_candidates(
        config,
        candidates,
        accounts,
        timestamp,
        downloader,
        prober,
        repair_client,
    )
    if preflight_rejections or len(planned) != len(accounts):
        first = preflight_rejections[0] if preflight_rejections else {}
        raise ManualRunError(
            first.get("error_message")
            or "Not every selected material passed media preflight",
            first.get("error_code") or "x_post_manual_media_preflight_failed",
        )
    if {str(item.get("material_id")) for item in planned} != set(
        run["material_ids"]
    ):
        raise ManualRunError(
            "Preflight output does not contain the exact frozen material set",
            "x_post_manual_material_mismatch",
        )
    if [int(item.get("account_id") or 0) for item in planned] != run[
        "account_ids"
    ]:
        raise ManualRunError(
            "Preflight output does not match the frozen account order",
            "x_post_manual_account_mismatch",
        )
    return planned


def execute_manual_tick(
    config,
    *,
    sidecar=None,
    connection_factory=None,
    downloader=download_media,
    prober=probe_media,
    repair_client=None,
    now=None,
):
    """Process at most one durable request; collaborators are test-injectable."""
    config.validate()
    sidecar = sidecar or ManualSidecarClient(
        config.internal_url,
        config.internal_token,
        timeout=config.internal_timeout,
    )
    if repair_client is None:
        repair_client = _repair_client(config)
    run = sidecar.claim()
    if run is None:
        return {"status": "no_pending"}

    identity = {
        "source_type": "material",
        "run_date": run["run_date"],
        "publish_time": "manual",
        "version": 1,
        "account_ids": list(run["account_ids"]),
    }
    if run["queues"]:
        result = _publish_frozen_queues(
            config,
            sidecar,
            identity,
            run["queues"],
            resumed=True,
        )
        result["manual_run_id"] = run["id"]
        return result

    try:
        sidecar.preflight_storage(config.storage_preflight_path)
        accounts = _verify_accounts(sidecar, run["account_ids"])
        candidates = _manual_candidates(
            config,
            run,
            accounts,
            connection_factory=connection_factory,
            downloader=downloader,
            prober=prober,
            repair_client=repair_client,
            now=now,
        )
        sidecar.preflight_storage(config.storage_preflight_path)
        planned = sidecar.create_plan(run["id"], candidates)
    except SidecarError as exc:
        if exc.unknown_outcome:
            raise
        recorded = _record_failure_best_effort(sidecar, run["id"], exc)
        return {
            "status": "failed_preflight",
            "manual_run_id": run["id"],
            "error_code": exc.code,
            "failure_recorded": recorded,
        }
    except Exception as exc:
        recorded = _record_failure_best_effort(sidecar, run["id"], exc)
        code, _message = _failure_fields(exc)
        return {
            "status": "failed_preflight",
            "manual_run_id": run["id"],
            "error_code": code,
            "failure_recorded": recorded,
        }

    result = _publish_frozen_queues(
        config,
        sidecar,
        identity,
        planned["queues"],
        resumed=False,
    )
    result["manual_run_id"] = run["id"]
    return result


def main():
    try:
        config = ScheduleConfig.from_env()
        with process_lock(config.lock_path) as acquired:
            if acquired is None:
                result = {"status": "skipped_locked"}
            else:
                result = execute_manual_tick(config)
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0
    except Exception as exc:
        code, message = _failure_fields(exc)
        print(
            json.dumps(
                {"status": "failed", "error_code": code, "message": message},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
