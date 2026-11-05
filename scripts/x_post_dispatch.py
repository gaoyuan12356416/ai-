"""Bounded account-isolated dispatch of immutable X queue identities."""
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import time


def account_scope(queue):
    return {int(queue[key]) for key in ("account_id", "relay_account_id")
            if int(queue.get(key) or 0) > 0}


def dispatch_queues(queues, publish, *, workers=1, max_seconds=900):
    """Never retry a row; serialize overlapping targets/relays and drain others.

    The canonical sidecar remains the cross-process authority for identity,
    account fences and the atomic zero-attempt-to-publishing transition.
    """
    workers = max(1, min(4, int(workers)))
    results = {}
    remaining = list(enumerate(queues))
    pending = {}
    shared_stop = False
    deadline = time.monotonic() + max_seconds

    def snapshot(queue):
        entry = {"queue_id": queue["id"], "account_id": queue["account_id"],
                 "status": queue["status"]}
        source_confirmed = (queue.get("delivery_mode") == "premium_relay_repost"
                            and queue.get("repost_status") == "source_published"
                            and not queue.get("unknown_outcome"))
        if queue.get("unknown_outcome") or (queue["status"] == "publishing" and not source_confirmed):
            entry.update(status="needs_review", unknown_outcome=True,
                         error_code="x_post_unknown_outcome")
            return entry
        if queue["status"] in {"published", "failed"}:
            if queue["status"] == "failed":
                entry["error_code"] = queue.get("error_code") or "x_post_retry_requires_review"
            return entry
        return None

    # Keep uncertain rows readback-only without holding their siblings.
    for index, queue in list(remaining):
        entry = snapshot(queue)
        if entry is not None:
            results[index] = entry
            remaining.remove((index, queue))

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="x-frozen-post") as pool:
        while remaining or pending:
            occupied = set().union(*(account_scope(q) for _, q in pending.values())) if pending else set()
            for index, queue in list(remaining):
                scope = account_scope(queue)
                if shared_stop or time.monotonic() >= deadline:
                    results[index] = {"queue_id": queue["id"], "account_id": queue["account_id"],
                                      "status": "queued", "error_code": "x_post_shared_backoff" if shared_stop else "x_post_tick_budget"}
                    remaining.remove((index, queue))
                    continue
                if len(pending) >= workers or scope & occupied:
                    continue
                pending[pool.submit(publish, queue)] = (index, queue)
                occupied.update(scope)
                remaining.remove((index, queue))
            if not pending:
                break
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                index, queue = pending.pop(future)
                try:
                    entry = future.result()
                except Exception as exc:
                    # A caller/transport exception without a receipt is never a
                    # reason to reissue this write; the sidecar ledger owns it.
                    entry = {"queue_id": queue["id"], "account_id": queue["account_id"],
                             "status": "needs_review", "unknown_outcome": True,
                             "error_code": getattr(exc, "code", "x_post_dispatch_outcome_unknown")}
                results[index] = entry
                if entry.get("shared_stop"):
                    shared_stop = True
    return [results[index] for index in sorted(results)], shared_stop
