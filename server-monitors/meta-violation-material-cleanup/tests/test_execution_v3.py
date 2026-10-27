"""V3 integration: actual Store claims/receipts, deterministic SQL and Meta."""
from contextlib import contextmanager
import copy
import pathlib
import tempfile
import threading
import unittest
from unittest.mock import patch

from features.fb_ad_asset_delete.store import Store
from meta_violation_cleanup.graph import GraphError
from meta_violation_cleanup.policy import (
    POLICY_CONTRACT, POLICY_VERSION, evaluate_ad, material_identity,
)
from meta_violation_cleanup.runner import ACTOR, Runner
from meta_violation_cleanup.runtime import digest


def ad(identifier='10', creative='20', account='100', status='PAUSED'):
    return {'id': identifier, 'account_id': account, 'creative': {'id': creative},
            'status': status, 'effective_status': status,
            'adset': {'id': '30', 'status': 'ACTIVE'},
            'campaign': {'id': '40', 'status': 'ACTIVE'},
            'issues_info': [], 'ad_review_feedback': {}}


def evidence(count=5):
    return {'materials': {'501': {'count': count,
            'violating_ad_ids': [str(101+i) for i in range(count)],
            'policies': ['Spam'], 'root_ids': ['1479']}}}


class Source:
    def __init__(self, ads):
        self.owner = threading.get_ident()
        self.rows = {a['id']: [{'ad_id': a['id'], 'creative_id': a['creative']['id'],
                     'product': '1479', 'source_id': '0', 'original_source_id': '501',
                     'ad_account_id': a['account_id']}]
                     for a in ads}
        self.calls = []
        self.on_context = None
        self.worker_access = []

    @contextmanager
    def snapshot(self):
        if threading.get_ident() != self.owner:
            self.worker_access.append(threading.get_ident())
            raise AssertionError('worker entered SQL snapshot')
        yield

    def current_ad_context(self, aid, cid):
        if threading.get_ident() != self.owner:
            self.worker_access.append(threading.get_ident())
            raise AssertionError('worker read SQL')
        self.calls.append((aid, cid))
        if self.on_context:
            self.on_context(aid, cid)
        return {'rows': copy.deepcopy(self.rows.get(aid, [])), 'sources': {},
                'scope': {'1479': '1479'}, 'credential_users': ['1']}


class Graph:
    def __init__(self, ads):
        self.ads = {a['id']: copy.deepcopy(a) for a in ads}
        self.calls = []
        self.results = {}
        self.proofs = {}
        self.on_get = None
        self.on_inventory = None
        self.on_delete = None
        self.lock = threading.Lock()

    def list_ads(self, account, user):
        with self.lock:
            self.calls.append(('inventory', account, user))
        if self.on_inventory:
            self.on_inventory(account, user)
        return copy.deepcopy([a for a in self.ads.values()
                              if a['account_id'] == account and a['status'] != 'DELETED'])

    def get_ad(self, identifier, user):
        with self.lock:
            self.calls.append(('get', identifier))
        if self.on_get:
            self.on_get(identifier)
        if identifier not in self.ads:
            raise GraphError(100, 'Object unreadable')
        return copy.deepcopy(self.ads[identifier])

    def delete(self, kind, identifier, user, account):
        with self.lock:
            self.calls.append(('delete', kind, identifier))
        if self.on_delete:
            self.on_delete(kind, identifier)
        result = copy.deepcopy(self.results.get((kind, identifier),
                               {'status': 'api_accepted', 'request_sent': True}))
        if result['status'] == 'api_accepted' and kind == 'ad':
            self.ads[identifier].update(status='DELETED', effective_status='DELETED')
        return result

    def reconcile(self, kind, identifier, user, account):
        return copy.deepcopy(self.proofs.get((kind, identifier),
            {'status': 'deleted_verified', 'object': {
                'id': identifier, 'account_id': account, 'status': 'DELETED'}}))


class Runtime:
    def __init__(self):
        self.data = {}
        self.events = []
        self.artifacts = {}
        self.queue = {}

    def notice(self, run, phase):
        return {'status': 'sent'}

    def update(self, run, **data):
        self.data.update(copy.deepcopy(data))

    def event(self, run, kind, data):
        self.events.append((kind, copy.deepcopy(data)))

    def artifact(self, run, name, data):
        self.artifacts[(run, name)] = copy.deepcopy(data)

    def queue_creative(self, entry):
        self.queue[entry['basis']['creative_id']] = copy.deepcopy(entry)

    def cleanup_backlog(self):
        return list(copy.deepcopy(self.queue).values())

    def complete_creative(self, cid):
        self.queue.pop(cid, None)

    def write_barrier(self, run):
        pass


