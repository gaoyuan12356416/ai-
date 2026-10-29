#!/usr/bin/env python3
"""Hourly discovery and independent cover-replacement worker."""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import threading
import time
import uuid

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1]))

from run import load_config, validate_paths
from features.fb_ad_asset_delete.store import Store
from meta_violation_cleanup.cover_source import CoverSource, scan
from meta_violation_cleanup.cover_store import CoverStore
from meta_violation_cleanup.cover_graph import CoverGraph
from meta_violation_cleanup.cover_payload import build_payload, payload_key, PayloadError
from meta_violation_cleanup.graph import GraphError
from meta_violation_cleanup.runtime import Runtime, CST, process_lock, legacy_delete_lock, now, digest, LegacyPriorityWindow
from meta_violation_cleanup.cover_performance import idle_delay, record_cycle
from meta_violation_cleanup.notifications import Notifier
from meta_violation_cleanup.runner import safe_error
from meta_violation_cleanup.throttle import Governor


class CoverWritePaused(RuntimeError):
    exit_status = 78


def batch_id(prefix='cover'):
    return prefix + '-' + datetime.now(CST).strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:8]


def report_body(batch, counts, global_counts=None):
    tasks, deletions = counts['tasks'], counts['deletions']
    meta = batch.get('meta') or {}
    return '\n'.join([
        'Meta 违规素材封面替换',
        '批次：' + batch['id'],
        '完成时间：' + now(),
        '范围：DramaWave / HotDrama，近3天≥3个不同违规广告',
        '新增达标素材：%d；新增关联广告：%d' % (meta.get('new_materials', 0), meta.get('new_ads', 0)),
        '广告替换：成功 %d，失败 %d，未知 %d，跳过 %d' % (
            tasks.get('succeeded', 0), tasks.get('failed', 0), tasks.get('unknown', 0),
            tasks.get('obsolete', 0) + tasks.get('blocked', 0)),
        '等待审核恢复：%d' % tasks.get('waiting_review', 0),
        '旧Creative：确认删除 %d，历史已删 %d，失败 %d，未知 %d' % (
            deletions.get('deleted_verified', 0), deletions.get('previous_deleted', 0),
            deletions.get('failed', 0) + deletions.get('delete_failed', 0),
            deletions.get('unknown', 0) + deletions.get('delete_unknown', 0)),
        '引用/读取暂缓：%d；广告积压：%d' % (
            deletions.get('waiting_references', 0) + deletions.get('reference_scan_incomplete', 0),
            sum((global_counts or counts)['tasks'].get(k, 0) for k in ('pending','in_progress'))),
        ('扫描异常：' + str((batch.get('summary') or {}).get('code', 'scan_failed')))
        if batch['status'] == 'failed' else '明确失败不自动重试；未知结果只读核实。',
    ])


def notify_completed(store, audit, config, include_pending=False):
    # Batch completions no longer send. Only the 09:30 daily timer delivers.
    # Keep historical notices/frozen bodies intact for receipt reconciliation.
    return


def _notify_completed(store, audit, config, include_pending=False):
    notifier = Notifier(audit, config['feishu_config'])
    for batch in store.notification_candidates(include_pending=include_pending):
        counts = store.stats(batch['id'])
        previous = batch.get('summary') or {}
        if batch.get('notified') and previous.get('counts') == counts:
            continue
        meta = batch.get('meta') or {}
        substantive = (batch['status'] == 'failed' or meta.get('new_ads', 0) or meta.get('new_materials', 0) or
                       any(v for k, v in counts['tasks'].items() if k not in ('waiting_review', 'pending')) or
                       any(v for k, v in counts['deletions'].items() if k not in ('waiting_references', 'reference_scan_incomplete')))
        attempt = int(previous.get('notification_attempt', 0)) + 1
        summary = {'counts': counts, 'notification_attempt': attempt}
        if not substantive:
            store.mark_batch_notified(batch['id'], summary)
            continue
        phase = 'end' if attempt == 1 else 'end-%d' % attempt
        try:
            audit.get(batch['id'])
        except ValueError:
            audit.begin(batch['id'], 'run', {'policy_version': '20261028-cover-v1'})
        existing = audit.notice(batch['id'], phase)
        if existing and existing['status'] in ('sending', 'unknown'):
            continue
        if existing and existing['status'] == 'failed':
            try:
                age = (datetime.now(CST) - datetime.fromisoformat(existing['updated'])).total_seconds()
                if age < 300:
                    continue
            except (TypeError, ValueError):
                continue
        try:
            # Freeze counts with the body: a late receipt must never acknowledge
            # newer outcomes that were absent from the actual message.
            name = 'notice-' + phase + '.json'
            if existing:
                frozen = audit.load_artifact(batch['id'], name)
            else:
                frozen = {'body': report_body(batch, counts, store.stats()), 'summary': summary}
                audit.artifact(batch['id'], name, frozen)
            body, summary = frozen['body'], frozen['summary']
            notifier.send(batch['id'], phase, body)
            store.mark_batch_notified(batch['id'], summary)
            audit.update(batch['id'], status='completed', final_counts=counts)
        except Exception as exc:
            audit.event(batch['id'], 'notification_failed', safe_error(exc))


