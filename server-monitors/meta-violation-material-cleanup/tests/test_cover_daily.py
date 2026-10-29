from contextlib import nullcontext
from datetime import datetime
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
        self.assertIn('尚无独立埋点', daily.report_body(data))

    def test_summary_nearest_rank_p95_and_empty(self):
        self.assertEqual(daily.summary([])['mean_seconds'], None)
        self.assertEqual(daily.summary(range(1, 101))['p95_seconds'], 95)

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
        self.assertIn('OnCalendar=*-*-* 09:30:00 Asia/Shanghai', timer)
        self.assertIn('Persistent=false', timer)
        self.assertNotIn('OnBootSec=', timer)
        self.assertNotIn('OnUnitActiveSec=', timer)


if __name__ == '__main__':
    unittest.main()
