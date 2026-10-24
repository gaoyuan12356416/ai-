#!/usr/bin/env python3
"""CPU command line entry point; no import of the production web application."""
import argparse
from collections import Counter
from datetime import datetime, timedelta
import json
import math
import os
from pathlib import Path
import re
import signal
import sys
import threading
import uuid

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1]))

from features.fb_ad_asset_delete import store as shared_store
from features.fb_ad_asset_delete.store import Store, StoreError
from meta_violation_cleanup.graph import GRAPH_METRIC_FIELDS, GraphClient
from meta_violation_cleanup.notifications import Notifier
from meta_violation_cleanup.runner import ACTOR, Runner
from meta_violation_cleanup.runtime import Runtime, CST, digest, process_lock, verify_mount
from meta_violation_cleanup.sql_source import SqlSource

_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}\Z")
_PHASE = re.compile(r"(?:start|end)(?:-[2-9][0-9]*|-[1-9][0-9]+)?\Z")
_USER_ID = re.compile(r"[1-9][0-9]{0,31}\Z")


class CliError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def load_config(path):
    try:
        config = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, UnicodeError):
        raise CliError("config_unavailable") from None
    if not isinstance(config, dict):
        raise CliError("invalid_config")
    for key in ("runtime_dir", "shared_ledger", "env_file", "feishu_config", "legacy_lock"):
        if not isinstance(config.get(key), str) or not config[key]:
            raise CliError("missing_config_path")
    if not isinstance(config.get("live_enabled", False), bool):
        raise CliError("invalid_live_setting")
    for key,default,minimum,maximum in (
            ('execution_workers',1,1,16),('execution_per_account',2,1,2),
            ('execution_batch_size',32,1,64),('execution_context_ttl_seconds',30,5,30),
            ('execution_max_rps',24,1,30)):
        value=config.get(key,default)
        if type(value) is not int or not minimum<=value<=maximum:raise CliError('invalid_'+key)
    return config


def validate_paths(config):
    verify_mount()
    base = Path("/mnt/data-disk").resolve()
    for key in ("runtime_dir", "shared_ledger"):
        target = Path(config[key]).resolve()
        if base not in target.parents:
            raise CliError("data_disk_path_required")


def validate_run_id(value):
    if not isinstance(value, str) or not _RUN_ID.fullmatch(value):
        raise CliError("invalid_run_id")
    return value


def _safe_counts(value):
    if not isinstance(value, dict):
        return {}
    return {str(k): v for k, v in value.items()
            if re.fullmatch(r"[A-Za-z0-9_]{1,80}", str(k)) and isinstance(v, int) and not isinstance(v, bool)}


def _safe_graph_metrics(value):
    if not isinstance(value, dict):
        return {}
    return {key: value[key] for key in GRAPH_METRIC_FIELDS
            if isinstance(value.get(key), (int, float)) and not isinstance(value[key], bool)
            and 0 <= value[key] <= 10 ** 15 and math.isfinite(value[key])}


def summary(runtime, run_id):
    run = runtime.get(run_id)
    data = run["data"]
    result = {key: run.get(key) for key in ("id", "status", "started", "ended")}
    result["mode"] = data.get("execution_mode", run.get("mode"))
    result["stage"] = data.get("stage")
    for key in ("window_start", "window_end", "manifest_sha256", "execution_attempt"):
        if key in data:
            result[key] = data[key]
    for key in ("vio_rows", "policy_rows", "material_count", "candidate_ads", "candidate_creatives",
                "accounts_total", "accounts_finished", "coverage_errors", "rule1_ads", "rule2_ads",
                "identities_total", "identities_finished", "identities_failed", "accounts_discovered",
                "shared_creatives", "remaining_ads", "deferred_creatives", "blocked_account_creatives",
                "target_ads", "target_ads_checked", "publication_rows", "recovery_creatives"):
        if isinstance(data.get(key), int):
            result[key] = data[key]
    for key in ("result_counts", "product_candidate_ads", "skip_counts", "reconciliation_counts"):
        if key in data:
            result[key] = _safe_counts(data[key])
    if "graph_metrics" in data:
        result["graph_metrics"] = _safe_graph_metrics(data["graph_metrics"])
    for key in ('scan_timings','lookup_metrics','execution_metrics'):
        if key in data:result[key]=data[key]
    attempt = data.get("execution_attempt", 1)
    attempt = attempt if isinstance(attempt, int) and attempt > 0 else 1
    result["notifications"] = {}
    for base in ("start", "end"):
        phase = base if attempt == 1 else "%s-%d" % (base, attempt)
        notice = runtime.notice(run_id, phase)
        if notice:
            result["notifications"][phase] = {"status": notice["status"]}
            try:
                receipt = json.loads(notice.get("receipt") or "{}")
                message_id = str(receipt.get("message_id") or "")
                if re.fullmatch(r"[A-Za-z0-9_:-]{1,128}", message_id):
                    result["notifications"][phase]["message_id"] = message_id
            except (ValueError, TypeError):
                pass
    return result