def do_scan(config, store, audit, stop):
    # The caller holds cover-scan.lock, proving previous scanner processes are
    # gone. Unsealed work never executes; mark it failed so a new scan can
    # reclaim only its provably unstarted queued rows.
    for prior in store.get_batches(1000):
        if prior['status'] == 'scanning':
            store.fail_batch(prior['id'], {'code': 'scanner_interrupted'})
    bid = batch_id()
    audit.begin(bid, 'run', {'policy_version': '20261028-cover-v1'})
    source = CoverSource(config)
    try:
        result = scan(source, store, bid, audit, stop)
        print(json.dumps({'batch_id': bid, 'scan': result}, ensure_ascii=False), flush=True)
    except Exception as exc:
        failure = dict(safe_error(exc), code=getattr(exc, 'code', 'scan_failed'))
        store.fail_batch(bid, failure)
        audit.update(bid, status='failed', error=failure)
        raise
    finally:
        source.close()
        notify_completed(store, audit, config)


def preview(config, stop, limit, reuse_dir=None):
    root = Path(config['runtime_dir']) / 'previews'
    root.mkdir(parents=True, exist_ok=True)
    folder = Path(reuse_dir).resolve() if reuse_dir else Path(tempfile.mkdtemp(prefix='cover-', dir=str(root)))
    if reuse_dir and (root.resolve() not in folder.parents or not (folder/'cover.sqlite3').is_file()):
        raise ValueError('invalid_preview_directory')
    store, audit = CoverStore(folder / 'cover.sqlite3'), Runtime(folder / 'audit')
    bid = batch_id('preview')
    audit.begin(bid, 'preview', {})
    source = CoverSource(config)
    try:
        if reuse_dir:
            batches = store.get_batches(1)
            if not batches or batches[0]['status'] != 'sealed':
                raise ValueError('preview_scan_not_complete')
            summary = dict(batches[0]['meta'], reused_scan_batch=batches[0]['id'])
        else:
            summary = scan(source, store, bid, audit, stop)
        tasks = store.ready_tasks(limit)
        prepared = source.prepare_candidates(tasks)
        tokens = source.credentials()
        governor = Governor(maximum=16, per_account=2, rps=24, initial_limit=16, ramp_up=False)
        graph = CoverGraph(tokens, version=config.get('graph_version', 'v25.0'), governor=governor, stop=stop)
        results = []
        try:
            for task in tasks:
                c = prepared[str(task['id'])]
                if c.get('error'):
                    results.append({'ad_id': c['ad_id'], 'reason': c['error']})
                    continue
                try:
                    ad = graph.get_cover_ad(c['ad_id'], c['user_id'], c['account_id'])
                    creative = graph.get_creative(c['old_creative_id'], c['user_id'], c['account_id'])
                    payload = build_payload(creative, c['cover_url'])
                    results.append({'ad_id': c['ad_id'], 'account_id': c['account_id'],
                                    'old_creative_id': c['old_creative_id'], 'current_creative_id': (ad.get('creative') or {}).get('id'),
                                    'status': ad.get('status'), 'effective_status': ad.get('effective_status'),
                                    'payload_key': payload_key(c['account_id'], payload), 'payload': payload,
                                    'old_creative_snapshot': creative, 'ad_snapshot': ad})
                except Exception as exc:
                    detail = exc.as_dict() if isinstance(exc, GraphError) else (
                        {'code': exc.code} if isinstance(exc, PayloadError) else safe_error(exc))
                    results.append({'ad_id': c['ad_id'], 'error': detail})
        finally:
            graph.close()
        audit.artifact(bid, 'payload-preview.json', {'summary': summary, 'results': results, 'media_writes': 0})
        print(json.dumps({'preview_dir': str(folder), 'batch_id': bid, 'scan': summary,
                          'prepared': len(tasks), 'valid_payloads': sum('payload' in x for x in results), 'media_writes': 0}, ensure_ascii=False))
    finally:
        source.close()
        audit.close()