class Lease:
    def check(self):
        pass


@contextmanager
def legacy_lock(path, stop):
    yield Lease()


class ExecutionV3Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(str(pathlib.Path(self.temp.name) / 'ledger.sqlite3'))
        owner = threading.get_ident()
        original = self.store._transaction
        self.sqlite_worker_access = []
        @contextmanager
        def transaction(*args, **kwargs):
            if threading.get_ident() != owner:
                self.sqlite_worker_access.append(threading.get_ident())
                raise AssertionError('worker accessed SQLite')
            with original(*args, **kwargs) as connection:
                yield connection
        self.store._transaction = transaction
        for name, value in (
            ('meta_violation_cleanup.concurrent_execution.legacy_delete_lock', legacy_lock),
            ('features.fb_ad_asset_delete.store._process_start', lambda *a: (True, 'v3-test-owner')),
        ):
            patcher = patch(name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def fixture(self, initial=None, references=None):
        initial = [ad()] if initial is None else initial
        references = references or []
        all_ads = initial + references
        source, graph, runtime = Source(all_ads), Graph(all_ads), Runtime()
        runner = Runner({'legacy_lock': 'unused', 'execution_workers': 4,
                         'execution_per_account': 2, 'shared_reference_scan_workers': 4},
                        source, graph, runtime, self.store, None)
        proof = evidence()
        candidates = []
        for current in initial:
            identity = material_identity(source.rows[current['id']], {}, {'1479': '1479'},
                                         current['creative']['id'])
            decision = evaluate_ad(current, identity, proof)
            candidates.append(dict(decision, identity=identity, user_id='1',
                                   account_id=current['account_id'], ad_snapshot=copy.deepcopy(current)))
        manifest = {'version': 3, 'policy_version': POLICY_VERSION,
                    'selection_policy': copy.deepcopy(POLICY_CONTRACT), 'run_id': 'v3-run',
                    'window_start': '2026-10-24T09:00:00+08:00',
                    'window_end': '2026-10-27T09:00:00+08:00',
                    'frozen_at': '2026-10-27T09:20:00+08:00',
                    'evidence': proof, 'candidates': candidates, 'recovery_creatives': [],
                    'blocked_account_creatives': [], 'coverage': {'errors': [],
                    'accounts': [{'account_id': account, 'status': 'complete'}
                                 for account in sorted({a['account_id'] for a in all_ads})]}}
        self.freeze(manifest)
        return runner, source, graph, runtime, manifest

    def freeze(self, manifest):
        manifest['sha256'] = digest({k: v for k, v in manifest.items() if k != 'sha256'})

    def recovery(self, manifest, cid='20', blocked=None):
        basis = next(c for c in manifest['candidates'] if c['creative_id'] == cid)
        entry = {'basis': copy.deepcopy(basis), 'origin_run_id': 'old-run',
                 'origin_manifest_sha256': 'old-proof', 'evidence': evidence(),
                 'blocked_ads': copy.deepcopy(blocked or [])}
        manifest['recovery_creatives'].append(entry)
        self.freeze(manifest)
        return entry

    def deletes(self, graph):
        return [row[1:] for row in graph.calls if row[0] == 'delete']

    def execute(self, runner, manifest):
        outcome = runner.execute(manifest['run_id'], manifest)
        self.assertEqual(self.sqlite_worker_access, [])
        self.assertEqual(runner.source.worker_access, [])
        return outcome

    def seed_result(self, kind, identifier, status, result, account='100', creative='20'):
        job = 'seed-' + kind + '-' + identifier
        self.store.create_job({'job_id': job, 'preview_id': 'seed-proof', 'actor': ACTOR,
            'objects': [{'kind': kind, 'object_id': identifier, 'account_ids': [account],
                         'user_ids': ['1'], 'creative_ids': [creative]}]})
        run = self.store.claim_run(job, 'seed-proof', ACTOR, [kind], job + '-request')
        claim = self.store.claim_object(job, kind + ':' + identifier, run['run_id'])
        self.assertTrue(claim['claimed'])
        self.store.finish_object(job, kind + ':' + identifier, run['run_id'], status, result)
        self.store.finish_run(run['run_id'], 'partial')

    def test_selected_and_shared_ads_finish_before_creative_and_sql_stays_on_owner(self):
        runner, source, graph, runtime, manifest = self.fixture(references=[ad('12')])
        self.assertEqual(self.execute(runner, manifest), 'completed')
        self.assertEqual(self.deletes(graph), [('ad', '10'), ('ad', '12'), ('creative', '20')])
        self.assertEqual(runtime.queue, {})
        objects = self.store.get_job('policy-cleanup-v3-run')['objects']
        self.assertEqual({(o['kind'], o['object_id'], o['status']) for o in objects},
                         {('ad', '10', 'deleted'), ('ad', '12', 'deleted'), ('creative', '20', 'deleted')})
        self.assertEqual(runtime.data['reference_scan_metrics']['account_scans'], 1)

    def test_active_shared_ad_and_creative_are_preserved(self):
        runner, source, graph, runtime, manifest = self.fixture(references=[ad('12', status='ACTIVE')])
        self.assertEqual(self.execute(runner, manifest), 'partial')
        self.assertEqual(self.deletes(graph), [('ad', '10')])
        self.assertEqual(runtime.queue['20']['blocked_ads'][0]['result']['reason'], 'active_ad_protected')

    def test_shared_ad_must_also_satisfy_five_violation_material_rule(self):
        runner, source, graph, runtime, manifest = self.fixture(references=[ad('12')])
        source.rows['12'][0]['original_source_id'] = '502'
        manifest['evidence']['materials']['502'] = {'count': 4, 'violating_ad_ids': ['1', '2', '3', '4']}
        self.freeze(manifest)
        self.execute(runner, manifest)
        self.assertEqual(self.deletes(graph), [('ad', '10')])

    def test_known_unchanged_active_blocker_avoids_account_inventory(self):
        runner, source, graph, runtime, manifest = self.fixture(references=[ad('12', status='ACTIVE')])
        self.recovery(manifest, blocked=[{'ad_id': '12', 'result': {'reason': 'active_ad_protected'}}])
        self.execute(runner, manifest)
        self.assertFalse(any(c[0] == 'inventory' for c in graph.calls))
        self.assertEqual(self.deletes(graph), [('ad', '10')])
        self.assertEqual(runtime.data['reference_scan_metrics']['skipped_creative_scans'], 1)

    def test_known_unreadable_blocker_never_counts_as_cleared(self):
        runner, source, graph, runtime, manifest = self.fixture(references=[ad('12')])
        self.recovery(manifest, blocked=[{'ad_id': '12'}])
        graph.ads.pop('12')
        self.execute(runner, manifest)
        self.assertFalse(any(c[0] == 'inventory' for c in graph.calls))
        self.assertEqual(self.deletes(graph), [('ad', '10')])

    def test_cleared_known_blocker_requires_full_inventory_and_finds_new_active_reference(self):
        runner, source, graph, runtime, manifest = self.fixture(
            references=[ad('12', status='DELETED'), ad('13', status='ACTIVE')])
        self.recovery(manifest, blocked=[{'ad_id': '12'}])
        self.execute(runner, manifest)
        self.assertEqual(sum(c[0] == 'inventory' for c in graph.calls), 1)
        self.assertEqual(self.deletes(graph), [('ad', '10')])
        self.assertEqual(runtime.queue['20']['blocked_ads'][0]['ad_id'], '13')

    def test_blocked_creative_does_not_prevent_other_creative_in_same_account(self):
        runner, source, graph, runtime, manifest = self.fixture(
            initial=[ad(), ad('11', '21')], references=[ad('12', status='ACTIVE')])
        self.recovery(manifest, blocked=[{'ad_id': '12'}])
        self.execute(runner, manifest)
        self.assertIn(('creative', '21'), self.deletes(graph))
        self.assertNotIn(('creative', '20'), self.deletes(graph))
        self.assertEqual(sum(c[0] == 'inventory' for c in graph.calls), 1)

    def test_shared_sql_identity_changes_after_inventory_qualification(self):
        runner, source, graph, runtime, manifest = self.fixture(references=[ad('12')])
        calls = []
        def change(aid, cid):
            if aid == '12':
                calls.append(aid)
                if len(calls) == 2:
                    source.rows['12'][0]['original_source_id'] = '999'
        source.on_context = change
        self.execute(runner, manifest)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.deletes(graph), [('ad', '10')])

    def test_shared_ad_resumes_after_inventory_before_delete(self):
        runner, source, graph, runtime, manifest = self.fixture(references=[ad('12')])
        def change(identifier):
            if identifier == '12':
                graph.ads['12'].update(status='ACTIVE', effective_status='ACTIVE')
        graph.on_get = change
        self.execute(runner, manifest)
        self.assertEqual(self.deletes(graph), [('ad', '10')])

    def test_shared_ad_rebinds_before_delete(self):
        runner, source, graph, runtime, manifest = self.fixture(references=[ad('12')])
        def change(identifier):
            if identifier == '12':
                graph.ads['12']['creative']['id'] = '99'
        graph.on_get = change
        self.execute(runner, manifest)
        self.assertEqual(self.deletes(graph), [('ad', '10')])

    def test_unknown_ad_write_is_not_replayed_and_does_not_release_creative(self):
        runner, source, graph, runtime, manifest = self.fixture()
        graph.results[('ad', '10')] = {'status': 'unknown', 'request_sent': True}
        graph.proofs[('ad', '10')] = {'status': 'unknown'}
        self.execute(runner, manifest)
        self.assertEqual(self.deletes(graph), [('ad', '10')])
        second = copy.deepcopy(manifest)
        second['run_id'] = 'next-run'
        second['recovery_creatives'] = runtime.cleanup_backlog()
        self.freeze(second)
        self.execute(runner, second)
        self.assertEqual(self.deletes(graph), [('ad', '10')])

    def test_unknown_creative_write_blocks_later_reference_work(self):
        runner, source, graph, runtime, manifest = self.fixture()
        graph.results[('creative', '20')] = {'status': 'unknown', 'request_sent': True}
        graph.proofs[('creative', '20')] = {'status': 'unknown'}
        self.execute(runner, manifest)
        before = len(graph.calls)
        second = copy.deepcopy(manifest)
        second.update(run_id='next-run', candidates=[], recovery_creatives=runtime.cleanup_backlog())
        self.freeze(second)
        self.execute(runner, second)
        self.assertEqual(self.deletes(graph).count(('creative', '20')), 1)
        self.assertFalse(any(c[0] == 'inventory' for c in graph.calls[before:]))

    def test_known_deleted_ad_with_unsettled_unknown_fence_still_blocks_creative(self):
        runner, source, graph, runtime, manifest = self.fixture(references=[ad('12', status='DELETED')])
        self.recovery(manifest, blocked=[{'ad_id': '12'}])
        self.seed_result('ad', '12', 'unknown', {'status': 'unknown', 'request_sent': True})
        self.execute(runner, manifest)
        self.assertEqual(self.deletes(graph), [('ad', '10')])
        self.assertIn('20', runtime.queue)

    def test_current_reference_conflicting_with_old_deleted_receipt_is_held(self):
        runner, source, graph, runtime, manifest = self.fixture(references=[ad('12')])
        self.seed_result('ad', '12', 'deleted', {
            'status': 'deleted_verified', 'request_sent': True, 'api_accepted': True,
            'verification': {'status': 'deleted_verified',
                             'object': {'id': '12', 'account_id': '100', 'status': 'DELETED'}}})
        self.execute(runner, manifest)
        self.assertEqual(self.deletes(graph), [('ad', '10')])
        self.assertIn('20', runtime.queue)

    def test_partial_account_scan_cannot_authorize_creative(self):
        runner, source, graph, runtime, manifest = self.fixture()
        def incomplete(*args):
            raise GraphError('invalid_page', 'Invalid final page')
        graph.on_inventory = incomplete
        self.execute(runner, manifest)
        self.assertEqual(self.deletes(graph), [('ad', '10')])

    def test_new_active_reference_after_scan_defers_on_meta_dependency_rejection(self):
        runner, source, graph, runtime, manifest = self.fixture()
        def add_after_inventory(aid, cid):
            if aid == '10' and any(c[0] == 'inventory' for c in graph.calls):
                graph.ads['12'] = ad('12', status='ACTIVE')
        source.on_context = add_after_inventory
        def reject_creative(kind, identifier):
            if kind == 'creative':
                self.assertIn('12', graph.ads)
                graph.results[(kind, identifier)] = {
                    'status': 'failed', 'request_sent': True,
                    'response': {'code': 100, 'error_subcode': 3858800}}
        graph.on_delete = reject_creative
        self.assertEqual(self.execute(runner, manifest), 'partial')
        self.assertEqual(self.deletes(graph), [('ad', '10'), ('creative', '20')])
        self.assertEqual(sum(c[0] == 'inventory' for c in graph.calls), 1)
        self.assertEqual(runtime.queue['20']['last_result']['response']['error_subcode'], 3858800)
        self.assertEqual(graph.ads['12']['status'], 'ACTIVE')

    def test_parent_pause_is_supported(self):
        current = ad(status='ACTIVE')
        current['campaign']['status'] = 'PAUSED'
        current['effective_status'] = 'CAMPAIGN_PAUSED'
        runner, source, graph, runtime, manifest = self.fixture(initial=[current])
        self.assertEqual(self.execute(runner, manifest), 'completed')
        self.assertEqual(self.deletes(graph), [('ad', '10'), ('creative', '20')])

    def test_historical_deleted_active_basis_cannot_use_receipt_to_bypass_pause(self):
        runner, source, graph, runtime, manifest = self.fixture()
        entry = self.recovery(manifest)
        entry['basis']['ad_snapshot'] = ad(status='ACTIVE')
        manifest['candidates'] = []
        self.freeze(manifest)
        graph.ads['10'].update(status='DELETED', effective_status='DELETED')
        self.seed_result('ad', '10', 'deleted', {
            'status': 'deleted_verified', 'request_sent': True, 'api_accepted': True,
            'verification': {'status': 'deleted_verified',
                             'object': {'id': '10', 'account_id': '100', 'status': 'DELETED'}}})
        self.execute(runner, manifest)
        self.assertEqual(self.deletes(graph), [])
        self.assertEqual(runtime.queue['20']['reference_check']['reason'], 'pre_delete_pause_proof_unavailable')

    def test_historical_deleted_paused_basis_still_requires_fresh_five_threshold(self):
        runner, source, graph, runtime, manifest = self.fixture()
        self.recovery(manifest)
        manifest.update(candidates=[], evidence=evidence(4))
        self.freeze(manifest)
        graph.ads['10'].update(status='DELETED', effective_status='DELETED')
        self.seed_result('ad', '10', 'deleted', {
            'status': 'deleted_verified', 'request_sent': True, 'api_accepted': True,
            'verification': {'status': 'deleted_verified',
                             'object': {'id': '10', 'account_id': '100', 'status': 'DELETED'}}})
        self.execute(runner, manifest)
        self.assertEqual(self.deletes(graph), [])
        self.assertEqual(runtime.queue['20']['reference_check']['reason'], 'violation_threshold_not_met')

    def test_worker_basis_validation_with_explicit_empty_proof_never_reads_sqlite(self):
        from concurrent.futures import ThreadPoolExecutor
        from meta_violation_cleanup.phased_execution import basis_valid
        runner, source, graph, runtime, manifest = self.fixture()
        candidate = manifest['candidates'][0]
        context = source.current_ad_context('10', '20')
        with ThreadPoolExecutor(max_workers=1) as pool:
            valid, reason = pool.submit(basis_valid, runner, candidate, manifest['evidence'],
                                        context=context, graph=graph, receipt={}).result()
        self.assertTrue(valid)
        self.assertEqual(self.sqlite_worker_access, [])
        self.assertEqual(source.worker_access, [])

    def test_old_policy_and_non_three_day_window_rejected_before_mutation(self):
        runner, source, graph, runtime, manifest = self.fixture()
        for field, value in [('policy_version', '20261024-v2'),
                             ('window_start', '2026-09-27T09:00:00+08:00')]:
            invalid = copy.deepcopy(manifest)
            invalid[field] = value
            self.freeze(invalid)
            with self.assertRaises(ValueError):
                runner.execute(invalid['run_id'], invalid)
        self.assertEqual(self.deletes(graph), [])

    def test_incomplete_or_missing_frozen_account_coverage_rejected_before_any_read_or_write(self):
        runner, source, graph, runtime, manifest = self.fixture()
        for accounts in ([], [{'account_id': '100', 'status': 'partial'}],
                         [{'account_id': '999', 'status': 'complete'}]):
            invalid = copy.deepcopy(manifest)
            invalid['coverage']['accounts'] = accounts
            self.freeze(invalid)
            with self.subTest(accounts=accounts), self.assertRaisesRegex(
                    ValueError, 'candidate_account_coverage_incomplete'):
                runner.execute(invalid['run_id'], invalid)
        self.assertEqual(graph.calls, [])
        self.assertEqual(source.calls, [])
        with self.store._transaction(False) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM fb_asset_delete_v2_jobs').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