def open_store(config):
    if not Path(config["shared_ledger"]).is_file():
        raise CliError("shared_ledger_missing")
    return Store(config["shared_ledger"])


def recover_owned(runtime, config, send_notifications=False):
    recovery = runtime.recover_interrupted()
    store = None
    for run_id in recovery.get("run_ids", []):
        run = runtime.get(run_id)
        expected = "policy-cleanup-" + validate_run_id(run_id)
        recorded_job = run["data"].get("shared_job_id")
        if recorded_job and recorded_job != expected:
            raise CliError("recovery_job_scope_mismatch")
        if store is None:
            store = open_store(config)
        try:
            job = store.get_job(expected)
        except StoreError as exc:
            if exc.code != "not_found":
                raise
            job = None
        if job is not None:
            # The job can exist even when SIGKILL occurred immediately after
            # shared claim_run and before its IDs reached the runtime ledger.
            manifest_hash = run["data"].get("manifest_sha256")
            if not isinstance(manifest_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", manifest_hash):
                frozen = runtime.load_artifact(run_id, "manifest.json")
                manifest_hash = digest({k: v for k, v in frozen.items() if k != "sha256"})
                if frozen.get("run_id") != run_id or frozen.get("sha256") != manifest_hash:
                    raise CliError("recovery_manifest_invalid")
            if (job.get("job_id") != expected or job.get("actor") != ACTOR or
                    job.get("preview_id") != manifest_hash or job.get("manifest_sha256") != manifest_hash):
                raise CliError("recovery_job_scope_mismatch")
            for shared_run in job.get("runs", []):
                if shared_run.get("status") != "running":
                    continue
                if shared_run.get("job_id") != expected or shared_run.get("actor") != ACTOR or shared_run.get("preview_id") != manifest_hash:
                    raise CliError("recovery_run_scope_mismatch")
                pid, identity = shared_run.get("owner_pid"), shared_run.get("owner_start")
                if (not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0 or
                        not isinstance(identity, str) or not identity or
                        shared_store._owner_alive(pid, identity) is not False):
                    raise CliError("recovery_owner_unresolved")
                store.finish_run(shared_run["run_id"], "interrupted", {"recovered_by_cleanup_cli": True})
        # Runtime atomically sets recovery_pending before exposing recovered IDs.
        # Clear it only after every owned dead shared run is fenced (or the exact
        # deterministic job is confirmed absent); failures remain retryable.
        runtime.update(run_id, recovery_pending=False)
        if send_notifications and run['data'].get('execution_mode', run.get('mode')) == 'run':
            attempt = int(run['data'].get('execution_attempt', 1))
            phase = 'end' if attempt == 1 else 'end-%d' % attempt
            notice = runtime.notice(run_id, phase)
            if notice and notice['status'] == 'sent':
                continue
            if notice and notice['status'] in ('sending', 'unknown'):
                runtime.event(run_id, 'recovery_notice_pending', {'phase': phase, 'reason': 'notice_requires_reconciliation'})
                continue
            try:
                body = Runner(config, None, None, runtime, None, None).end_text(run_id)
                Notifier(runtime, config['feishu_config']).send(run_id, phase, body)
            except Exception:
                # The notification ledger retains failed/unknown outcomes. No
                # deletion is replayed to recover a crashed worker's notice.
                runtime.event(run_id, 'recovery_notice_pending', {'phase': phase})


def open_graph(config, source):
    workers = config.get("graph_read_workers", 4)
    if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= 16:
        raise CliError("invalid_graph_read_workers")
    tokens = source.credentials()
    if (not isinstance(tokens, dict) or not tokens or
            any(not _USER_ID.fullmatch(str(uid)) or not isinstance(token, str) or not token.strip()
                for uid, token in tokens.items())):
        raise CliError("invalid_credential_directory")
    return GraphClient(tokens, version=config.get("graph_version", "v25.0"), read_workers=workers)


def load_manifest(runtime, run_id):
    manifest = runtime.load_artifact(run_id, "manifest.json")
    if manifest.get("run_id") != run_id:
        raise CliError("manifest_run_mismatch")
    if digest({k: v for k, v in manifest.items() if k != "sha256"}) != manifest.get("sha256"):
        raise CliError("manifest_hash_mismatch")
    try:
        frozen = datetime.fromisoformat(manifest["frozen_at"])
        if frozen.tzinfo is None:
            frozen = frozen.replace(tzinfo=CST)
        age = (datetime.now(CST) - frozen.astimezone(CST)).total_seconds()
    except (KeyError, ValueError, TypeError):
        raise CliError("invalid_manifest_window") from None
    if not 0 <= age <= 3600:
        raise CliError("manifest_expired_start_new_scan")
    return manifest


def scan_or_run(args, config, runtime, stop):
    live = args.command == "run"
    if live and config.get('comparison_baseline_run_id'):
        raise CliError('comparison_config_is_read_only')
    if live and config.get("live_enabled") is not True:
        raise CliError("live_execution_disabled")
    run_id = validate_run_id(args.run_id or datetime.now(CST).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8])
    end = datetime.now(CST).replace(microsecond=0)
    start = end - timedelta(days=30)
    created = runtime.begin(run_id, args.command, {"window_start": start.isoformat(), "window_end": end.isoformat(), "stage": "starting"})
    manifest = None
    if not created:
        previous = runtime.get(run_id)
        if not live or previous["status"] == "completed":
            return summary(runtime, run_id), 0
        if previous["status"] not in ("previewed", "partial", "interrupted"):
            raise CliError("existing_run_requires_recovery")
        manifest = load_manifest(runtime, run_id)
        if manifest.get('comparison_only'):
            raise CliError('comparison_manifest_is_read_only')
        start, end = manifest["window_start"], manifest["window_end"]
    else:
        start, end = start.isoformat(), end.isoformat()
    notifier = Notifier(runtime, config["feishu_config"]) if live else None
    runner = Runner(config, None, None, runtime, None, notifier, stop.is_set)
    source = graph = None
    status, error, notification_failed = "failed", None, False
    data = runtime.get(run_id)["data"]
    attempt = int(data.get("execution_attempt", 0)) + 1 if live else 0
    start_phase = "start" if attempt == 1 else "start-%d" % attempt
    end_phase = "end" if attempt == 1 else "end-%d" % attempt
    try:
        runtime.update(run_id, status="running", execution_mode=args.command,
                       execution_attempt=attempt, stage="starting",
                       execution_started_at=datetime.now(CST).isoformat(timespec="seconds"))
        runner.check_stop()
        if live:
            notifier.send(run_id, start_phase, runner.start_text(run_id, start, end))
            notice = runtime.notice(run_id, start_phase)
            if not notice or notice["status"] != "sent":
                raise CliError("start_notification_not_confirmed")
            runner.store = open_store(config)
        source = SqlSource(config)
        graph = open_graph(config, source)
        runner.source, runner.graph = source, graph
        runner.check_stop()
        if manifest is None:
            manifest = runner.scan(run_id, start, end)
        runner.check_stop()
        if live:
            status = runner.execute(run_id, manifest, max_ads=args.max_ads, start_phase=start_phase)
        else:
            status = "previewed"
    except (KeyboardInterrupt, InterruptedError) as exc:
        status, error = "interrupted", type(exc).__name__
    except Exception as exc:
        status, error = "failed", type(exc).__name__
        # Controlled internal error labels are useful without logging SQL,
        # database credentials, Graph bodies or arbitrary exception messages.
        label=str(exc)
        if re.fullmatch(r'[a-z][a-z0-9_]{0,95}',label):
            runtime.event(run_id,'failure',{'type':error,'code':label})
    finally:
        try:
            runtime.update(run_id, status=status, stage="finished", error_type=error,
                           sql_metrics=source.safe_metrics if source is not None else {},
                           graph_metrics=_safe_graph_metrics(getattr(graph, 'safe_metrics', {})))
        except Exception:
            status, error = "failed", "RuntimeLedgerError"
        if live:
            try:
                body = runner.end_text(run_id) if error != "RuntimeLedgerError" else (
                    "Meta违规素材清理｜异常结束\n运行：%s\n本地账本不可写；已停止新删除，请检查共享账本。" % run_id)
                notifier.send(run_id, end_phase, body)
            except Exception:
                notification_failed = True
        for resource in (graph, source):
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    pass
    output = summary(runtime, run_id)
    if error:
        output["error_type"] = error
    if notification_failed:
        output["end_notification_failed"] = True
    return output, 130 if status == "interrupted" else (1 if status == "failed" or notification_failed else 0)


