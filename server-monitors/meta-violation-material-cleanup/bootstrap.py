#!/usr/bin/env python3
"""One-shot, fail-closed rollout of an already-running complete preview.

This does not implement deletion. It invokes the audited CLI and only enables
the timer after independently checking its durable receipts and results.
An interrupted bootstrap is never automatically replayed.
"""
import argparse
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import sys
import time
import uuid

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from run import load_config, validate_paths, validate_run_id
from meta_violation_cleanup.policy import evaluate_ad
from meta_violation_cleanup.runner import ACTOR
from meta_violation_cleanup.notifications import Notifier
from meta_violation_cleanup.runtime import CST, Runtime, digest, now, process_lock

TIMER = 'meta-violation-material-cleanup.timer'
SERVICE = 'meta-violation-material-cleanup.service'


class RolloutError(RuntimeError):
    pass


def atomic_json(path, value):
    path = Path(path)
    if path.is_symlink():
        raise RolloutError('symlink_not_allowed')
    temporary = path.with_name(path.name + '.tmp-' + uuid.uuid4().hex)
    fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            json.dump(value, output, ensure_ascii=False, sort_keys=True)
            output.write('\n')
            output.flush()
            os.fsync(output.fileno())
        os.replace(str(temporary), str(path))
        fd = os.open(str(path.parent), os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if temporary.exists():
            temporary.unlink()


def read_run(root, run_id):
    with closing(sqlite3.connect('file:' + str(Path(root) / 'runs.sqlite3') + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN')
        row = db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
        if row is None:
            raise RolloutError('preview_run_missing')
        result = dict(row)
        result['data'] = json.loads(result['data'])
        result['notices'] = {
            item['phase']: dict(item) for item in db.execute(
                'SELECT phase,status,receipt FROM notices WHERE run_id=?', (run_id,))
        }
    return result


def read_shared(path, job_id):
    """Read only this job and its objects' global receipts/fences."""
    with closing(sqlite3.connect('file:' + str(path) + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN')
        job = db.execute('SELECT * FROM fb_asset_delete_v2_jobs WHERE job_id=?', (job_id,)).fetchone()
        if job is None:
            raise RolloutError('shared_job_missing')
        result = dict(job)
        result['metadata'] = json.loads(result['metadata'])
        result['objects'] = [dict(row) for row in db.execute(
            'SELECT * FROM fb_asset_delete_v2_objects WHERE job_id=?', (job_id,))]
        result['runs'] = [dict(row) for row in db.execute(
            'SELECT * FROM fb_asset_delete_v2_runs WHERE job_id=?', (job_id,))]
        result['locks'], result['receipts'] = {}, {}
        keys = [row['object_key'] for row in result['objects']]
        for offset in range(0, len(keys), 500):
            part = keys[offset:offset + 500]
            placeholders = ','.join('?' for _ in part)
            for table, target in (('object_locks', 'locks'), ('receipts', 'receipts')):
                result[target].update({row['object_key']: dict(row) for row in db.execute(
                    'SELECT * FROM fb_asset_delete_v2_' + table + ' WHERE object_key IN (' + placeholders + ')', part)})
    return result


def artifact(root, run_id, name):
    directory = Path(root) / 'runs' / validate_run_id(run_id)
    path = directory / name
    if directory.is_symlink() or path.is_symlink():
        raise RolloutError('artifact_symlink')
    return json.loads(path.read_text(encoding='utf-8'))


def verify_preview(run, manifest, current=None):
    """Independent whole-manifest gate before enabling any live invocation."""
    current = current or datetime.now(CST)
    if run['status'] != 'previewed' or run['mode'] != 'preview':
        raise RolloutError('whole_preview_not_complete')
    if manifest.get('run_id') != run['id']:
        raise RolloutError('manifest_run_mismatch')
    plain = {key: value for key, value in manifest.items() if key != 'sha256'}
    if digest(plain) != manifest.get('sha256') or run['data'].get('manifest_sha256') != manifest['sha256']:
        raise RolloutError('manifest_hash_mismatch')
    if set(manifest.get('root_products', [])) != {'1479', '3302'}:
        raise RolloutError('product_scope_mismatch')
    if manifest.get('policy_version') != '20261023-v1':
        raise RolloutError('policy_version_mismatch')
    frozen = datetime.fromisoformat(manifest['frozen_at'])
    if frozen.tzinfo is None or not 0 <= (current - frozen).total_seconds() <= 3600:
        raise RolloutError('manifest_expired')
    start = datetime.fromisoformat(manifest['window_start'])
    end = datetime.fromisoformat(manifest['window_end'])
    if start.tzinfo is None or end.tzinfo is None or (end - start).total_seconds() != 30 * 86400:
        raise RolloutError('window_mismatch')
    if any(manifest[key] != run['data'].get(key) for key in ('window_start', 'window_end')) or end > frozen:
        raise RolloutError('window_mismatch')
    # A daily 11-hour service must have time left for actual deletion. Do not
    # enable a schedule proven unable to complete its required inventory.
    elapsed = (frozen - datetime.fromisoformat(run['started'])).total_seconds()
    if not 0 <= elapsed < 10 * 3600:
        raise RolloutError('scan_exceeds_daily_runtime_budget')
    coverage = manifest['coverage']
    accounts = coverage['accounts']
    account_ids = [str(row['account_id']) for row in accounts]
    if (len(accounts) != coverage['accounts_total'] or len(set(account_ids)) != len(accounts)
            or run['data'].get('accounts_finished') != len(accounts)
            or run['data'].get('accounts_total') != len(accounts)
            or any(row.get('status') not in ('complete', 'failed') for row in accounts)):
        raise RolloutError('account_coverage_incomplete')
    complete = {str(row['account_id']) for row in accounts if row['status'] == 'complete'}
    groups = defaultdict(list)
    ad_ids = set()
    for candidate in manifest['candidates']:
        if any(not re.fullmatch(r'[1-9][0-9]{0,31}', str(candidate[key]))
               for key in ('ad_id', 'creative_id', 'account_id', 'user_id')):
            raise RolloutError('candidate_identity_invalid')
        if candidate['ad_id'] in ad_ids or candidate['account_id'] not in complete:
            raise RolloutError('candidate_coverage_conflict')
        ad_ids.add(candidate['ad_id'])
        decision = evaluate_ad(candidate['ad_snapshot'], candidate['identity'], manifest['evidence'])
        if (not decision['eligible'] or candidate['root_id'] not in ('1479', '3302')
                or str(candidate['ad_snapshot'].get('account_id', '')).removeprefix('act_') != candidate['account_id']
                or any(decision[key] != candidate[key] for key in
                       ('ad_id', 'creative_id', 'root_id', 'rules', 'material_ids'))
                or any(manifest['scope'].get(pid) != candidate['root_id']
                       for pid in candidate['identity']['product_ids'])):
            raise RolloutError('candidate_policy_conflict')
        groups[candidate['creative_id']].append(candidate)
    if any(len({c['account_id'] for c in items}) != 1 for items in groups.values()):
        raise RolloutError('creative_account_conflict')
    if run['data'].get('candidate_ads') != len(ad_ids):
        raise RolloutError('candidate_count_conflict')
    sizes = [len(items) for items in groups.values()]
    canary_size = min(sizes) if sizes else 0
    if canary_size > 3:
        raise RolloutError('no_small_whole_creative_group')
    chosen = next((items for items in groups.values() if len(items) == canary_size), [])
    return {
        'manifest_sha256': manifest['sha256'], 'scan_seconds': elapsed,
        'accounts_complete': len(complete), 'accounts_failed': len(accounts) - len(complete),
        'candidate_ads': len(ad_ids), 'candidate_creatives': len(groups),
        'canary_ads': [item['ad_id'] for item in chosen],
        'canary_creatives': list({item['creative_id'] for item in chosen}),
        'canary_limit': max(1, canary_size),
        'deletion_validation': 'pending' if sizes else 'not_performed_zero_candidates',
        'product_candidates': dict(Counter(item['root_id'] for item in manifest['candidates'])),
        'coverage_errors': len(coverage.get('errors', [])),
        'shared_creatives': len(manifest.get('shared_creative_impacts', {})),
        'shared_canary_impacts': {item['creative_id']: manifest.get('shared_creative_impacts', {}).get(item['creative_id'], [])
                                 for item in chosen},
        'canary_accounts': {item['ad_id']: item['account_id'] for item in chosen},
        'canary_creative_accounts': {item['creative_id']: item['account_id'] for item in chosen},
    }


def verify_notices(run, attempt):
    if run['data'].get('execution_attempt') != attempt:
        raise RolloutError('execution_attempt_mismatch')
    suffix = '' if attempt == 1 else '-' + str(attempt)
    for phase in ('start' + suffix, 'end' + suffix):
        notice = run['notices'].get(phase, {})
        if notice.get('status') != 'sent' or not notice.get('receipt'):
            raise RolloutError('notification_receipt_missing')
        try:
            receipt = json.loads(notice['receipt'])
            def valid_single(value):
                return (isinstance(value, dict) and type(value.get('code')) is int and value['code'] == 0
                    and isinstance(value.get('message_id'), str)
                    and re.fullmatch(r'[A-Za-z0-9_:-]{1,128}', value['message_id']) is not None)
            if not isinstance(receipt, dict) or type(receipt.get('code')) is not int or receipt['code'] != 0:
                raise ValueError()
            if receipt.get('delivery') == 'multipart':
                parts = receipt.get('parts')
                if not isinstance(parts, list) or not parts or type(receipt.get('part_count')) is not int or receipt['part_count'] != len(parts):
                    raise ValueError()
                for index, part in enumerate(parts, 1):
                    expected = phase + '-part-' + str(index).zfill(3)
                    child = run['notices'].get(expected, {})
                    if (part.get('phase') != expected or child.get('status') != 'sent'
                            or not valid_single(part.get('receipt'))
                            or json.loads(child.get('receipt', '{}')) != part['receipt']):
                        raise ValueError()
            elif not valid_single(receipt):
                raise ValueError()
        except (ValueError, TypeError, KeyError, AttributeError):
            raise RolloutError('notification_receipt_invalid') from None


def verify_canary(run, results, plan):
    verify_notices(run, 1)
    if run['status'] not in ('completed', 'partial') or run['data'].get('error_type'):
        raise RolloutError('canary_execution_failed')
    expected = {('ad', value) for value in plan['canary_ads']}
    expected.update(('creative', value) for value in plan['canary_creatives'])
    actual = {(row['kind'], row['id']) for row in results}
    if len(actual) != len(results) or actual != expected:
        raise RolloutError('canary_result_scope_mismatch')
    for row in results:
        result = row['result']
        # For this first deployment require real API acceptance AND exact
        # DELETED readback. Unknown objects retain their shared fence and stop
        # rollout; no automatic trial with another object or token.
        if (result.get('status') != 'deleted_verified' or result.get('api_accepted') is not True
                or result.get('request_sent') is not True
                or result.get('confirmed_deleted') is not True
                or result.get('verification', {}).get('status') != 'deleted_verified'):
            raise RolloutError('canary_not_confirmed_deleted')
        record = result['verification'].get('object', {})
        account = (plan['canary_accounts'] if row['kind'] == 'ad' else plan['canary_creative_accounts'])[row['id']]
        if (record.get('id') != row['id'] or str(record.get('account_id', '')).removeprefix('act_') != account
                or record.get('status') != 'DELETED'):
            raise RolloutError('canary_deleted_identity_mismatch')
    if run['data'].get('result_counts', {}).get('skipped_ads', 0):
        raise RolloutError('canary_qualification_changed')


def verify_continuation(run):
    verify_notices(run, 2)
    data = run['data']
    if (run['status'] not in ('completed', 'partial') or data.get('error_type')
            or data.get('execution_status') not in ('completed', 'partial')
            or data.get('canary_deferred_ads') != 0):
        raise RolloutError('continuation_not_finished')


def verify_shared(run, manifest, shared, full_attempt, required_success=()):
    expected = {'ad:' + c['ad_id'] for c in manifest['candidates']}
    expected.update('creative:' + c['creative_id'] for c in manifest['candidates'])
    job_id = 'policy-cleanup-' + run['id']
    if (run['data'].get('shared_job_id') != job_id or shared.get('job_id') != job_id
            or shared.get('actor') != ACTOR or shared.get('preview_id') != manifest['sha256']
            or shared.get('metadata', {}).get('manifest_sha256') != manifest['sha256']):
        raise RolloutError('shared_job_scope_mismatch')
    objects = shared['objects']
    if len(objects) != len(expected) or {o['object_key'] for o in objects} != expected:
        raise RolloutError('shared_object_scope_mismatch')
    runs = shared['runs']
    if (not any(r['run_id'] == run['data'].get('shared_run_id') for r in runs)
            or any(r['status'] == 'running' or r['actor'] != ACTOR or r['preview_id'] != manifest['sha256'] for r in runs)):
        raise RolloutError('shared_execution_not_final')
    counts = Counter()
    for obj in objects:
        key, status = obj['object_key'], obj['status']
        if key in required_success and status not in ('deleted', 'already_deleted'):
            raise RolloutError('canary_receipt_not_preserved')
        if key != obj['kind'] + ':' + obj['object_id'] or status not in ('pending', 'failed', 'unknown', 'deleted', 'already_deleted'):
            raise RolloutError('shared_object_not_final')
        lock = shared['locks'].get(key)
        if lock and lock['status'] == 'in_progress':
            raise RolloutError('shared_object_in_progress')
        if status == 'unknown' and (not lock or lock['status'] != 'unknown'):
            raise RolloutError('unknown_fence_missing')
        if status in ('deleted', 'already_deleted'):
            receipt = shared['receipts'].get(key, {})
            if receipt.get('status') not in ('deleted', 'already_deleted') or not json.loads(receipt.get('result') or '{}'):
                raise RolloutError('success_receipt_missing')
        counts[obj['kind'] + '_' + status] += 1
    if full_attempt and counts['ad_pending'] > run['data'].get('result_counts', {}).get('skipped_ads', 0):
        raise RolloutError('pending_ads_not_accounted_for')
    return dict(counts)


class Bootstrap:
    def __init__(self, config_path, run_id, release_sha):
        self.config_path = Path(config_path)
        self.config = load_config(config_path)
        validate_paths(self.config)
        self.root = Path(self.config['runtime_dir'])
        self.run_id = validate_run_id(run_id)
        self.state_path = self.root / ('bootstrap-' + self.run_id + '.json')
        self.release_sha = release_sha
        self.state = {'run_id': self.run_id, 'release_sha': release_sha, 'started': now()}
        self.child = None
        self.stop = False
        self.live_changed = False
        self.timer_changed = False

    def save(self, stage, **fields):
        self.state.update(stage=stage, updated=now(), **fields)
        atomic_json(self.state_path, self.state)

    def systemctl(self, *args):
        return subprocess.run(['systemctl', *args], capture_output=True, text=True, timeout=30)

    def check(self):
        if self.stop:
            raise RolloutError('stop_requested')
        if (self.root / 'current').resolve() != HERE.parents[1]:
            raise RolloutError('release_changed')
        result = subprocess.run(['git', '-C', str(HERE.parents[1]), 'rev-parse', 'HEAD'],
                                capture_output=True, text=True, timeout=10)
        if result.returncode or result.stdout.strip() != self.release_sha:
            raise RolloutError('release_sha_mismatch')

    def set_live(self, enabled):
        current = load_config(self.config_path)
        if {k: v for k, v in current.items() if k != 'live_enabled'} != {
                k: v for k, v in self.config.items() if k != 'live_enabled'}:
            raise RolloutError('configuration_changed')
        current['live_enabled'] = enabled
        atomic_json(self.config_path, current)

    def verify_units(self):
        for unit in (SERVICE, TIMER):
            if (Path('/etc/systemd/system') / unit).read_bytes() != (HERE / 'deploy' / unit).read_bytes():
                raise RolloutError('unit_content_mismatch')
        # Read loaded units too: matching disk files cannot prove daemon-reload
        # has loaded them, or that an external drop-in is not overriding them.
        result = self.systemctl('show', SERVICE, TIMER, '-p', 'FragmentPath', '-p', 'DropInPaths', '-p', 'NeedDaemonReload')
        lines = result.stdout.splitlines()
        fragments = {line.split('=', 1)[1] for line in lines if line.startswith('FragmentPath=')}
        if (result.returncode or fragments != {'/etc/systemd/system/' + unit for unit in (SERVICE, TIMER)}
                or lines.count('NeedDaemonReload=no') != 2 or lines.count('DropInPaths=') != 2):
            raise RolloutError('unit_loaded_state_mismatch')

    def notify_failure(self, code):
        notice_id = self.run_id + '-bootstrap'
        if len(notice_id) > 100:
            notice_id = self.run_id[:80] + '-bootstrap-' + digest(self.run_id)[:8]
        self.state['failure_notification_run_id'] = notice_id
        body = ('Meta违规素材清理｜上线验收未通过\n原运行：%s\n'
                '正式定时任务未启用，或已请求停止；自动上线已停止。\n'
                '原因：%s\n证据：%s\n请核对保存的验收状态及清理账本；不会自动重放删除。') % (
                    self.run_id, code, self.state_path)
        runtime = Runtime(self.root)
        try:
            runtime.begin(notice_id, 'preview', {'bootstrap_run_id': self.run_id, 'stage': 'bootstrap_failed'})
            runtime.update(notice_id, status='failed', bootstrap_reason=code)
            saved = runtime.notice(notice_id, 'end')
            receipt = Notifier(runtime, self.config['feishu_config']).send(
                notice_id, 'end', saved['body'] if saved else body)
            self.state['failure_notification_status'] = 'sent'
            return receipt
        finally:
            notice = runtime.notice(notice_id, 'end')
            if notice:
                self.state['failure_notification_status'] = notice['status']
            runtime.close()

    def invoke(self, limit=None):
        self.check()
        command = [sys.executable, str(HERE / 'run.py'), '--config', str(self.config_path),
                   'run', '--run-id', self.run_id]
        if limit is not None:
            command.extend(['--max-ads', str(limit)])
        self.child = subprocess.Popen(command, close_fds=True)
        try:
            code = self.child.wait()
        finally:
            self.child = None
        self.check()
        if code != 0:
            raise RolloutError('live_cli_failed')

    def interrupt(self, *_):
        self.stop = True
        if self.child is not None and self.child.poll() is None:
            self.child.send_signal(signal.SIGINT)

    def execute(self):
        with process_lock(self.root / 'bootstrap.lock'):
            if self.state_path.exists():
                raise RolloutError('bootstrap_already_started_review_saved_state')
            self.save('waiting_for_preview')
            try:
                self.check()
                if self.config.get('live_enabled') is not False:
                    raise RolloutError('bootstrap_requires_live_disabled')
                if self.systemctl('is-enabled', TIMER).stdout.strip() != 'disabled':
                    raise RolloutError('bootstrap_requires_timer_disabled')
                if self.systemctl('is-active', TIMER).stdout.strip() != 'inactive':
                    raise RolloutError('bootstrap_requires_timer_inactive')
                while True:
                    self.check()
                    run = read_run(self.root, self.run_id)
                    if run['status'] != 'running':
                        break
                    if run['mode'] != 'preview':
                        raise RolloutError('not_a_preview')
                    started = datetime.fromisoformat(run['started'])
                    if started.tzinfo is None or (datetime.now(CST) - started).total_seconds() >= 10 * 3600:
                        raise RolloutError('scan_exceeds_daily_runtime_budget')
                    time.sleep(30)
                manifest = artifact(self.root, self.run_id, 'manifest.json')
                plan = verify_preview(run, manifest)
                atomic_json(self.root / ('rehearsal-acceptance-' + self.run_id + '.json'), plan)
                self.save('preview_verified', acceptance=plan)
                atomic_json(self.root / ('config-before-bootstrap-' + self.run_id + '.json'), self.config)
                self.live_changed = True
                self.set_live(True)
                self.save('canary_running')
                self.invoke(plan['canary_limit'])
                run = read_run(self.root, self.run_id)
                results = artifact(self.root, self.run_id, 'execution-results.json')
                verify_canary(run, results, plan)
                canary_keys = {'ad:' + value for value in plan['canary_ads']}
                canary_keys.update('creative:' + value for value in plan['canary_creatives'])
                shared = verify_shared(run, manifest, read_shared(self.config['shared_ledger'], run['data'].get('shared_job_id')),
                                       full_attempt=plan['candidate_ads'] == len(plan['canary_ads']), required_success=canary_keys)
                self.save('canary_verified', canary_results=results, shared_ledger_status=shared,
                          deletion_validation='confirmed' if results else 'not_performed_zero_candidates')
                if plan['candidate_ads'] > len(plan['canary_ads']):
                    self.save('continuation_running')
                    self.invoke()
                    run = read_run(self.root, self.run_id)
                    verify_continuation(run)
                    shared = verify_shared(run, manifest, read_shared(self.config['shared_ledger'], run['data'].get('shared_job_id')),
                                           full_attempt=True, required_success=canary_keys)
                    self.save('continuation_verified', shared_ledger_status=shared)
                self.check()
                self.verify_units()
                self.save('enabling_timer')
                self.timer_changed = True
                result = self.systemctl('enable', '--now', TIMER)
                if result.returncode:
                    raise RolloutError('timer_enable_failed')
                state = self.systemctl('show', TIMER, '-p', 'ActiveState', '-p', 'UnitFileState', '-p', 'NextElapseUSecRealtime')
                fields = dict(line.split('=', 1) for line in state.stdout.splitlines() if '=' in line)
                if state.returncode or fields.get('ActiveState') != 'active' or fields.get('UnitFileState') != 'enabled' or fields.get('NextElapseUSecRealtime', '').strip() in ('', 'n/a', '0'):
                    raise RolloutError('timer_readback_failed')
                self.save('completed', timer=fields, ended=now())
            except BaseException as exc:
                # Never undo object results, receipts, or unknown fences. Keep
                # the timer off on every incomplete/uncertain deployment.
                cleanup_errors = {}
                def cleanup(name, action):
                    try:
                        result = action()
                        if getattr(result, 'returncode', 0):
                            cleanup_errors[name] = 'CommandFailed'
                    except BaseException as error:
                        cleanup_errors[name] = type(error).__name__
                if self.timer_changed:
                    cleanup('disable_timer', lambda: self.systemctl('disable', '--now', TIMER))
                    cleanup('stop_service', lambda: self.systemctl('stop', '--no-block', SERVICE))
                if self.live_changed:
                    cleanup('disable_live', lambda: self.set_live(False))
                code = str(exc) if isinstance(exc, RolloutError) else type(exc).__name__
                if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,99}', code):
                    code = 'bootstrap_validation_failed'
                self.state.update(stage='stopped_for_review', reason=code, cleanup_errors=cleanup_errors, ended=now())
                cleanup('failure_notification', lambda: self.notify_failure(code))
                cleanup('save_state', lambda: self.save('stopped_for_review'))
                raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--release-sha', required=True)
    args = parser.parse_args()
    worker = Bootstrap(args.config, args.run_id, args.release_sha)
    signal.signal(signal.SIGINT, worker.interrupt)
    signal.signal(signal.SIGTERM, worker.interrupt)
    try:
        worker.execute()
    except Exception as exc:
        print(json.dumps({'bootstrap': 'stopped_for_review', 'error_type': type(exc).__name__}))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
