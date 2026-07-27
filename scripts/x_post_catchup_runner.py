#!/usr/bin/env python3
"""One-off, fail-closed X Post catch-up for the 2026-07-27 scope expansion."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from features.x_posts.selector import (  # noqa: E402
    CandidateSelectionError,
    normalize_date,
    previous_source_date,
    select_pool_candidates,
    shanghai_now,
)
from features.x_posts.service import (  # noqa: E402
    XPostError,
    download_media,
    probe_media,
    redact_text,
)
from scripts import x_post_daily_runner as daily  # noqa: E402


AUTHORIZED_RUN_DATE = "2026-07-27"
AUTHORIZED_EXPECTED_MISSING_COUNT = 6
AUTHORIZED_REASON = "scope_expansion_v1"

CATCHUP_QUERY_PATH = "/internal/posts/catchup-plan/query"
CATCHUP_CREATE_PATH = "/internal/posts/catchup-plan"
CATCHUP_FAILURE_PATH = "/internal/posts/catchup-runs/record-failure"

_RUN_STATUSES = frozenset(
    {
        "queued",
        "running",
        "completed",
        "completed_with_errors",
        "needs_review",
        "stopped",
        "failed_preflight",
    }
)
_QUEUE_STATUSES = frozenset({"queued", "publishing", "published", "failed"})
_RUN_REQUIRED_FIELDS = frozenset(
    {
        "id",
        "parent_run_id",
        "batch_kind",
        "run_date",
        "source_date",
        "reason",
        "account_ids",
        "status",
        "expected_count",
        "queued_count",
        "published_count",
        "failed_count",
        "unknown_count",
        "error_code",
        "error_message",
        "started_at",
        "finished_at",
        "created_at",
        "updated_at",
    }
)
_QUEUE_REQUIRED_FIELDS = frozenset(
    {
        "id",
        "run_id",
        "catchup_run_id",
        "run_date",
        "source_date",
        "account_id",
        "candidate_rank",
        "status",
        "created_at",
        "updated_at",
    }
)


def _safe_string(value, field, maximum, *, allow_empty=True):
    if not isinstance(value, str):
        raise daily.SidecarError(
            "x_catchup_plan_invalid_response",
            "Catch-up %s is invalid" % field,
        )
    if (not allow_empty and not value) or len(value) > maximum:
        raise daily.SidecarError(
            "x_catchup_plan_invalid_response",
            "Catch-up %s is invalid" % field,
        )
    if any(ord(character) < 32 for character in value):
        raise daily.SidecarError(
            "x_catchup_plan_invalid_response",
            "Catch-up %s is invalid" % field,
        )
    return value


def _positive_integer(value, field):
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value <= 0
    ):
        raise daily.SidecarError(
            "x_catchup_plan_invalid_response",
            "Catch-up %s is invalid" % field,
        )
    return value


def _normalize_catchup_run(raw, run_date, parent_run_id, reason):
    if not isinstance(raw, dict) or not _RUN_REQUIRED_FIELDS.issubset(raw):
        raise daily.SidecarError(
            "x_catchup_plan_invalid_response",
            "Catch-up run response is invalid",
        )

    run_id = _positive_integer(raw.get("id"), "run identity")
    expected_count = raw.get("expected_count")
    account_ids = raw.get("account_ids")
    counters = (
        raw.get("queued_count"),
        raw.get("published_count"),
        raw.get("failed_count"),
        raw.get("unknown_count"),
    )
    if (
        raw.get("parent_run_id") != int(parent_run_id)
        or raw.get("batch_kind") != "catchup"
        or raw.get("run_date") != run_date
        or raw.get("reason") != reason
        or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}",
            str(raw.get("source_date") or ""),
        )
        or raw.get("status") not in _RUN_STATUSES
        or not isinstance(expected_count, int)
        or isinstance(expected_count, bool)
        or expected_count < 1
        or expected_count > daily.MAX_DAILY_ACCOUNTS
        or not isinstance(account_ids, list)
        or len(account_ids) != expected_count
        or any(
            not isinstance(account_id, int)
            or isinstance(account_id, bool)
            or account_id <= 0
            for account_id in account_ids
        )
        or len(set(account_ids)) != expected_count
        or any(
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
            or value > expected_count
            for value in counters
        )
    ):
        raise daily.SidecarError(
            "x_catchup_plan_invalid_response",
            "Catch-up run identity is invalid",
        )

    _safe_string(raw.get("error_code"), "error code", 64)
    _safe_string(raw.get("error_message"), "error message", 240)
    for field in ("started_at", "finished_at", "created_at", "updated_at"):
        _safe_string(raw.get(field), field, 64)

    normalized = dict(raw)
    normalized["id"] = run_id
    normalized["account_ids"] = list(account_ids)
    return normalized


def _normalize_catchup_snapshot(raw, run_date, parent_run_id, reason):
    if not isinstance(raw, dict):
        raise daily.SidecarError(
            "x_catchup_plan_invalid_response",
            "Catch-up plan response is invalid",
        )
    found = raw.get("found")
    run = raw.get("run")
    queues = raw.get("queues")
    if not isinstance(found, bool) or not isinstance(queues, list):
        raise daily.SidecarError(
            "x_catchup_plan_invalid_response",
            "Catch-up plan response is invalid",
        )
    if not found:
        if run is not None or queues:
            raise daily.SidecarError(
                "x_catchup_plan_invalid_response",
                "Missing catch-up plan response is inconsistent",
            )
        return {"found": False, "run": None, "queues": []}

    normalized_run = _normalize_catchup_run(
        run,
        run_date,
        parent_run_id,
        reason,
    )
    expected_count = normalized_run["expected_count"]
    if normalized_run["status"] == "failed_preflight":
        if queues or normalized_run["queued_count"] != 0:
            raise daily.SidecarError(
                "x_catchup_plan_invalid_response",
                "Failed catch-up plan unexpectedly contains queues",
            )
        return {
            "found": True,
            "run": normalized_run,
            "queues": [],
        }
    if (
        len(queues) != expected_count
        or normalized_run["queued_count"] != expected_count
    ):
        raise daily.SidecarError(
            "x_catchup_plan_invalid_response",
            "Catch-up queue count is inconsistent",
        )

    normalized_queues = []
    queue_ids = set()
    account_ids = []
    ranks = []
    for raw_queue in queues:
        if (
            not isinstance(raw_queue, dict)
            or not _QUEUE_REQUIRED_FIELDS.issubset(raw_queue)
        ):
            raise daily.SidecarError(
                "x_catchup_plan_invalid_response",
                "Catch-up queue response is invalid",
            )
        queue_id = _positive_integer(raw_queue.get("id"), "queue identity")
        account_id = _positive_integer(
            raw_queue.get("account_id"),
            "queue account identity",
        )
        rank = _positive_integer(
            raw_queue.get("candidate_rank"),
            "candidate rank",
        )
        if (
            queue_id in queue_ids
            or account_id in account_ids
            or rank in ranks
            or raw_queue.get("run_id") is not None
            or raw_queue.get("catchup_run_id") != normalized_run["id"]
            or raw_queue.get("run_date") != run_date
            or raw_queue.get("source_date") != normalized_run["source_date"]
            or raw_queue.get("status") not in _QUEUE_STATUSES
        ):
            raise daily.SidecarError(
                "x_catchup_plan_invalid_response",
                "Catch-up queue identity is inconsistent",
            )
        for field in ("created_at", "updated_at"):
            _safe_string(raw_queue.get(field), "queue %s" % field, 64)
        queue_ids.add(queue_id)
        account_ids.append(account_id)
        ranks.append(rank)
        normalized_queues.append(dict(raw_queue))

    if (
        account_ids != normalized_run["account_ids"]
        or ranks != list(range(1, expected_count + 1))
    ):
        raise daily.SidecarError(
            "x_catchup_plan_invalid_response",
            "Catch-up queue order is inconsistent",
        )
    return {
        "found": True,
        "run": normalized_run,
        "queues": normalized_queues,
    }


class CatchupSidecarClient(daily.SidecarClient):
    """Strict one-off HTTP client layered over the production sidecar client."""

    def query_catchup_plan(self, run_date, parent_run_id, reason):
        requested_date = normalize_date(run_date, "run_date")
        result = self.post(
            CATCHUP_QUERY_PATH,
            {
                "run_date": requested_date,
                "parent_run_id": int(parent_run_id),
                "reason": str(reason),
            },
        )
        item = result.get("item")
        return _normalize_catchup_snapshot(
            item,
            requested_date,
            int(parent_run_id),
            str(reason),
        )

    def create_catchup_plan(
        self,
        run_date,
        source_date,
        parent_run_id,
        reason,
        candidates,
    ):
        if (
            not isinstance(candidates, list)
            or not 1 <= len(candidates) <= daily.MAX_DAILY_ACCOUNTS
        ):
            raise daily.DailyRunError("catch-up candidates are invalid")
        requested_accounts = []
        requested_materials = []
        requested_pool_items = []
        for candidate in candidates:
            if not isinstance(candidate, dict):
                raise daily.DailyRunError("catch-up candidate is invalid")
            account_id = candidate.get("account_id")
            material_id = str(candidate.get("material_id", "") or "")
            pool_item_id = candidate.get("pool_item_id")
            if (
                not isinstance(account_id, int)
                or isinstance(account_id, bool)
                or account_id <= 0
                or not re.fullmatch(r"[1-9][0-9]*", material_id)
                or not isinstance(pool_item_id, int)
                or isinstance(pool_item_id, bool)
                or pool_item_id <= 0
            ):
                raise daily.DailyRunError(
                    "catch-up candidate identity is invalid"
                )
            requested_accounts.append(account_id)
            requested_materials.append(material_id)
            requested_pool_items.append(pool_item_id)
        if (
            len(set(requested_accounts)) != len(candidates)
            or len(set(requested_materials)) != len(candidates)
            or len(set(requested_pool_items)) != len(candidates)
        ):
            raise daily.DailyRunError(
                "catch-up candidate identities are not unique"
            )

        requested_date = normalize_date(run_date, "run_date")
        payload = {
            "run_date": requested_date,
            "source_date": normalize_date(source_date, "source_date"),
            "parent_run_id": int(parent_run_id),
            "reason": str(reason),
            "candidates": candidates,
        }
        result = self.post(
            CATCHUP_CREATE_PATH,
            payload,
            write_may_have_happened=True,
        )
        item = result.get("item")
        if not isinstance(item, dict) or not isinstance(item.get("created"), bool):
            raise daily.SidecarError(
                "x_catchup_plan_invalid_response",
                "Catch-up plan creation response is invalid",
                502,
                unknown_outcome=True,
            )
        if isinstance(item.get("run"), dict):
            snapshot = {
                "found": True,
                "run": item.get("run"),
                "queues": item.get("queues"),
            }
        else:
            queues = item.get("queues")
            snapshot = {
                "found": True,
                "run": {
                    key: value
                    for key, value in item.items()
                    if key not in {"queues", "created"}
                },
                "queues": queues,
            }
        try:
            normalized = _normalize_catchup_snapshot(
                snapshot,
                requested_date,
                int(parent_run_id),
                str(reason),
            )
        except daily.SidecarError as exc:
            raise daily.SidecarError(
                exc.code,
                str(exc),
                502,
                unknown_outcome=True,
            ) from None
        if normalized["run"]["account_ids"] != requested_accounts:
            raise daily.SidecarError(
                "x_catchup_plan_invalid_response",
                "Catch-up plan account order is invalid",
                502,
                unknown_outcome=True,
            )
        return normalized

    def record_catchup_failure(
        self,
        run_date,
        source_date,
        parent_run_id,
        reason,
        expected_missing_count,
        error_code,
        error_message,
    ):
        requested_date = normalize_date(run_date, "run_date")
        result = self.post(
            CATCHUP_FAILURE_PATH,
            {
                "run_date": requested_date,
                "source_date": normalize_date(source_date, "source_date"),
                "parent_run_id": int(parent_run_id),
                "reason": str(reason),
                "expected_missing_count": int(expected_missing_count),
                "error_code": str(error_code),
                "error_message": str(error_message),
            },
            write_may_have_happened=True,
        )
        item = result.get("item")
        if not isinstance(item, dict) or not isinstance(item.get("recorded"), bool):
            raise daily.SidecarError(
                "x_catchup_failure_invalid_response",
                "Catch-up failure response is invalid",
                502,
                unknown_outcome=True,
            )
        if isinstance(item.get("run"), dict):
            raw_run = item["run"]
        else:
            raw_run = {
                key: value
                for key, value in item.items()
                if key != "recorded"
            }
        run = _normalize_catchup_run(
            raw_run,
            requested_date,
            int(parent_run_id),
            str(reason),
        )
        if run["status"] != "failed_preflight":
            raise daily.SidecarError(
                "x_catchup_failure_invalid_response",
                "Catch-up failure status is invalid",
                502,
                unknown_outcome=True,
            )
        return {"recorded": item["recorded"], "run": run}


def _validate_authorized_invocation(
    run_date,
    expected_missing_count,
    reason,
    *,
    now=None,
):
    requested_date = normalize_date(run_date, "run_date")
    if (
        requested_date != AUTHORIZED_RUN_DATE
        or expected_missing_count != AUTHORIZED_EXPECTED_MISSING_COUNT
        or reason != AUTHORIZED_REASON
    ):
        raise daily.DailyRunError(
            "catch-up invocation is outside the authorized one-off scope",
            code="x_post_catchup_scope_not_authorized",
        )
    current = shanghai_now(now)
    if current.date().isoformat() != requested_date:
        raise daily.DailyRunError(
            "catch-up run_date must be the current Beijing date",
            code="x_post_catchup_date_not_current",
        )
    return current


def _validate_parent_for_catchup(
    parent_plan,
    config,
    run_date,
    source_date,
    expected_missing_count,
):
    run = parent_plan["run"]
    queues = parent_plan["queues"]
    expected_count = int(run.get("expected_count") or 0)
    if (
        run.get("run_date") != run_date
        or run.get("source_date") != source_date
        or run.get("status") != "completed"
        or len(queues) != expected_count
        or int(run.get("queued_count") or 0) != expected_count
        or int(run.get("published_count") or 0) != expected_count
        or int(run.get("failed_count") or 0) != 0
        or int(run.get("unknown_count") or 0) != 0
        or any(queue.get("status") != "published" for queue in queues)
    ):
        raise daily.DailyRunError(
            "daily parent plan is not a fully published frozen batch",
            code="x_post_catchup_parent_not_complete",
        )
    parent_account_ids = tuple(int(queue.get("account_id") or 0) for queue in queues)
    if (
        len(set(parent_account_ids)) != expected_count
        or not set(parent_account_ids).issubset(set(config.account_ids))
    ):
        raise daily.DailyRunError(
            "daily parent account scope is inconsistent",
            code="x_post_catchup_parent_scope_conflict",
        )
    missing_account_ids = tuple(
        account_id
        for account_id in config.account_ids
        if account_id not in set(parent_account_ids)
    )
    if (
        len(missing_account_ids) != expected_missing_count
        or expected_count + expected_missing_count != len(config.account_ids)
    ):
        raise daily.DailyRunError(
            "configured scope does not contain the expected missing accounts",
            code="x_post_catchup_missing_scope_conflict",
        )
    return parent_account_ids, missing_account_ids


def _validate_existing_child(
    child_plan,
    parent_plan,
    config,
    source_date,
    expected_missing_count,
):
    child_run = child_plan["run"]
    parent_accounts = {
        int(queue.get("account_id") or 0)
        for queue in parent_plan["queues"]
    }
    child_accounts = tuple(child_run["account_ids"])
    if (
        child_run.get("source_date") != source_date
        or child_run.get("expected_count") != expected_missing_count
        or len(child_accounts) != expected_missing_count
        or set(child_accounts) & parent_accounts
        or not set(child_accounts).issubset(set(config.account_ids))
        or parent_accounts | set(child_accounts) != set(config.account_ids)
    ):
        raise daily.DailyRunError(
            "existing catch-up child scope is inconsistent",
            code="x_post_catchup_resume_conflict",
        )
    return child_accounts


def _record_catchup_failure_best_effort(
    sidecar,
    *,
    run_date,
    source_date,
    parent_run_id,
    reason,
    expected_missing_count,
    exc,
):
    recorder = getattr(sidecar, "record_catchup_failure", None)
    if not callable(recorder):
        return
    error_code, error_message = daily._failure_audit_fields(exc)
    try:
        recorder(
            run_date,
            source_date,
            parent_run_id,
            reason,
            expected_missing_count,
            error_code,
            error_message,
        )
    except Exception:
        return


def _publish_catchup_queues(
    config,
    sidecar,
    queues,
    *,
    run_date,
    source_date,
    parent_run_id,
    reason,
    expected_missing_count,
    preflight_rejected_count,
    resumed_existing_plan,
):
    result = daily._publish_daily_queues(
        config,
        sidecar,
        queues,
        run_date=run_date,
        source_date=source_date,
        preflight_rejected_count=preflight_rejected_count,
        resumed_existing_plan=resumed_existing_plan,
    )
    result.update(
        {
            "workflow": "catchup",
            "parent_run_id": int(parent_run_id),
            "reason": reason,
            "expected_missing_count": int(expected_missing_count),
        }
    )
    return result


def execute_catchup_run(
    config,
    *,
    run_date,
    expected_missing_count,
    reason,
    sidecar=None,
    connection_factory=None,
    pool_candidate_loader=select_pool_candidates,
    downloader=download_media,
    prober=probe_media,
    repair_client=None,
    now=None,
):
    """Execute or recover the explicitly authorized six-account child batch."""
    config.validate()
    current = _validate_authorized_invocation(
        run_date,
        expected_missing_count,
        reason,
        now=now,
    )
    source_date = previous_source_date(current)
    sidecar = sidecar or CatchupSidecarClient(
        config.internal_url,
        config.internal_token,
        timeout=config.internal_timeout,
    )
    if repair_client is None and config.repair_url:
        repair_client = daily.MediaRepairClient(
            config.repair_url,
            config.repair_token,
            timeout=config.repair_timeout,
            max_output_bytes=config.max_media_bytes,
        )

    parent_plan = sidecar.query_daily_plan(config.plan_query_path, run_date)
    if not parent_plan["found"]:
        raise daily.DailyRunError(
            "daily parent plan does not exist",
            code="x_post_catchup_parent_missing",
        )
    parent_run = parent_plan["run"]
    parent_run_id = int(parent_run.get("id") or 0)
    if parent_run_id <= 0:
        raise daily.DailyRunError(
            "daily parent plan identity is invalid",
            code="x_post_catchup_parent_invalid",
        )

    # Query the immutable child before any account refresh, pool read, source
    # query, download, media repair, or reservation.
    child_plan = sidecar.query_catchup_plan(
        run_date,
        parent_run_id,
        reason,
    )
    if child_plan["found"]:
        _validate_existing_child(
            child_plan,
            parent_plan,
            config,
            source_date,
            expected_missing_count,
        )
        child_run = child_plan["run"]
        if child_run["status"] == "failed_preflight":
            return {
                "status": "failed_preflight",
                "workflow": "catchup",
                "run_date": run_date,
                "source_date": source_date,
                "parent_run_id": parent_run_id,
                "catchup_run_id": child_run["id"],
                "reason": reason,
                "expected_missing_count": expected_missing_count,
                "planned_count": 0,
                "published_count": 0,
                "preflight_rejected_count": 0,
                "resumed_existing_plan": True,
                "results": [],
            }
        return _publish_catchup_queues(
            config,
            sidecar,
            child_plan["queues"],
            run_date=run_date,
            source_date=source_date,
            parent_run_id=parent_run_id,
            reason=reason,
            expected_missing_count=expected_missing_count,
            preflight_rejected_count=0,
            resumed_existing_plan=True,
        )

    try:
        _, missing_account_ids = _validate_parent_for_catchup(
            parent_plan,
            config,
            run_date,
            source_date,
            expected_missing_count,
        )
        sidecar.preflight_storage(config.storage_preflight_path)
        verified_accounts = [
            daily._safe_account(sidecar.verify_account(account_id))
            for account_id in missing_account_ids
        ]
        if tuple(item["id"] for item in verified_accounts) != missing_account_ids:
            raise daily.DailyRunError(
                "verified catch-up account order does not match missing scope",
                code="x_post_catchup_account_mismatch",
            )

        pool_items = sidecar.available_pool_items(
            config.pool_available_path,
            config.scan_limit,
        )
        if len(pool_items) < expected_missing_count:
            raise daily.DailyRunError(
                "fewer than %s unused materials are available for catch-up"
                % expected_missing_count,
                code="x_post_catchup_pool_shortage",
            )
        connection_factory = connection_factory or daily._connect_from_config
        connection = connection_factory(config)
        try:
            candidates, selector_rejections = pool_candidate_loader(
                connection,
                pool_items,
                source_date,
                limit=config.candidate_pool_limit,
                schema=config.mysql_database,
            )
        finally:
            close = getattr(connection, "close", None)
            if callable(close):
                close()
        daily._record_pool_checks_best_effort(
            sidecar,
            config,
            selector_rejections,
        )
        if len(candidates) < expected_missing_count:
            raise daily.DailyRunError(
                "fewer than %s compliant catch-up candidates were found"
                % expected_missing_count,
                code="x_post_catchup_candidate_shortage",
            )

        planned_candidates, preflight_failures = daily._preflight_candidates(
            config,
            candidates,
            verified_accounts,
            max(1, int(current.timestamp())),
            downloader,
            prober,
            repair_client,
        )
        daily._record_pool_checks_best_effort(
            sidecar,
            config,
            preflight_failures,
        )
        if len(planned_candidates) != expected_missing_count:
            raise daily.DailyRunError(
                "only %s catch-up candidates passed all preflight gates "
                "(required %s)"
                % (len(planned_candidates), expected_missing_count),
                code="x_post_catchup_candidate_preflight_shortage",
            )
        sidecar.preflight_storage(config.storage_preflight_path)
    except Exception as exc:
        _record_catchup_failure_best_effort(
            sidecar,
            run_date=run_date,
            source_date=source_date,
            parent_run_id=parent_run_id,
            reason=reason,
            expected_missing_count=expected_missing_count,
            exc=exc,
        )
        raise

    try:
        created = sidecar.create_catchup_plan(
            run_date,
            source_date,
            parent_run_id,
            reason,
            planned_candidates,
        )
    except daily.SidecarError as exc:
        if not exc.unknown_outcome:
            _record_catchup_failure_best_effort(
                sidecar,
                run_date=run_date,
                source_date=source_date,
                parent_run_id=parent_run_id,
                reason=reason,
                expected_missing_count=expected_missing_count,
                exc=exc,
            )
        raise

    queues = created["queues"]
    if tuple(int(queue.get("account_id") or 0) for queue in queues) != missing_account_ids:
        raise daily.DailyRunError(
            "created catch-up queue order does not match missing accounts",
            code="x_post_catchup_created_scope_conflict",
        )
    return _publish_catchup_queues(
        config,
        sidecar,
        queues,
        run_date=run_date,
        source_date=source_date,
        parent_run_id=parent_run_id,
        reason=reason,
        expected_missing_count=expected_missing_count,
        preflight_rejected_count=len(preflight_failures),
        resumed_existing_plan=False,
    )


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Run the explicitly authorized one-off X Post scope-expansion catch-up."
        )
    )
    parser.add_argument("--run-date", required=True)
    parser.add_argument("--expected-missing-count", required=True, type=int)
    parser.add_argument("--reason", required=True)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        config = daily.DailyConfig.from_env()
        with daily.process_lock(config.lock_path) as acquired:
            if acquired is None:
                result = {
                    "status": "skipped_locked",
                    "workflow": "catchup",
                    "run_date": args.run_date,
                    "reason": args.reason,
                }
            else:
                result = execute_catchup_run(
                    config,
                    run_date=args.run_date,
                    expected_missing_count=args.expected_missing_count,
                    reason=args.reason,
                )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if result.get("status") == "published" else 1
    except (daily.DailyRunError, CandidateSelectionError, XPostError) as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_code": str(
                        getattr(exc, "code", type(exc).__name__)
                    )[:64],
                    "message": redact_text(str(exc), 240),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_code": "unexpected_error",
                    "message": type(exc).__name__,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