def reconcile(args, config, runtime, stop):
    run_id = validate_run_id(args.run_id)
    run = runtime.get(run_id)
    expected = "policy-cleanup-" + run_id
    if run["data"].get("shared_job_id") != expected:
        raise CliError("run_has_no_owned_shared_job")
    store = open_store(config)
    job = store.get_job(expected)
    if job.get("actor") != ACTOR:
        raise CliError("shared_job_actor_mismatch")
    source = graph = None
    counts, results = Counter(), []
    try:
        source = SqlSource(config)
        graph = open_graph(config, source)
        for obj in job.get("objects", []):
            if stop.is_set():
                raise InterruptedError("stop_requested")
            if obj.get("status") != "unknown":
                continue
            kind, identifier = obj.get("kind"), str(obj.get("object_id", ""))
            if kind not in ("ad", "creative") or not _USER_ID.fullmatch(identifier):
                counts["invalid_object_scope"] += 1
                continue
            credential = str((obj.get("result") or {}).get("credential_user_id") or "")
            users, accounts = obj.get("user_ids", []), obj.get("account_ids", [])
            if not credential and len(users) == 1:
                credential = str(users[0])
            if credential not in graph.tokens or len(accounts) != 1:
                counts["credential_or_account_unavailable"] += 1
                continue
            # This path deliberately has no Runner and no call to Graph.delete.
            outcome = graph.reconcile(kind, identifier, credential, str(accounts[0]))
            verified = outcome.get("status") == "deleted_verified"
            record = dict(outcome, confirmed_deleted=verified, credential_user_id=credential)
            store.reconcile_object(expected, obj["key"], "deleted" if verified else "unknown", record)
            counts["deleted_verified" if verified else "unknown"] += 1
            results.append({"kind": kind, "id": identifier, "result": record})
        runtime.artifact(run_id, "reconciliation-%s.json" % uuid.uuid4().hex[:12], results)
        runtime.update(run_id, reconciliation_counts=dict(counts))
    finally:
        for resource in (graph, source):
            if resource is not None:
                resource.close()
    return {"run_id": run_id, "reconciliation_counts": dict(counts)}, 0


