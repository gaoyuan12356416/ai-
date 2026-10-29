from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import cover
from meta_violation_cleanup import cover_daily as daily
from meta_violation_cleanup.cover_store import CoverStore
from meta_violation_cleanup.runtime import Runtime, CST


class DailyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = CoverStore(self.root / 'cover.sqlite3')
        self.audit = Runtime(self.root / 'audit')
        self.addCleanup(self.audit.close)
        self.config = {'runtime_dir': str(self.root), 'feishu_config': 'fixture'}
        self.at = datetime(2026, 10, 29, 9, 30, tzinfo=CST)

    def event(self, key, action, at):
        with self.store._tx() as db:
            db.execute('INSERT INTO events(object_key,action,created_at,details) VALUES(?,?,?,?)',
                       (key, action, at, '{}'))

    def test_window_uses_beijing_schedule_before_at_and_after_cutoff(self):
        start, end = daily.window(self.at)
        self.assertEqual(start.isoformat(), '2026-10-28T09:30:00+08:00')
        self.assertEqual(end, self.at)
        self.assertEqual(daily.window(self.at.replace(hour=8))[1].day, 28)
        self.assertEqual(daily.window(self.at.replace(hour=15))[1], self.at)

    def test_half_open_window_deduplicates_and_ignores_old_batch_totals(self):
        self.event('task:1', 'unknown', '2026-10-28T01:30:00.000000+00:00')
        self.event('task:1', 'succeeded', '2026-10-29T01:29:59.000000+00:00')
        self.event('task:2', 'succeeded', '2026-10-28T01:29:59.999999+00:00')
        self.event('task:3', 'failed', '2026-10-29T01:30:00.000000+00:00')
        self.event('creative:4', 'deleted_verified', '2026-10-29T01:00:00.000000+00:00')
        data = daily.collect(self.store.path, *daily.window(self.at))
        self.assertEqual(data['tasks'], {'succeeded': 1})
        self.assertEqual(data['deletions'], {'deleted_verified': 1})

    def test_timing_spans_include_queue_and_reconciliation_without_inventing_api_time(self):
        self.store.begin_batch('old-batch', {})
        self.store.enqueue('old-batch', {'account_id': '100', 'ad_id': '200',
                                       'old_creative_id': '300', 'root_id': '1479',
                                       'material_ids': ['400'], 'user_id': '803'})
        with self.store._tx() as db:
            db.execute("UPDATE tasks SET created_at='2026-10-28T00:30:00.000000+00:00'")
        self.event('task:1', 'write_intent', '2026-10-28T02:30:00.000000+00:00')
        self.event('task:1', 'succeeded', '2026-10-28T02:30:10.000000+00:00')
        data = daily.collect(self.store.path, *daily.window(self.at))
        self.assertEqual(data['new_ads'], 0)
        self.assertEqual(data['timings']['enqueue_to_bind']['mean_seconds'], 7200)
        self.assertEqual(data['timings']['bind_verification']['mean_seconds'], 10)
        self.assertEqual(data['timings']['replacement_total']['mean_seconds'], 7210)
        self.assertIn('账本阶段历时', daily.report_body(data))
        self.assertNotIn('尚无独立埋点', daily.report_body(data))

    def test_summary_nearest_rank_p95_and_empty(self):
        self.assertEqual(daily.summary([])['mean_seconds'], None)
        self.assertEqual(daily.summary(range(1, 101))['p95_seconds'], 95)

    def test_worker_error_batches_are_not_scans(self):
        with self.store._tx() as db:
            for key, status in (('cover-scan-ok', 'sealed'), ('cover-scan-fail', 'failed'),
                                ('cover-error-interrupted', 'failed')):
                db.execute('INSERT INTO batches(id,status,created_at,sealed_at,meta) VALUES(?,?,?,?,?)',
                           (key, status, '2026-10-28T02:00:00.000000+00:00',
                            '2026-10-28T02:01:00.000000+00:00', '{"seconds":60}'))
        data = daily.collect(self.store.path, *daily.window(self.at))
        self.assertEqual(data['scan_completed'], 1)
        self.assertEqual(data['scan_failed'], 1)
        self.assertEqual(data['worker_errors'], 1)

    def test_daily_delivery_is_frozen_and_sent_once(self):
        audit, sent = self.audit, []
        class Sender:
            def __init__(self, *args): pass
            def send(self, run_id, phase, body):
                audit.claim_notice(run_id, phase, body)
                audit.start_notice(run_id, phase)
                audit.finish_notice(run_id, phase, 'sent', {'message_id': 'fixture'})
                sent.append(body)
                return {'message_id': 'fixture'}
        with patch.object(daily, 'process_lock', lambda *_: nullcontext()), \
             patch.object(daily, 'worker_state', lambda: {'ActiveState': 'active'}), \
             patch.object(daily, 'Notifier', Sender):
            daily.notify_daily(self.config, audit, self.at)
            self.event('task:99', 'failed', '2026-10-29T01:20:00.000000+00:00')
            result = daily.notify_daily(self.config, audit, self.at.replace(hour=10))
        self.assertEqual(len(sent), 1)
        self.assertEqual(result['delivery'], 'sent')
        self.assertIn('失败 0', sent[0])

    def test_unknown_notice_not_replayed(self):
        self.audit.begin('cover-daily-20261029', 'run', {})
        self.audit.claim_notice('cover-daily-20261029', 'end', 'frozen')
        self.audit.start_notice('cover-daily-20261029', 'end')
        self.audit.finish_notice('cover-daily-20261029', 'end', 'unknown', {})
        with patch.object(daily, 'process_lock', lambda *_: nullcontext()), patch.object(daily, 'Notifier') as sender:
            result = daily.notify_daily(self.config, self.audit, self.at)
        sender.assert_not_called()
        self.assertEqual(result['delivery'], 'unknown')

    def test_dry_run_and_worker_callbacks_do_not_send(self):
        with patch.object(daily, 'process_lock', lambda *_: nullcontext()), \
             patch.object(daily, 'worker_state', lambda: {}), patch.object(daily, 'Notifier') as sender:
            result = daily.notify_daily(self.config, self.audit, self.at, dry_run=True)
        sender.assert_not_called()
        self.assertEqual(result['delivery'], 'dry_run')
        self.assertEqual(self.audit.list(), [])
        with patch.object(cover, '_notify_completed') as old:
            cover.notify_completed(self.store, self.audit, self.config, True)
        old.assert_not_called()

    def test_timer_has_no_minute_or_boot_trigger(self):
        timer = (Path(cover.__file__).parent / 'deploy/meta-cover-notify.timer').read_text()
        self.assertIn('OnCalendar=*-*-* 01,05,09,13,17,21:30:00 Asia/Shanghai', timer)
        self.assertIn('Persistent=false', timer)
        self.assertNotIn('OnBootSec=', timer)
        self.assertNotIn('OnUnitActiveSec=', timer)

    def test_four_hour_window_is_anchored_in_beijing_and_handles_midnight(self):
        for hour in (1, 5, 9, 13, 17, 21):
            at = self.at.replace(hour=hour)
            start, end = daily.window(at, 4)
            self.assertEqual(end, at)
            self.assertEqual(end - start, timedelta(hours=24))
            self.assertEqual(daily.window(at - timedelta(microseconds=1), 4)[1], end - timedelta(hours=4))
            self.assertEqual(daily.window(at.astimezone(timezone.utc), 4), (start, end))
        start, end = daily.window(self.at.replace(hour=0), 4)
        self.assertEqual(end.isoformat(), '2026-10-28T21:30:00+08:00')
        self.assertEqual(start.isoformat(), '2026-10-27T21:30:00+08:00')
        self.assertEqual(daily.window(self.at, 4, 4)[0], self.at - timedelta(hours=4))

    def periodic_config(self):
        return dict(self.config, cover_report_hours=4, cover_report_lookback_hours=24,
                    cover_report_feishu_config='group-fixture',
                    cover_report_recipient={'receive_id_type': 'chat_id',
                                            'receive_id': 'oc_b633b6fba2c78f43ef1ebacd314d518c'})

    def test_periodic_send_freezes_window_destination_and_uses_dedicated_config(self):
        config, calls, audit = self.periodic_config(), [], self.audit
        class Sender:
            def __init__(self, runtime, path, destination):
                calls.append((path, destination))
            def send(self, run_id, phase, body):
                audit.claim_notice(run_id, phase, body)
                audit.start_notice(run_id, phase)
                audit.finish_notice(run_id, phase, 'sent', {'message_id': 'fixture'})
                return {'message_id': 'fixture'}
        with patch.object(daily, 'process_lock', lambda *_: nullcontext()), \
             patch.object(daily, 'worker_state', lambda: {}), patch.object(daily, 'Notifier', Sender):
            first = daily.notify_daily(config, audit, self.at)
            repeat = daily.notify_daily(config, audit, self.at + timedelta(hours=3))
            following = daily.notify_daily(config, audit, self.at + timedelta(hours=4))
        self.assertEqual(first['run_id'], 'cover-periodic-4h-20261029T0930')
        self.assertEqual(repeat['run_id'], first['run_id'])
        self.assertNotEqual(following['run_id'], first['run_id'])
        self.assertEqual(calls, [('group-fixture', config['cover_report_recipient'])] * 2)
        frozen = audit.load_artifact(first['run_id'], 'daily-summary.json')
        self.assertEqual(frozen['recipient'], config['cover_report_recipient'])
        self.assertEqual(frozen['data']['window_start'], '2026-10-28T09:30:00+08:00')
        self.assertIn('每4小时汇总', frozen['body'])
        self.assertIn('每4小时播报一次，汇总近24小时', frozen['body'])

    def test_periodic_unknown_is_not_replayed_and_daily_receipt_is_preserved(self):
        for run_id in ('cover-daily-20261029', 'cover-periodic-4h-20261029T0930'):
            self.audit.begin(run_id, 'run', {})
            self.audit.claim_notice(run_id, 'end', 'frozen')
            self.audit.start_notice(run_id, 'end')
            self.audit.finish_notice(run_id, 'end', 'unknown', {})
        with patch.object(daily, 'process_lock', lambda *_: nullcontext()), patch.object(daily, 'Notifier') as sender:
            result = daily.notify_daily(self.periodic_config(), self.audit, self.at)
        sender.assert_not_called()
        self.assertEqual(result['delivery'], 'unknown')
        self.assertEqual(self.audit.notice('cover-daily-20261029', 'end')['status'], 'unknown')

    def test_failed_frozen_period_cannot_be_rerouted(self):
        config = self.periodic_config()
        run_id = 'cover-periodic-4h-20261029T0930'
        self.audit.begin(run_id, 'run', {})
        self.audit.artifact(run_id, 'daily-summary.json', {'body': 'frozen', 'data': {},
                                                         'recipient': config['cover_report_recipient']})
        self.audit.claim_notice(run_id, 'end', 'frozen')
        self.audit.finish_notice(run_id, 'end', 'failed', {'request_sent': False})
        config['cover_report_recipient'] = {'receive_id_type': 'chat_id', 'receive_id': 'oc_different'}
        with patch.object(daily, 'process_lock', lambda *_: nullcontext()), patch.object(daily, 'Notifier') as sender:
            with self.assertRaisesRegex(ValueError, 'frozen_notification_recipient_changed'):
                daily.notify_daily(config, self.audit, self.at)
        sender.assert_not_called()

    def test_terminal_period_keeps_frozen_recipient_after_group_change_without_sending(self):
        config = self.periodic_config()
        original = dict(config['cover_report_recipient'])
        run_id = 'cover-periodic-4h-20261029T0930'
        self.audit.begin(run_id, 'run', {})
        self.audit.artifact(run_id, 'daily-summary.json', {'body': 'frozen', 'data': {},
                                                         'recipient': original})
        self.audit.claim_notice(run_id, 'end', 'frozen')
        self.audit.start_notice(run_id, 'end')
        self.audit.finish_notice(run_id, 'end', 'sent', {'message_id': 'old-group-message'})
        config['cover_report_recipient'] = {'receive_id_type': 'chat_id', 'receive_id': 'oc_newgroup'}
        with patch.object(daily, 'process_lock', lambda *_: nullcontext()), patch.object(daily, 'Notifier') as sender:
            result = daily.notify_daily(config, self.audit, self.at)
        sender.assert_not_called()
        self.assertEqual(result['delivery'], 'sent')
        self.assertEqual(result['recipient'], original)
        self.assertEqual(result['requested_recipient'], config['cover_report_recipient'])
        self.assertFalse(result['recipient_matches_requested'])

    def test_terminal_missing_artifact_does_not_infer_current_periodic_destination(self):
        run_id = 'cover-periodic-4h-20261029T0930'
        self.audit.begin(run_id, 'run', {})
        self.audit.claim_notice(run_id, 'end', 'frozen')
        self.audit.start_notice(run_id, 'end')
        self.audit.finish_notice(run_id, 'end', 'unknown', {})
        with patch.object(daily, 'process_lock', lambda *_: nullcontext()), patch.object(daily, 'Notifier') as sender:
            result = daily.notify_daily(self.periodic_config(), self.audit, self.at)
        sender.assert_not_called()
        self.assertIsNone(result['recipient'])
        self.assertFalse(result['recipient_matches_requested'])

    def test_invalid_interval_does_not_create_a_notice(self):
        for hours in (0, 3, 4.0, True, '4'):
            with self.subTest(hours=hours), self.assertRaisesRegex(ValueError, 'invalid_cover_report_hours'):
                daily.notify_daily(dict(self.config, cover_report_hours=hours), self.audit, self.at)
        self.assertEqual(self.audit.list(), [])
        for hours in (0, 3, 4.0, True, '24'):
            with self.subTest(lookback_hours=hours), self.assertRaisesRegex(ValueError, 'invalid_cover_report_lookback_hours'):
                daily.notify_daily(dict(self.config, cover_report_lookback_hours=hours), self.audit, self.at)
        self.assertEqual(self.audit.list(), [])

    def test_performance_is_weighted_and_attributed_by_completed_cycle(self):
        folder = self.root / 'performance'
        folder.mkdir()
        cycles = []
        for at, n, total, seconds, successes in (
                ('2026-10-29T05:30:00+08:00', 2, 20, 60, 8),
                ('2026-10-29T09:29:59+08:00', 3, 15, 40, 2),
                ('2026-10-29T09:30:00+08:00', 1, 999, 1, 99)):
            cycles.append({'at': at, 'started_at': '2026-10-29T01:00:00+08:00',
                           'result': {'seconds': seconds, 'tasks': {'succeeded': successes},
                                      'timings': {'source_prepare': {'count': n, 'total_seconds': total}}}})
        cycles.append({'at': '2026-10-29T08:00:00+08:00', 'result': {'seconds': 1000, 'tasks': {}}})
        (folder / '20261029.jsonl').write_text('\n'.join(json.dumps(x) for x in cycles) + '\n{partial', encoding='utf-8')
        result = daily.collect_performance(self.root, *daily.window(self.at, 4, 4))
        self.assertEqual(result['cycles'], 2)
        self.assertEqual(result['successes'], 10)
        self.assertEqual(result['success_per_hour'], 360)
        self.assertEqual(result['timings']['source_prepare'], {'n': 5, 'mean_seconds': 7})
        self.assertEqual(result['read_errors'], 1)
        data = daily.collect(self.store.path, *daily.window(self.at, 4, 4))
        data.update(report_hours=4, performance=result)
        body = daily.report_body(data)
        self.assertIn('周期可能跨窗', body)
        self.assertIn('360.00 条/小时', body)
        self.assertIn('样本可能不完整', body)


if __name__ == '__main__':
    unittest.main()
