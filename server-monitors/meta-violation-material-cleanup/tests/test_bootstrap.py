"""One-shot rollout gates; all systemd, child processes and messages are fake."""
import copy
from contextlib import nullcontext
from datetime import datetime, timedelta
import importlib.util
import json
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
spec = importlib.util.spec_from_file_location('cleanup_bootstrap', HERE / 'bootstrap.py')
boot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(boot)


def fixture(count=2):
    current = datetime.now(boot.CST)
    end = current - timedelta(minutes=5)
    manifest = {'run_id': 'preview-test', 'policy_version': '20261023-v1',
        'root_products': ['1479', '3302'], 'scope': {'1479': '1479', '3302': '3302'},
        'frozen_at': (current - timedelta(seconds=1)).isoformat(),
        'window_start': (end - timedelta(days=30)).isoformat(), 'window_end': end.isoformat(),
        'coverage': {'accounts_total': 1, 'accounts': [{'account_id': '100', 'status': 'complete'}], 'errors': []},
        'evidence': {'materials': {'501': {'count': 5, 'violating_ad_ids': ['101', '102', '103', '104', '105']}}},
        'candidates': [], 'shared_creative_impacts': {}}
    for i in range(count):
        aid, cid = str(10 + i), str(20 + i)
        ad = {'id': aid, 'account_id': '100', 'creative': {'id': cid}, 'status': 'PAUSED',
            'effective_status': 'PAUSED', 'adset': {'status': 'ACTIVE'}, 'campaign': {'status': 'ACTIVE'},
            'issues_info': [], 'ad_review_feedback': {}}
        identity = {'reason': '', 'ad_id': aid, 'creative_id': cid, 'root_id': '1479',
                    'product_ids': ['1479'], 'material_ids': ['501']}
        decision = boot.evaluate_ad(ad, identity, manifest['evidence'])
        manifest['candidates'].append(dict(decision, account_id='100', user_id='803', identity=identity, ad_snapshot=ad))
    run = {'id': 'preview-test', 'mode': 'preview', 'status': 'previewed', 'started': end.isoformat(),
        'data': {'accounts_total': 1, 'accounts_finished': 1, 'candidate_ads': count,
                 'window_start': manifest['window_start'], 'window_end': manifest['window_end']}, 'notices': {}}
    rehash(run, manifest)
    return run, manifest


def rehash(run, manifest):
    manifest['sha256'] = boot.digest({k: v for k, v in manifest.items() if k != 'sha256'})
    run['data']['manifest_sha256'] = manifest['sha256']


def notice(mid='om_test'):
    return {'status': 'sent', 'receipt': json.dumps({'code': 0, 'message_id': mid, 'uuid': 'test'})}


def outcome(kind, identifier, account='100'):
    return {'kind': kind, 'id': identifier, 'result': {'status': 'deleted_verified', 'request_sent': True,
        'api_accepted': True, 'confirmed_deleted': True, 'verification': {'status': 'deleted_verified',
            'object': {'id': identifier, 'account_id': account, 'status': 'DELETED'}}}}


def executed(run, attempt, status='partial', deferred=0):
    run = copy.deepcopy(run)
    suffix = '' if attempt == 1 else '-' + str(attempt)
    run['status'] = status
    run['notices'] = {'start' + suffix: notice(), 'end' + suffix: notice()}
    run['data'].update(execution_attempt=attempt, execution_status=status, canary_deferred_ads=deferred,
        result_counts={'skipped_ads': 0}, shared_job_id='policy-cleanup-' + run['id'], shared_run_id='shared-' + str(attempt))
    return run


