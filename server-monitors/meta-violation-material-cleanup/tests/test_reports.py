"""Bounded summaries preserve evidence and count final unique ledger objects."""
import copy
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

HERE = pathlib.Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1]))
sys.path.insert(0, str(HERE.parents[3]))
from features.fb_ad_asset_delete.store import Store, StoreError
from meta_violation_cleanup.reports import classify, summarize_objects
from meta_violation_cleanup.runner import ACTOR, Runner
from meta_violation_cleanup.runtime import Runtime


def row(identifier, status='pending', result=None, kind='ad'):
    return {'key': kind + ':' + identifier, 'kind': kind, 'object_id': identifier,
            'account_ids': ['100'], 'user_ids': ['1'], 'status': status, 'result': result or {}}


def verified(identifier, **overrides):
    result = {'status': 'deleted_verified', 'api_accepted': True, 'request_sent': True,
              'confirmed_deleted': True, 'verification': {'status': 'deleted_verified',
              'object': {'id': identifier, 'account_id': '100', 'status': 'DELETED'}}}
    result.update(overrides)
    return result


def failed(code=100, subcode=2446289):
    return {'status': 'failed', 'request_sent': True,
            'response': {'code': code, 'error_subcode': subcode}}


class SnapshotStore:
    def __init__(self, objects):
        self.objects = objects
        self.reads = []

    def get_job(self, job_id):
        self.reads.append(job_id)
        return {'objects': copy.deepcopy(self.objects)}


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = Runtime(self.temp.name)
        self.runtime.begin('report', 'run', {'execution_started_at': '2026-10-25T01:00:00Z',
            'coverage_errors': 0, 'result_counts': {'ad_failed': 8836, 'ad_deleted_verified': 91}})
        self.runtime.update('report', status='partial')
        self.runtime.db.execute("UPDATE runs SET ended=? WHERE id=?", ('2026-10-25T10:01:02+08:00', 'report'))
        self.manifest = {'candidates': [{'ad_id': '10', 'root_id': '1479'},
                                       {'ad_id': '11', 'root_id': '3302'}],
                         'coverage': {'errors': []}, 'shared_reference_coverage': 'checked_on_dependency_failure'}
        self.runtime.artifact('report', 'manifest.json', self.manifest)
        self.store = SnapshotStore([])
        self.notifier = Mock()
        self.runner = Runner({}, None, None, self.runtime, self.store, self.notifier)

    def tearDown(self):
        self.runtime.close()
        self.temp.cleanup()

    def test_start_is_three_lines_with_beijing_time_batch_and_window(self):
        with patch('meta_violation_cleanup.reports.now', return_value='2026-10-26T01:00:00Z'):
            text = self.runner.start_text('report', '2026-10-23T01:00:00Z', '2026-10-26T01:00:00Z')
        self.assertEqual(len(text.splitlines()), 3)
        self.assertIn('北京时间 2026-10-26 09:00:00', text)
        self.assertIn('批次：report', text)
        self.assertIn('2026-10-23 09:00:00 至 2026-10-26 09:00:00', text)

    def test_database_failure_names_native_error_without_raw_sensitive_text(self):
        self.runtime.update('report', status='failed', error_type='SqlSourceError',
                            mysql_error_codes=[3024, 'synthetic-secret', True, -1, 999999],
                            failure_code='sql_stream_incomplete', stderr='synthetic-secret')
        text = self.runner.end_text('report')
        self.assertIn('数据库查询超时（MySQL 3024）', text)
        self.assertIn('mysql-errors.jsonl', text)
        self.assertNotIn('synthetic-secret', text)

    def test_final_unique_categories_are_mutually_exclusive_and_ignore_event_counts(self):
        self.store.objects = [row('10', 'deleted', verified('10')),
            row('11', 'unknown', {'api_accepted': True, 'verification': {'status': 'unknown'}}),
            row('12', 'already_deleted', {'reason': 'previous_task_deleted', 'proof': verified('12')}),
            row('13', 'failed', failed(10, 999)),
            row('14', 'failed', {'request_sent': False, 'code': 'current_policy_changed'}),
            row('15', 'unknown', {'reason': 'unknown_fence', 'verification': {'status': 'present',
                'object': {'id': '15', 'account_id': '100', 'status': 'ACTIVE'}}}),
            row('16'), row('17', 'blocked', {'status': 'blocked', 'request_sent': False,
                'policy_skip_reason': 'content_unavailable'}),
            row('20', 'deleted', verified('20'), kind='creative')]
        text = self.runner.end_text('report')
        for expected in ('广告（最终唯一8）：已确认删除1；已接受待核验1；历史已删回执1；平台失败1；按策略跳过1；未提交跳过1；未知1；待处理1',
                         'Creative（最终唯一1）：已确认删除1', '失败主因1：其他平台拒绝 1个',
                         'DramaWave 1；HotDrama 1', '2026-10-25 09:00:00 → 2026-10-25 10:01:02', '耗时：1时1分2秒'):
            self.assertIn(expected, text)
        self.assertNotIn('8836', text)
        self.assertNotIn('共享引用覆盖未完成', text)
        self.assertEqual(self.store.reads, ['policy-cleanup-report'])
        self.notifier.assert_not_called()

    def test_repeated_failures_then_success_count_once_using_real_store(self):
        store = Store(str(pathlib.Path(self.temp.name) / 'shared.sqlite3'))
        store.create_job({'job_id': 'policy-cleanup-report', 'preview_id': 'hash', 'actor': ACTOR,
                          'objects': [row('10')]})
        with patch('features.fb_ad_asset_delete.store._process_start', return_value=(True, 'test-owner')):
            run = store.claim_run('policy-cleanup-report', 'hash', ACTOR, ['ad'], 'test-request')['run_id']
        for result, status in ((failed(), 'failed'), (failed(), 'failed'), (verified('10'), 'deleted')):
            self.assertTrue(store.claim_object('policy-cleanup-report', 'ad:10', run)['claimed'])
            store.finish_object('policy-cleanup-report', 'ad:10', run, status, result)
        store.finish_run(run, 'completed', {'ad_failed': 2, 'ad_deleted_verified': 1})
        self.runner.store = store
        before = store.get_job('policy-cleanup-report')
        text = self.runner.end_text('report')
        self.assertIn('广告（最终唯一1）：已确认删除1', text)
        self.assertIn('平台失败0', text)
        self.assertNotIn('失败主因', text)
        self.assertEqual(store.get_job('policy-cleanup-report'), before)

    def test_existing_receipt_stays_existing_even_with_nested_exact_proof(self):
        nested = verified('10', request_sent=False, reconciliation_only=True)
        nested['proof'] = {'id': '10', 'account_id': '100', 'status': 'DELETED'}
        self.assertEqual(classify(row('10', 'already_deleted', {'reason': 'previous_task_deleted',
            'source_job_id': 'old-job', 'proof': nested})), 'receipt')

    def test_exact_readback_without_delete_request_is_verified_not_new_api_acceptance(self):
        self.assertEqual(classify(row('10', 'deleted', {'status': 'deleted_verified', 'request_sent': False,
            'object': {'id': '10', 'account_id': '100', 'status': 'DELETED'}})), 'verified')
        self.assertEqual(classify(row('10', 'already_deleted', verified('10',
            request_sent=False, reconciliation_only=True, api_accepted=False))), 'verified')

    def test_success_labels_wrong_account_or_effective_status_are_not_exact_proof(self):
        for field, value in (('id', '11'), ('account_id', '101'), ('status', 'ACTIVE')):
            result = verified('10')
            result['verification']['object'][field] = value
            result['verification']['object']['effective_status'] = 'DELETED'
            with self.subTest(field=field):
                self.assertEqual(classify(row('10', 'deleted', result)), 'accepted')
        self.assertEqual(classify(row('10', 'deleted', {'status': 'deleted_verified',
            'confirmed_deleted': True})), 'unknown')

    def test_ledger_deleted_with_only_api_acceptance_is_not_verified(self):
        self.assertEqual(classify(row('10', 'deleted', {'api_accepted': True, 'request_sent': True})), 'accepted')
        self.assertEqual(classify(row('10', 'unknown', {'delete_status': 'api_accepted',
            'verification': {'status': 'present'}})), 'accepted')

    def test_final_top_three_failures_merge_shared_codes_and_exclude_unsent(self):
        self.store.objects = [row(str(10+i), 'failed', failed()) for i in range(5)]
        self.store.objects += [row(str(30+i), 'failed', failed(200, 2490592)) for i in range(4)]
        self.store.objects += [row('40', 'failed', failed(200, 1487235)), row('41', 'failed', failed(100, 3858800)),
            row('50', 'failed', failed(10, 999)), row('60', 'failed', dict(failed(), request_sent=False))]
        text = self.runner.end_text('report')
        for expected in ('失败主因1：共享引用阻塞 2个', '失败主因2：其他平台拒绝 1个',
                         '平台失败3；按策略跳过9；未提交跳过1',
                         '策略跳过：帖子失效/无权限5；账户不可写入4。'):
            self.assertIn(expected, text)
        self.assertNotIn('失败主因3', text)
        self.assertNotIn('失败主因1：内容', text)
        self.assertNotIn('失败主因2：账户', text)

    def test_policy_skip_keeps_actual_request_truth_and_counts_both_kinds(self):
        initial = dict(failed(), policy_skip_reason='content_unavailable')
        cached = {'status': 'blocked', 'request_sent': False,
                  'policy_skip_reason': 'account_not_writable',
                  'policy_skip_source': {'source_result': failed(200, 2490592)}}
        self.store.objects = [row('10', 'failed', initial),
                              row('20', 'blocked', cached, kind='creative')]
        before = copy.deepcopy(self.store.objects)
        counts, failures = summarize_objects(self.store.objects)
        self.assertEqual(counts['ad']['policy_skip'], 1)
        self.assertEqual(counts['creative']['policy_skip'], 1)
        self.assertFalse(failures)
        text = self.runner.end_text('report')
        self.assertIn('策略跳过：帖子失效/无权限1；账户不可写入1。', text)
        self.assertNotIn('失败主因', text)
        self.assertNotIn('2446289', text)
        self.assertNotIn('2490592', text)
        self.assertEqual(self.store.objects, before)
        self.assertTrue(self.store.objects[0]['result']['request_sent'])
        self.assertFalse(self.store.objects[1]['result']['request_sent'])

    def test_unknown_accepted_and_receipts_precede_policy_skip(self):
        marker = {'policy_skip_reason': 'content_unavailable'}
        cases = [
            (row('10', 'unknown', dict(failed(), **marker)), 'unknown'),
            (row('10', 'in_progress', dict(failed(), **marker)), 'unknown'),
            (row('10', 'failed', dict(failed(), status='unknown', **marker)), 'unknown'),
            (row('10', 'blocked', dict(failed(), reason='unknown_fence', **marker)), 'unknown'),
            (row('10', 'unknown', {'policy_skip_source': {'source_result': failed()}}), 'unknown'),
            (row('10', 'unknown', dict(failed(), api_accepted=True, **marker)), 'accepted'),
            (row('10', 'deleted', verified('10', **marker)), 'verified'),
            (row('10', 'already_deleted', dict(marker, reason='previous_task_deleted',
                proof=verified('10'))), 'receipt'),
            (row('10', 'pending', marker), 'pending'),
        ]
        for final_row, expected in cases:
            with self.subTest(status=final_row['status'], result=final_row['result']):
                self.assertEqual(classify(final_row), expected)

    def test_historical_policy_skip_requires_exact_pair_sent_and_final_failure(self):
        for code, subcode in ((200, 2446289), (100, 2490592), ('100', 2446289), (100, '2446289')):
            with self.subTest(code=code, subcode=subcode):
                self.assertEqual(classify(row('10', 'failed', failed(code, subcode))), 'failed')
        self.assertEqual(classify(row('10', 'failed', dict(failed(), request_sent=False))), 'skipped')
        self.assertEqual(classify(row('10', 'blocked', failed())), 'pending')
        self.assertEqual(classify(row('10', 'blocked', {'source_result': failed()})), 'pending')
        self.assertEqual(classify(row('10', 'failed', {'request_sent': True,
            'policy_skip_source': {'source_result': failed()}})), 'failed')

    def test_content_skip_is_ad_only_but_account_skip_applies_to_creative(self):
        for result in (failed(), dict(failed(), policy_skip_reason='content_unavailable')):
            self.assertEqual(classify(row('20', 'failed', result, kind='creative')), 'failed')
        self.assertEqual(classify(row('20', 'blocked', {'request_sent': False,
            'policy_skip_reason': 'content_unavailable'}, kind='creative')), 'pending')
        self.assertEqual(classify(row('20', 'failed', failed(200, 2490592), kind='creative')), 'policy_skip')

    def test_repeated_policy_skip_uses_final_unique_objects_not_events(self):
        store = Store(str(pathlib.Path(self.temp.name) / 'policy-shared.sqlite3'))
        store.create_job({'job_id': 'policy-cleanup-report', 'preview_id': 'hash', 'actor': ACTOR,
                          'objects': [row('10')]})
        with patch('features.fb_ad_asset_delete.store._process_start', return_value=(True, 'test-owner')):
            run = store.claim_run('policy-cleanup-report', 'hash', ACTOR, ['ad'], 'policy-request')['run_id']
        for result in (failed(), dict(failed(), policy_skip_reason='content_unavailable')):
            self.assertTrue(store.claim_object('policy-cleanup-report', 'ad:10', run)['claimed'])
            store.finish_object('policy-cleanup-report', 'ad:10', run, 'failed', result)
        store.finish_run(run, 'partial', {'ad_failed': 2})
        self.runner.store = store
        text = self.runner.end_text('report')
        self.assertIn('广告（最终唯一1）', text)
        self.assertIn('平台失败0；按策略跳过1', text)
        self.assertIn('策略跳过：帖子失效/无权限1；账户不可写入0。', text)
        self.assertNotIn('失败主因', text)

    def test_zero_objects_are_zero_only_with_valid_ledger_snapshot(self):
        text = self.runner.end_text('report')
        self.assertIn('广告（最终唯一0）', text)
        self.assertIn('Creative（最终唯一0）', text)
        self.assertNotIn('未取得', text)

    def test_preview_without_job_does_not_invent_success(self):
        self.runtime.db.execute("UPDATE runs SET mode='preview',status='previewed' WHERE id='report'")
        with patch.object(self.store, 'get_job', side_effect=StoreError('missing', 'not_found')):
            text = self.runner.end_text('report')
        self.assertIn('预演：未执行删除；无最终执行统计', text)
        self.assertNotIn('已确认删除', text)

    def test_preview_converted_to_execution_uses_final_ledger(self):
        self.runtime.db.execute("UPDATE runs SET mode='preview' WHERE id='report'")
        self.runtime.update('report', execution_mode='run', execution_attempt=1,
                            shared_job_id='policy-cleanup-report')
        self.store.objects = [row('10', 'deleted', verified('10'))]
        text = self.runner.end_text('report')
        self.assertIn('广告（最终唯一1）：已确认删除1', text)
        self.assertNotIn('未执行删除', text)

    def test_existing_job_overrides_original_preview_mode_even_without_execution_metadata(self):
        self.runtime.db.execute("UPDATE runs SET mode='preview' WHERE id='report'")
        self.store.objects = [row('10', 'deleted', verified('10'))]
        text = self.runner.end_text('report')
        self.assertIn('广告（最终唯一1）：已确认删除1', text)
        self.assertNotIn('未执行删除', text)

    def test_missing_job_and_ledger_errors_do_not_fall_back_to_event_counts(self):
        for error, expected in ((StoreError('missing', 'not_found'), '未建立执行账本'),
                                (RuntimeError('SECRET /full/server/path'), '账本读取或校验失败')):
            with self.subTest(error=type(error).__name__), patch.object(self.store, 'get_job', side_effect=error):
                text = self.runner.end_text('report')
                self.assertIn(expected, text)
                self.assertNotIn('已确认删除', text)
                self.assertNotIn('8836', text)
                self.assertNotIn('SECRET', text)
                self.assertNotIn('/full/server/path', text)

    def test_early_failure_without_manifest_does_not_report_zero_candidates(self):
        with patch.object(self.runtime, 'load_artifact', side_effect=ValueError('missing')):
            self.runner.store = None
            self.runtime.update('report', status='failed')
            text = self.runner.end_text('report')
        self.assertIn('Meta违规素材清理｜失败', text)
        self.assertIn('候选广告：未取得产品统计', text)
        self.assertIn('未取得执行账本', text)

    def test_corrupt_final_snapshot_is_explicitly_unavailable(self):
        for objects in (None, [row('10'), row('10')], [{'kind': 'ad'}]):
            with self.subTest(objects=objects):
                self.store.objects = objects
                self.assertIn('账本读取或校验失败', self.runner.end_text('report'))

    def test_runtime_read_error_never_raises_or_exposes_exception(self):
        with patch.object(self.runtime, 'get', side_effect=RuntimeError('SECRET')):
            text = self.runner.end_text('report')
        self.assertIn('统计暂不可用', text)
        self.assertIn('未取得最终统计', text)
        self.assertNotIn('SECRET', text)

    def test_large_reference_details_stay_in_artifacts_and_old_notices_untouched(self):
        self.store.objects = [row('10', 'failed', failed()),
            row('11', 'failed', failed(200, 2490592)),
            row('12', 'failed', failed(200, 1487235)),
            row('13', 'failed', {'request_sent': True, 'code': 'rate_limited'}),
            row('14', 'failed', failed(10, 999))]
        refs = [{'ad_id': '9999999999' + str(i), 'product_ids': ['9999']} for i in range(20000)]
        self.manifest.update(shared_creative_impacts={'888888888888': refs},
                             shared_reference_coverage='partial', coverage={'errors': [{'stage': 'read'}]})
        self.runtime.artifact('report', 'manifest.json', self.manifest)
        self.runtime.artifact('report', 'deferred-cleanup.json', [{'basis': {'creative_id': '888888888888'},
            'blocked_ads': [{'ad_id': ref['ad_id'], 'result': {'reason': 'violating'}} for ref in refs]}])
        self.runtime.update('report', deferred_creatives=1)
        self.runtime.claim_notice('report', 'end', 'HISTORICAL BODY ' + str(refs))
        before_notice = self.runtime.notice('report', 'end')
        paths = list((pathlib.Path(self.temp.name) / 'runs' / 'report').glob('*.json'))
        before_files = {p: p.read_bytes() for p in paths}
        text = self.runner.end_text('report')
        self.assertLessEqual(len(text), 1200)
        self.assertLessEqual(len(text.splitlines()), 15)
        self.assertNotIn('待下轮复核Creative', text)
        for expected in ('扫描覆盖异常1项', '共享引用覆盖未完成', '共享Creative 1个'):
            self.assertIn(expected, text)
        self.assertNotIn('888888888888', text)
        self.assertNotIn('9999999999', text)
        self.assertNotIn('/mnt/', text)
        self.assertEqual(self.runtime.notice('report', 'end'), before_notice)
        self.assertTrue(before_files)
        self.assertEqual({p: p.read_bytes() for p in paths}, before_files)
        self.assertEqual(len(self.runtime.load_artifact('report', 'deferred-cleanup.json')[0]['blocked_ads']), 20000)
        self.notifier.assert_not_called()


if __name__ == '__main__':
    unittest.main()