def notify(args, config, runtime):
    run_id = validate_run_id(args.run_id)
    phase = args.phase
    if not _PHASE.fullmatch(phase):
        raise CliError("invalid_notification_phase")
    run = runtime.get(run_id)
    notice = runtime.notice(run_id, phase)
    if notice:
        body = notice["body"]
    elif phase.startswith("end") and run["status"] != "running" and run["data"].get("execution_mode") == "run":
        attempt = int(run["data"].get("execution_attempt", 1))
        expected = "end" if attempt == 1 else "end-%d" % attempt
        if phase != expected:
            raise CliError("notification_attempt_mismatch")
        body = Runner(config, None, None, runtime, None, None).end_text(run_id)
    else:
        raise CliError("notification_body_not_prepared")
    Notifier(runtime, config["feishu_config"]).send(run_id, phase, body)
    return summary(runtime, run_id), 0


def parser():
    result = argparse.ArgumentParser(description="DramaWave/HotDrama Meta violation material cleanup")
    result.add_argument("--config", required=True)
    commands = result.add_subparsers(dest="command", required=True)
    for name in ("preview", "run", "status", "reconcile", "notify"):
        command = commands.add_parser(name)
        command.add_argument("--run-id", required=name in ("reconcile", "notify"))
        if name == "run":
            command.add_argument("--max-ads", type=int)
        if name == "notify":
            command.add_argument("--phase", default="end")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    runtime = None
    stop = threading.Event()
    handlers = {}
    try:
        os.umask(0o077)
        if args.run_id:
            validate_run_id(args.run_id)
        if args.command == "run" and args.max_ads is not None and args.max_ads <= 0:
            raise CliError("max_ads_must_be_positive")
        config = load_config(args.config)
        validate_paths(config)
        runtime = Runtime(config["runtime_dir"])
        for signum in (signal.SIGINT, signal.SIGTERM):
            handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, lambda *_: stop.set())
        if args.command == "status":
            output = summary(runtime, args.run_id) if args.run_id else {"runs": [summary(runtime, row["id"]) for row in runtime.list()]}
            code = 0
        else:
            with process_lock(Path(config["runtime_dir"]) / "process.lock"):
                if args.command == "run":
                    recover_owned(runtime, config, send_notifications=True)
                elif args.command == "reconcile":
                    recover_owned(runtime, config, send_notifications=False)
                if args.command in ("preview", "run"):
                    output, code = scan_or_run(args, config, runtime, stop)
                elif args.command == "reconcile":
                    output, code = reconcile(args, config, runtime, stop)
                else:
                    output, code = notify(args, config, runtime)
        print(json.dumps(output, ensure_ascii=False), flush=True)
        return code
    except (KeyboardInterrupt, InterruptedError):
        print(json.dumps({"error": "interrupted"}), flush=True)
        return 130
    except Exception as exc:
        error = exc.code if isinstance(exc, CliError) else "operation_failed"
        print(json.dumps({"error": error, "error_type": type(exc).__name__}), flush=True)
        return 1
    finally:
        for signum, handler in handlers.items():
            signal.signal(signum, handler)
        if runtime is not None:
            try:
                runtime.db.close()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