def shared_fixture(run, manifest, statuses=None):
    statuses = statuses or {}
    keys = {'ad:' + c['ad_id'] for c in manifest['candidates']}
    keys.update('creative:' + c['creative_id'] for c in manifest['candidates'])
    shared = {'job_id': run['data']['shared_job_id'], 'actor': boot.ACTOR, 'preview_id': manifest['sha256'],
        'metadata': {'manifest_sha256': manifest['sha256']}, 'objects': [], 'locks': {}, 'receipts': {},
        'runs': [{'run_id': run['data']['shared_run_id'], 'actor': boot.ACTOR, 'preview_id': manifest['sha256'], 'status': run['status']}]}
    for key in sorted(keys):
        kind, identifier = key.split(':')
        status = statuses.get(key, 'deleted')
        shared['objects'].append({'object_key': key, 'kind': kind, 'object_id': identifier, 'status': status})
        if status in ('deleted', 'already_deleted'):
            shared['receipts'][key] = {'object_key': key, 'status': status, 'result': '{"confirmed_deleted":true}'}
        if status == 'unknown':
            shared['locks'][key] = {'object_key': key, 'status': 'unknown'}
    return shared


class GateTests(unittest.TestCase):
    def test_real_notice_table_is_read_without_mutating_database(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'runs.sqlite3'
            with sqlite3.connect(path) as db:
                db.executescript('CREATE TABLE runs(id TEXT,status TEXT,mode TEXT,data TEXT); '
                                 'CREATE TABLE notices(run_id TEXT,phase TEXT,status TEXT,receipt TEXT);')
                db.execute('INSERT INTO runs VALUES(?,?,?,?)', ('r', 'previewed', 'preview', '{}'))
                db.execute('INSERT INTO notices VALUES(?,?,?,?)', ('r', 'end', 'sent', notice()['receipt']))
            db.close()
            before = path.read_bytes()
            self.assertEqual(boot.read_run(root, 'r')['notices']['end']['status'], 'sent')
            self.assertEqual(path.read_bytes(), before)

    def test_preview_requires_hash_coverage_scope_and_current_rules(self):
        mutations = (
            lambda r, m: r.update(status='partial'),
            lambda r, m: m.update(root_products=['1479', '9999']),
            lambda r, m: m.update(policy_version='different'),
            lambda r, m: r['data'].update(accounts_finished=0),
            lambda r, m: r['data'].update(accounts_total=0),
            lambda r, m: m['candidates'][0].update(account_id='101'),
            lambda r, m: m['candidates'][0]['ad_snapshot'].update(account_id='999'),
            lambda r, m: m['candidates'][0].update(user_id='a-token'),
            lambda r, m: m['candidates'][0]['ad_snapshot'].update(effective_status='DISAPPROVED'),
            lambda r, m: m['candidates'].append(copy.deepcopy(m['candidates'][0])),
            lambda r, m: m['scope'].update({'1479': '3302'}),
        )
        for index, change in enumerate(mutations):
            with self.subTest(index=index):
                run, manifest = fixture(); change(run, manifest); rehash(run, manifest)
                with self.assertRaises(boot.RolloutError): boot.verify_preview(run, manifest)
        run, manifest = fixture(); manifest['sha256'] = 'wrong'
        with self.assertRaisesRegex(boot.RolloutError, 'manifest_hash'): boot.verify_preview(run, manifest)

    def test_preview_age_and_ten_hour_budget_are_fail_closed(self):
        run, manifest = fixture()
        run['started'] = (datetime.fromisoformat(manifest['frozen_at']) - timedelta(hours=10)).isoformat()
        with self.assertRaisesRegex(boot.RolloutError, 'runtime_budget'): boot.verify_preview(run, manifest)
        run, manifest = fixture()
        manifest['frozen_at'] = (datetime.now(boot.CST) - timedelta(hours=2)).isoformat(); rehash(run, manifest)
        with self.assertRaisesRegex(boot.RolloutError, 'manifest_expired'): boot.verify_preview(run, manifest)

    def test_whole_group_is_selected_and_large_only_group_is_rejected(self):
        run, manifest = fixture(4)
        for c in manifest['candidates']:
            c['creative_id'] = c['identity']['creative_id'] = c['ad_snapshot']['creative']['id'] = '20'
        rehash(run, manifest)
        with self.assertRaisesRegex(boot.RolloutError, 'no_small_whole'): boot.verify_preview(run, manifest)
        run, manifest = fixture(2)
        for c in manifest['candidates']:
            c['creative_id'] = c['identity']['creative_id'] = c['ad_snapshot']['creative']['id'] = '20'
        rehash(run, manifest)
        plan = boot.verify_preview(run, manifest)
        self.assertEqual(plan['canary_limit'], 2)
        self.assertEqual(plan['canary_ads'], ['10', '11'])

    def test_zero_and_partial_coverage_are_explicit_not_silently_rejected(self):
        run, manifest = fixture(0)
        manifest['coverage']['accounts'][0]['status'] = 'failed'; rehash(run, manifest)
        plan = boot.verify_preview(run, manifest)
        self.assertEqual(plan['canary_ads'], [])
        self.assertEqual(plan['deletion_validation'], 'not_performed_zero_candidates')
        boot.verify_canary(executed(run, 1), [], plan)

    def test_canary_requires_exact_deleted_identity_real_request_and_scope(self):
        run, manifest = fixture(); plan = boot.verify_preview(run, manifest); run = executed(run, 1)
        good = [outcome('creative', '20'), outcome('ad', '10')]
        boot.verify_canary(run, good, plan)
        changes = (
            lambda rows: rows[0]['result'].update(status='unknown'),
            lambda rows: rows[0]['result'].update(api_accepted=False),
            lambda rows: rows[0]['result'].update(request_sent=False),
            lambda rows: rows[1]['result']['verification']['object'].update(id='999'),
            lambda rows: rows[1]['result']['verification']['object'].update(account_id='999'),
            lambda rows: rows[1]['result']['verification']['object'].update(status='ACTIVE'),
            lambda rows: rows.append(outcome('ad', '11')),
        )
        for index, change in enumerate(changes):
            with self.subTest(index=index):
                rows = copy.deepcopy(good); change(rows)
                with self.assertRaises(boot.RolloutError): boot.verify_canary(run, rows, plan)

    def test_notifications_require_actual_receipts_and_multipart_children(self):
        run, _ = fixture(); run = executed(run, 1)
        for receipt in ('{}', 'invalid', '{"code":true,"message_id":"om_x"}', '{"code":0}', '{"code":9,"message_id":"om_x"}'):
            with self.subTest(receipt=receipt):
                run['notices']['end'] = {'status': 'sent', 'receipt': receipt}
                with self.assertRaises(boot.RolloutError): boot.verify_notices(run, 1)
        child = notice(); saved = json.loads(child['receipt'])
        parent = {'code': 0, 'delivery': 'multipart', 'part_count': 1,
                  'parts': [{'phase': 'end-part-001', 'receipt': saved}]}
        run['notices'].update({'end': {'status': 'sent', 'receipt': json.dumps(parent)}, 'end-part-001': child})
        boot.verify_notices(run, 1)
        run['notices']['end-part-001']['status'] = 'unknown'
        with self.assertRaises(boot.RolloutError): boot.verify_notices(run, 1)

    def test_shared_partial_preserves_unknown_fences_and_success_receipts(self):
        run, manifest = fixture(); run = executed(run, 2)
        shared = shared_fixture(run, manifest, {'ad:11': 'unknown', 'creative:21': 'failed'})
        self.assertEqual(boot.verify_shared(run, manifest, shared, True)['ad_unknown'], 1)
        for change in (
            lambda s: s['locks'].clear(),
            lambda s: s['receipts'].pop('ad:10'),
            lambda s: s['objects'][0].update(status='in_progress'),
            lambda s: s.update(actor='unrelated-actor'),
            lambda s: s['objects'].append({'object_key': 'video:999'}),
            lambda s: s['runs'][0].update(status='running'),
        ):
            broken = copy.deepcopy(shared); change(broken)
            with self.assertRaises(boot.RolloutError): boot.verify_shared(run, manifest, broken, True)

    def test_full_attempt_allows_only_accounted_pending_qualification_skips(self):
        run, manifest = fixture(); run = executed(run, 2)
        shared = shared_fixture(run, manifest, {'ad:11': 'pending', 'creative:21': 'pending'})
        with self.assertRaisesRegex(boot.RolloutError, 'pending_ads'): boot.verify_shared(run, manifest, shared, True)
        run['data']['result_counts']['skipped_ads'] = 1
        boot.verify_shared(run, manifest, shared, True)
        run['data']['canary_deferred_ads'] = 1
        with self.assertRaises(boot.RolloutError): boot.verify_continuation(run)

    def test_shared_reader_scopes_queries_and_preserves_database(self):
        run, manifest = fixture(1); run = executed(run, 1)
        expected = shared_fixture(run, manifest, {'ad:10': 'unknown'})
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'shared.sqlite3'
            db = sqlite3.connect(path)
            with db:
                db.executescript('''
                    CREATE TABLE fb_asset_delete_v2_jobs(job_id TEXT,actor TEXT,preview_id TEXT,metadata TEXT);
                    CREATE TABLE fb_asset_delete_v2_objects(job_id TEXT,object_key TEXT,kind TEXT,object_id TEXT,status TEXT);
                    CREATE TABLE fb_asset_delete_v2_runs(job_id TEXT,run_id TEXT,actor TEXT,preview_id TEXT,status TEXT);
                    CREATE TABLE fb_asset_delete_v2_object_locks(object_key TEXT,status TEXT);
                    CREATE TABLE fb_asset_delete_v2_receipts(object_key TEXT,status TEXT,result TEXT);
                ''')
                db.execute('INSERT INTO fb_asset_delete_v2_jobs VALUES(?,?,?,?)',
                    (expected['job_id'], expected['actor'], expected['preview_id'], json.dumps(expected['metadata'])))
                for obj in expected['objects']:
                    db.execute('INSERT INTO fb_asset_delete_v2_objects VALUES(?,?,?,?,?)',
                        (expected['job_id'], obj['object_key'], obj['kind'], obj['object_id'], obj['status']))
                for row in expected['runs']:
                    db.execute('INSERT INTO fb_asset_delete_v2_runs VALUES(?,?,?,?,?)',
                        (expected['job_id'], row['run_id'], row['actor'], row['preview_id'], row['status']))
                db.execute('INSERT INTO fb_asset_delete_v2_object_locks VALUES(?,?)', ('ad:10', 'unknown'))
                db.execute('INSERT INTO fb_asset_delete_v2_object_locks VALUES(?,?)', ('ad:999', 'in_progress'))
                db.execute('INSERT INTO fb_asset_delete_v2_receipts VALUES(?,?,?)', ('creative:20', 'deleted', '{"confirmed_deleted":true}'))
            db.close(); before = path.read_bytes()
            shared = boot.read_shared(path, expected['job_id'])
            self.assertEqual(set(shared['locks']), {'ad:10'})
            boot.verify_shared(run, manifest, shared, True)
            self.assertEqual(path.read_bytes(), before)

    def test_canary_success_cannot_disappear_into_later_unknown_fence(self):
        run, manifest = fixture(); run = executed(run, 2)
        shared = shared_fixture(run, manifest, {'ad:10': 'unknown'})
        with self.assertRaisesRegex(boot.RolloutError, 'canary_receipt_not_preserved'):
            boot.verify_shared(run, manifest, shared, True, required_success={'ad:10'})


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {'runtime_dir': str(self.root), 'shared_ledger': str(self.root / 'shared.sqlite3'),
            'feishu_config': str(self.root / 'feishu.json'), 'live_enabled': False}
        self.log = []; self.children = []; self.current_config = dict(self.config)
        self.run, self.manifest = fixture()
        self.canary = executed(self.run, 1, deferred=1)
        self.full = executed(self.run, 2)
        self.results = [outcome('creative', '20'), outcome('ad', '10')]
        self.reads = [self.run, self.canary, self.full]
        self.shared_reads = [shared_fixture(self.canary, self.manifest, {'ad:11': 'pending', 'creative:21': 'pending'}),
                             shared_fixture(self.full, self.manifest)]
        self.returncodes = [0, 0]
        self.show_fail = False; self.cleanup_timeout = False
        self.patch('load_config', lambda path: copy.deepcopy(self.current_config))
        self.patch('validate_paths', lambda config: None)
        self.patch('process_lock', lambda path: nullcontext())
        self.patch('atomic_json', self.atomic)
        self.patch('read_run', lambda root, rid: copy.deepcopy(self.reads.pop(0)))
        self.patch('read_shared', lambda path, rid: copy.deepcopy(self.shared_reads.pop(0)))
        self.patch('artifact', lambda root, rid, name: copy.deepcopy(self.manifest if name == 'manifest.json' else self.results))
        self.patch('subprocess.run', self.subprocess_run)
        self.patch('subprocess.Popen', self.popen)
        self.worker = boot.Bootstrap(str(self.root / 'config.json'), self.run['id'], 'a' * 40)
        self.worker.check = self.check
        self.worker.verify_units = lambda: self.log.append(('units_verified',))
        self.worker.notify_failure = lambda code: self.log.append(('failure_notice', code))

    def patch(self, target, replacement):
        item = patch.object(boot, target, replacement) if '.' not in target else patch('cleanup_bootstrap.' + target, replacement)
        # importlib module is intentionally not in sys.modules; nested patches
        # use the actual subprocess object rather than a module-name string.
        if '.' in target:
            owner, name = target.split('.'); item = patch.object(getattr(boot, owner), name, replacement)
        item.start(); self.addCleanup(item.stop)

    def atomic(self, path, value):
        path = Path(path)
        if path.name == 'config.json':
            self.current_config = copy.deepcopy(value); self.log.append(('live', value['live_enabled']))
        elif path.name.startswith('bootstrap-'):
            self.log.append(('stage', value['stage']))
        path.write_text(json.dumps(value), encoding='utf-8')

    def check(self):
        if self.worker.stop: raise boot.RolloutError('stop_requested')

    def subprocess_run(self, command, **kwargs):
        self.log.append(tuple(command))
        action = command[1]
        if action == 'is-enabled': output = 'disabled\n'
        elif action == 'is-active': output = 'inactive\n'
        elif action == 'show': output = 'ActiveState=active\nUnitFileState=enabled\nNextElapseUSecRealtime=' + ('n/a' if self.show_fail else 'Fri 2026-10-23 21:00:00 CST')
        else: output = ''
        if action == 'disable' and self.cleanup_timeout: raise subprocess.TimeoutExpired(command, 30)
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr='')

    def popen(self, command, **kwargs):
        self.log.append(('cli', 'canary' if '--max-ads' in command else 'full'))
        parent = self
        class Child:
            signals = None
            def __init__(self): self.signals = []
            def wait(self): return parent.returncodes.pop(0)
            def poll(self): return None
            def send_signal(self, sig): self.signals.append(sig)
        child = Child(); self.children.append(child); return child

    def test_stage_order_canary_full_then_timer(self):
        self.worker.execute()
        significant = [x for x in self.log if x[0] in ('live', 'cli', 'units_verified') or x[:2] == ('systemctl', 'enable')]
        self.assertEqual(significant, [('live', True), ('cli', 'canary'), ('cli', 'full'), ('units_verified',),
                                     ('systemctl', 'enable', '--now', boot.TIMER)])
        self.assertEqual(self.worker.state['stage'], 'completed')

    def test_zero_result_uses_one_real_cli_notice_cycle_and_records_no_delete_validation(self):
        self.run, self.manifest = fixture(0); self.canary = executed(self.run, 1, 'completed')
        self.reads[:] = [self.run, self.canary]; self.results[:] = []
        self.shared_reads[:] = [shared_fixture(self.canary, self.manifest)]
        self.worker.execute()
        self.assertEqual([x for x in self.log if x[0] == 'cli'], [('cli', 'canary')])
        self.assertEqual(self.worker.state['deletion_validation'], 'not_performed_zero_candidates')

    def test_unknown_canary_stops_without_second_trial_or_timer(self):
        self.results[0]['result']['status'] = 'unknown'
        with self.assertRaises(boot.RolloutError): self.worker.execute()
        self.assertEqual([x for x in self.log if x[0] == 'cli'], [('cli', 'canary')])
        self.assertFalse(self.current_config['live_enabled'])
        self.assertNotIn(('systemctl', 'enable', '--now', boot.TIMER), self.log)
        self.assertEqual(self.worker.state['stage'], 'stopped_for_review')

    def test_full_partial_with_unknown_is_enabled_only_when_fence_is_durable(self):
        self.shared_reads[1] = shared_fixture(self.full, self.manifest, {'ad:11': 'unknown', 'creative:21': 'failed'})
        self.worker.execute()
        self.assertEqual(self.worker.state['shared_ledger_status']['ad_unknown'], 1)
        self.assertEqual(self.worker.state['stage'], 'completed')

    def test_full_unknown_without_fence_cannot_enable(self):
        self.shared_reads[1] = shared_fixture(self.full, self.manifest, {'ad:11': 'unknown'})
        self.shared_reads[1]['locks'].clear()
        with self.assertRaisesRegex(boot.RolloutError, 'unknown_fence'): self.worker.execute()
        self.assertFalse(self.current_config['live_enabled'])
        self.assertNotIn(('systemctl', 'enable', '--now', boot.TIMER), self.log)

    def test_missing_notice_and_child_failure_never_enable(self):
        self.canary['notices']['end']['status'] = 'unknown'; self.reads[1] = self.canary
        with self.assertRaises(boot.RolloutError): self.worker.execute()
        self.assertFalse(self.current_config['live_enabled'])
        self.assertEqual(len(self.children), 1)

    def test_nonzero_child_exit_turns_live_off(self):
        self.returncodes[0] = 1
        with self.assertRaisesRegex(boot.RolloutError, 'live_cli_failed'): self.worker.execute()
        self.assertFalse(self.current_config['live_enabled'])

    def test_preview_failure_and_over_budget_do_not_enable_live(self):
        self.run['status'] = 'failed'
        with self.assertRaises(boot.RolloutError): self.worker.execute()
        self.assertFalse(any(x[0] in ('live', 'cli') for x in self.log))
        self.assertTrue(any(x[0] == 'failure_notice' for x in self.log))

    def test_running_preview_exceeding_budget_stops_without_killing_preview(self):
        self.run.update(status='running', started=(datetime.now(boot.CST) - timedelta(hours=10)).isoformat())
        with self.assertRaisesRegex(boot.RolloutError, 'runtime_budget'): self.worker.execute()
        self.assertFalse(self.children)
        self.assertFalse(self.current_config['live_enabled'])

    def test_stop_forwards_sigint_and_does_not_start_next_stage(self):
        original = self.popen
        def stopping(command, **kwargs):
            child = original(command, **kwargs)
            def wait(): self.worker.interrupt(); return 130
            child.wait = wait
            return child
        with patch.object(boot.subprocess, 'Popen', stopping):
            with self.assertRaisesRegex(boot.RolloutError, 'stop_requested'): self.worker.execute()
        self.assertEqual(self.children[0].signals, [signal.SIGINT])
        self.assertFalse(self.current_config['live_enabled'])
        self.assertEqual(len(self.children), 1)

    def test_existing_bootstrap_is_never_replayed(self):
        self.worker.state_path.write_text('{"stage":"canary_running"}', encoding='utf-8')
        with self.assertRaisesRegex(boot.RolloutError, 'already_started'): self.worker.execute()
        self.assertEqual(self.log, [])

    def test_cleanup_timeout_does_not_skip_live_disable_and_saved_failure(self):
        self.show_fail = True; self.cleanup_timeout = True
        with self.assertRaisesRegex(boot.RolloutError, 'timer_readback'): self.worker.execute()
        self.assertFalse(self.current_config['live_enabled'])
        self.assertEqual(self.worker.state['cleanup_errors']['disable_timer'], 'TimeoutExpired')
        self.assertIn(('systemctl', 'stop', '--no-block', boot.SERVICE), self.log)
        saved = json.loads(self.worker.state_path.read_text())
        self.assertEqual(saved['stage'], 'stopped_for_review')

    def test_atomic_live_write_then_error_is_rolled_back(self):
        original = self.worker.set_live
        def set_live(enabled):
            original(enabled)
            if enabled: raise OSError('synthetic-fsync-failure')
        self.worker.set_live = set_live
        with self.assertRaises(OSError): self.worker.execute()
        self.assertFalse(self.current_config['live_enabled'])
        self.assertFalse(self.children)

    def test_configuration_failure_during_cleanup_is_recorded_before_state_save(self):
        self.show_fail = True
        original = self.worker.set_live
        def set_live(enabled):
            if not enabled: raise boot.RolloutError('configuration_changed')
            original(enabled)
        self.worker.set_live = set_live
        with self.assertRaisesRegex(boot.RolloutError, 'timer_readback'): self.worker.execute()
        saved = json.loads(self.worker.state_path.read_text())
        self.assertEqual(saved['cleanup_errors']['disable_live'], 'RolloutError')
        self.assertEqual(saved['stage'], 'stopped_for_review')
        self.assertTrue(any(x[0] == 'failure_notice' for x in self.log))

    def test_loaded_units_must_match_files_and_have_no_overrides(self):
        valid = '\n'.join('FragmentPath=/etc/systemd/system/%s\nDropInPaths=\nNeedDaemonReload=no' % unit
                          for unit in (boot.SERVICE, boot.TIMER))
        def same_contents(path): return path.name.encode('utf-8')
        with patch.object(Path, 'read_bytes', same_contents):
            self.worker.systemctl = lambda *args: subprocess.CompletedProcess(args, 0, stdout=valid)
            boot.Bootstrap.verify_units(self.worker)
            for output in ('', valid.replace('DropInPaths=', 'DropInPaths=/unrelated/override.conf', 1),
                           valid.replace('NeedDaemonReload=no', 'NeedDaemonReload=yes', 1),
                           valid.replace('/etc/systemd/system/', '/other/', 1)):
                self.worker.systemctl = lambda *args, output=output: subprocess.CompletedProcess(args, 0, stdout=output)
                with self.assertRaises(boot.RolloutError): boot.Bootstrap.verify_units(self.worker)
        def changed_contents(path): return b'old' if 'etc' in path.parts else b'new'
        with patch.object(Path, 'read_bytes', changed_contents):
            with self.assertRaisesRegex(boot.RolloutError, 'unit_content'): boot.Bootstrap.verify_units(self.worker)

    def test_failure_notice_has_own_ledger_and_unknown_is_not_resent(self):
        calls = []; saved = {}; runtime_states = []
        class FakeRuntime:
            def __init__(self, root): pass
            def begin(self, run_id, mode, data): runtime_states.append((run_id, mode, data))
            def update(self, run_id, **data): pass
            def notice(self, run_id, phase): return saved.get((run_id, phase))
            def close(self): pass
        class FakeNotifier:
            def __init__(self, runtime, path): pass
            def send(self, run_id, phase, body):
                if (run_id, phase) in saved:
                    raise RuntimeError('notification_unknown_requires_reconciliation')
                calls.append((run_id, phase, body))
                saved[(run_id, phase)] = {'status': 'unknown', 'body': body, 'receipt': '{}'}
                raise RuntimeError('unknown')
        with patch.object(boot, 'Runtime', FakeRuntime), patch.object(boot, 'Notifier', FakeNotifier):
            for _ in range(2):
                with self.assertRaises(RuntimeError): boot.Bootstrap.notify_failure(self.worker, 'scan_exceeds_daily_runtime_budget')
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:2], ('preview-test-bootstrap', 'end'))
        self.assertIn('scan_exceeds_daily_runtime_budget', calls[0][2])
        self.assertIn(str(self.worker.state_path), calls[0][2])
        self.assertNotEqual(runtime_states[0][0], self.run['id'])
        self.assertEqual(self.worker.state['failure_notification_status'], 'unknown')


if __name__ == '__main__': unittest.main()