def work(config, store, audit, stop, once, max_ads):
    from meta_violation_cleanup.cover_worker import CoverWorker
    if config.get('cover_live_enabled') is not True:
        raise RuntimeError('cover_live_disabled')
    shared = Store(config['shared_ledger'])
    store.recover_inflight()
    pause = Path(config['runtime_dir']) / 'cover-write-paused.json'
    if pause.exists():
        raise CoverWritePaused('cover_writes_paused_requires_review')
    governor = Governor(maximum=32, per_account=2, rps=24, initial_limit=32, ramp_up=True)
    next_reconcile = 0
    recovered_reservations = False
    while not stop():
        source = CoverSource(config)
        worker = None
        result = {}
        cycle_started = time.monotonic()
        cycle_started_at = now()
        try:
            tokens = source.credentials()
            with legacy_delete_lock(config['legacy_lock'], stop) as lease:
                def stopped():
                    if stop():
                        return True
                    try:
                        lease.check()
                        return False
                    except InterruptedError:
                        return True
                factory = lambda: CoverGraph(tokens, version=config.get('graph_version', 'v25.0'),
                                             governor=governor, stop=stopped)
                worker = CoverWorker(config, store, source, factory, shared, stop=stopped, governor=governor)
                if not recovered_reservations:
                    recovered = worker.recover_reservations()
                    recovered_reservations = True
                    if recovered:
                        print(json.dumps({'reservation_recovery': recovered}), flush=True)
                if time.monotonic() >= next_reconcile:
                    reconciled = worker.reconcile_unknown(limit=1000)
                    next_reconcile = time.monotonic() + 3600
                    if reconciled:
                        print(json.dumps({'reconciliation': reconciled}), flush=True)
                result = worker.run_once(max_ads=max_ads, process_deletes=True)
                metrics = {'at': now(), 'started_at': cycle_started_at,
                           'cycle_seconds': round(time.monotonic() - cycle_started, 3),
                           'result': {k:v for k,v in result.items() if k not in ('task_results','deletion_results')},
                           'governor': governor.snapshot()}
                print(json.dumps(metrics, ensure_ascii=False), flush=True)
                try:
                    record_cycle(config['runtime_dir'], metrics)
                except OSError as exc:
                    print(json.dumps({'performance_record_error': safe_error(exc)}), flush=True)
                if result.get('halted'):
                    from run import CliError
                    raise CliError(result.get('halt_reason') or 'cover_worker_halted')
        except LegacyPriorityWindow:
            print(json.dumps({'at': now(), 'handoff': 'legacy_priority_window'}), flush=True)
            # Re-enter the lock, wait out the priority window, then reacquire.
            if once:
                break
            continue
        except InterruptedError:
            if not stop():
                raise
            break
        except Exception as exc:
            bid = batch_id('cover-error')
            failure = safe_error(exc)
            audit.begin(bid, 'run', {'policy_version': '20261028-cover-v1'})
            audit.artifact(bid, 'write-pause.json', failure)
            with pause.open('w', encoding='utf-8') as fp:
                json.dump({'batch_id': bid, 'at': now(), 'error': failure}, fp)
                fp.flush()
                os.fsync(fp.fileno())
            directory = os.open(str(pause.parent), os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            store.begin_batch(bid, {})
            store.fail_batch(bid, failure)
            notify_completed(store, audit, config)
            raise CoverWritePaused('cover_writes_paused_requires_review') from exc
        finally:
            if worker:
                worker.close()
            source.close()
        notify_completed(store, audit, config, include_pending=True)
        if once:
            break
        delay = idle_delay(result, bool(store.ready_tasks(limit=1)))
        for _ in range(delay):
            if stop():
                break
            time.sleep(1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    parser.add_argument('command', choices=('scan', 'work', 'preview', 'status', 'notify'))
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--dry-run', action='store_true', help='Preview daily notice without sending')
    parser.add_argument('--max-ads', type=int, default=256)
    parser.add_argument('--preview-dir', help='Reuse only an isolated completed preview scan; refresh all write-time sources/Graph reads')
    args = parser.parse_args()
    if args.dry_run and args.command != 'notify':
        parser.error('--dry-run is only supported for notify')
    config = load_config(args.config)
    validate_paths(config)
    root = Path(config['runtime_dir'])
    event = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: event.set())
    if args.command == 'preview':
        preview(config, event.is_set, min(max(args.max_ads, 1), 256), args.preview_dir)
        return 0
    if args.command == 'notify':
        from meta_violation_cleanup.cover_daily import notify_daily
        audit = Runtime(root / 'cover-audit')
        try:
            print(json.dumps(notify_daily(config, audit, dry_run=args.dry_run), ensure_ascii=False))
        finally:
            audit.close()
        return 0
    store, audit = CoverStore(root / 'cover.sqlite3'), Runtime(root / 'cover-audit')
    try:
        if args.command == 'status':
            print(json.dumps({'counts': store.stats(), 'batches': store.get_batches(5)}, ensure_ascii=False))
        elif args.command == 'scan':
            with process_lock(root / 'cover-scan.lock'):
                do_scan(config, store, audit, event.is_set)
        elif args.command == 'work':
            with process_lock(root / 'process.lock'):
                work(config, store, audit, event.is_set, args.once, max(1, args.max_ads))
    finally:
        audit.close()
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        print(json.dumps({'error': safe_error(exc)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(getattr(exc, 'exit_status', 1))
