#!/usr/bin/env python3
"""Exercise five real screening candidates without Graph writes or notifications."""
import argparse
import fcntl
import hashlib
import importlib.util
import json
import shutil
from pathlib import Path
from types import SimpleNamespace


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    output = Path(args.output_dir).resolve()
    if not str(output).startswith("/mnt/data-disk/"):
        raise RuntimeError("verification artifacts must use the mounted data disk")
    output.mkdir(parents=True, exist_ok=True)
    spec = importlib.util.spec_from_file_location(
        "daily_verification", "/root/codex_test/fb_2446289_daily_replace_delete.py"
    )
    daily = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(daily)
    lock = daily.LOCK_PATH.open("w")
    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    history = daily.SUCCESS_HISTORY_PATH
    original_cache = daily.PAGE_HEALTH_CACHE_PATH
    before = {"success_history": digest(history), "page_cache": digest(original_cache)}
    daily.OUT_DIR = output
    daily.LOG_DIR = output
    daily.SUCCESS_HISTORY_PATH = output / "success_history.json"
    daily.PAGE_HEALTH_CACHE_PATH = output / "page_health_cache.json"
    if history.exists():
        shutil.copy2(str(history), str(daily.SUCCESS_HISTORY_PATH))
    if original_cache.exists():
        shutil.copy2(str(original_cache), str(daily.PAGE_HEALTH_CACHE_PATH))

    original_query = daily.mysql_query
    selection = {}

    def sampled_query(port, sql, timeout=180):
        rows = original_query(port, sql, timeout=timeout)
        if "MIN(created_at) AS first_failed_at" in sql:
            selection["full_window_candidate_rows"] = len(rows)
            rows = rows[:5]
            selection["sampled_candidate_rows"] = len(rows)
        return rows

    daily.mysql_query = sampled_query
    original_graph = daily._orig_graph_request
    graph_reads = []

    def readonly_graph(method, path, token, params=None, **kwargs):
        if method.upper() != "GET":
            raise RuntimeError("verification blocked non-GET Graph request")
        graph_reads.append({"method": method.upper(), "path": path})
        return original_graph(method, path, token, params, **kwargs)

    def forbidden(*_args, **_kwargs):
        raise RuntimeError("verification blocked a mutating operation")

    def no_notify(message, notify=True):
        if notify:
            raise RuntimeError("verification blocked notification")
        daily.log("verification notification suppressed")
        return {"skipped": True}

    daily._orig_graph_request = readonly_graph
    daily._orig_graph_upload_file = forbidden
    daily.safe_notify = no_notify
    daily.DailyExecutor.run = forbidden
    daily.update_success_history = forbidden
    result = daily.run_once(SimpleNamespace(
        hours=24, workers=1, graph_timeout=25, dry_run=True, no_notify=True, limit=5
    ))
    after = {"success_history": digest(history), "page_cache": digest(original_cache)}
    report = {
        "mode": "sampled_readonly_verification",
        "selection": selection,
        "screening": result["screening"],
        "counts": result["counts"],
        "graph_get_count": len(graph_reads),
        "graph_writes": 0,
        "notifications": 0,
        "production_state_unchanged": before == after,
        "before_sha256": before,
        "after_sha256": after,
    }
    (output / "verification.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not selection.get("sampled_candidate_rows"):
        raise RuntimeError("no real candidates available for sample verification")
    if before != after:
        raise RuntimeError("production state changed during verification")


if __name__ == "__main__":
    main()
