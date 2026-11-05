"""Read-only status partition, ACL, future scheduling and anomaly regressions."""
import contextlib
import datetime as dt
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.pipeline_dispatch import views, queue_states
from features.pipeline_dispatch.core import DispatchStore, stamp


class QueueStatesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = dt.datetime(2026, 11, 4, 2, tzinfo=dt.timezone.utc)
        self.store = DispatchStore(Path(self.temp.name) / 'dispatch.sqlite3', clock=lambda: self.now)
        self.actor = {'is_admin': False, 'user_id': 'alice', 'tenant_key': 't',
                      'allowed_channels': ['x'], 'allowed_modules': ['x_accounts']}
        self.store.source_health('x', 'manual')
        self.store.register_node('hk-gpu', ['prepare'], {})

    def tearDown(self):
        views.invalidate_cache(self.store)
        self.temp.cleanup()

    def job(self, key, status='queued', **changes):
        row = self.store.offer({'source_key': key, 'channel': 'x', 'stage': 'prepare',
            'family': 'manual', 'owner_user_id': 'alice', 'owner_tenant_key': 't',
            'required_module': 'x_accounts', 'resource_keys': ['account:' + key],
            'execution_available': True, 'status': 'queued'})
        with self.store.transaction() as c:
            for name, value in dict(status=status, **changes).items():
                c.execute('UPDATE pipeline_job SET ' + name + '=? WHERE id=?', (value, row['id']))
        return row['id']

    def summary(self):
        views.invalidate_cache(self.store)
        return views.query(self.store, 'summary', self.actor, {})

    def test_partition_due_future_ready_running_failed_unknown_and_paused(self):
        self.job('due', scheduled_at=stamp(self.now - dt.timedelta(hours=1)))
        self.job('ready-due', 'ready')
        self.job('future', scheduled_at=stamp(self.now + dt.timedelta(seconds=1)))
        self.job('future-ready', 'ready', scheduled_at=stamp(self.now + dt.timedelta(hours=1)))
        self.job('running', 'running')
        self.job('legacy', 'legacy_running')
        self.job('held', hold_reason='adapter_unavailable')
        self.job('failed', 'failed', finished_at=stamp(self.now))
        self.job('old-failed', 'failed', finished_at=stamp(self.now - dt.timedelta(days=2)))
        self.job('unknown', 'unknown')
        self.job('paused', 'backlog_paused')
        self.job('metadata', hold_reason='metadata_only')
        result = self.summary()
        self.assertEqual(result['queue_state_version'], 1)
        self.assertEqual({k: result['totals'][k] for k in queue_states.KEYS},
                         dict(waiting=2, executing=2, scheduled=2, abnormal=3))
        row = result['channels'][0]
        self.assertEqual(row['oldest_pending_seconds'], 3600)
        self.assertEqual(row['abnormal_reasons'], dict(held=1, failed=1, unknown=1))
        self.assertEqual(result['totals']['backlog_paused'], 1)

    def test_unknown_account_blocker_does_not_leak_other_owner(self):
        blocker = self.job('secret-bob-key', 'unknown', owner_user_id='bob')
        for key in ('due', 'future'):
            self.job(key, resource_keys_json='["shared-account"]',
                     scheduled_at=stamp(self.now + dt.timedelta(hours=1 if key == 'future' else 0)))
        with self.store.transaction() as c:
            c.execute('INSERT INTO pipeline_resource VALUES (?,?,1,1,0)', ('shared-account', blocker))
        result = self.summary()
        self.assertEqual(result['totals']['abnormal'], 1)
        self.assertEqual(result['totals']['scheduled'], 1)
        self.assertEqual(result['totals']['unknown'], 0)
        self.assertNotIn('secret-bob-key', json.dumps(result))
        self.assertNotIn('shared-account', json.dumps(result))

    def test_all_windows_channels_and_stages_exclude_refreshed_history(self):
        self.actor.update(is_admin=True, allowed_channels=['x', 'fb', 'tt', 'youtube'])
        times = [self.now - dt.timedelta(days=2), self.now - dt.timedelta(hours=12),
                 self.now - dt.timedelta(hours=2), self.now - dt.timedelta(minutes=15)]
        for channel in self.actor['allowed_channels']:
            for stage in ('prepare', 'publish'):
                for index, occurred in enumerate(times):
                    for status in ('failed', 'unknown', 'queued'):
                        self.job(f'{channel}-{stage}-{status}-{index}', status,
                                 channel=channel, stage=stage, scheduled_at=stamp(occurred),
                                 source_created_at=stamp(occurred), updated_at=stamp(self.now),
                                 finished_at=stamp(occurred) if status == 'failed' else '',
                                 hold_reason='adapter_unavailable' if status == 'queued' else '')
        for window, expected in [('hour', 3), ('today', 6), ('day', 9), ('week', 12)]:
            result = views.query(self.store, 'summary', self.actor, {'window': window})
            self.assertEqual(result['totals']['abnormal'], expected * 8)
            for row in result['channels']:
                self.assertEqual(row['abnormal'], expected)
                self.assertEqual(sum(row['abnormal_reasons'].values()), expected)
        scoped = views.query(self.store, 'summary', self.actor, {'window': 'hour', 'channel': 'fb'})
        self.assertEqual(scoped['totals']['abnormal'], 6)
        self.assertEqual({r['channel'] for r in scoped['channels']}, {'fb'})

    def test_beijing_midnight_and_inclusive_window_boundaries(self):
        start, end = views._window({'window': 'today'}, self.now)
        midnight = dt.datetime(2026, 11, 3, 16, tzinfo=dt.timezone.utc)
        self.assertEqual(start, stamp(midnight))
        for name, occurred in [('before', midnight-dt.timedelta(milliseconds=1)),
                               ('start', midnight), ('end', self.now),
                               ('after', self.now+dt.timedelta(milliseconds=1))]:
            self.job(name, 'failed', finished_at=stamp(occurred))
        self.assertEqual(self.summary()['totals']['abnormal'], 2)

    def test_original_time_fallback_and_confirmed_or_closed_failures(self):
        old = stamp(self.now-dt.timedelta(days=2))
        current = stamp(self.now-dt.timedelta(minutes=10))
        self.job('recent-result', 'failed', finished_at=current, started_at=old, scheduled_at=old)
        self.job('recent-start', 'unknown', started_at=current, scheduled_at=old)
        self.job('original-created', scheduled_at='', source_created_at=current, hold_reason='held')
        self.job('no-time', scheduled_at='', source_created_at='', hold_reason='held')
        self.job('closed', 'failed', finished_at=current, source_status='cancelled')
        failed = self.job('confirmed', 'failed', stage='publish', finished_at=current)
        unknown = self.job('comment-unknown', 'unknown', stage='publish', started_at=current)
        for job, key in [(failed, 'confirmed'), (unknown, 'comment-unknown')]:
            self.store.record_publication(job, {'source_key': key, 'publish_id': 'post-'+key,
                                               'published_at': current}, canonical_verified=True)
        result = self.summary()
        self.assertEqual(result['totals']['abnormal'], 4)
        self.assertEqual(result['channels'][1]['abnormal_reasons'], {'unknown': 1})

    def test_normal_account_lock_is_pending_but_legacy_unknown_is_abnormal(self):
        blocker = self.job('other', 'running', owner_user_id='bob')
        self.job('due', resource_keys_json='["shared"]')
        with self.store.transaction() as c:
            c.execute('INSERT INTO pipeline_resource VALUES (?,?,1,0,0)', ('shared', blocker))
        self.assertEqual(self.summary()['totals']['waiting'], 1)
        with self.store.transaction() as c:
            c.execute('DELETE FROM pipeline_resource')
            c.execute("UPDATE pipeline_job SET status='unknown' WHERE id=?", (blocker,))
            c.execute('INSERT INTO pipeline_legacy_resource VALUES (?,?)', ('shared', blocker))
        self.assertEqual(self.summary()['totals']['abnormal'], 1)

    def stalled(self, phase='idle', generation=10):
        job = self.job('stalled', 'running', node_id='hk-gpu', worker_id='prepare-1',
                       generation=generation, lease_expires_at=stamp(self.now + dt.timedelta(seconds=90)))
        self.store.worker_heartbeat('hk-gpu', 'prepare-1', 'prepare', observation={'phase': phase})
        with self.store.transaction() as c:
            for minutes in (2, 4, 6):
                c.execute('INSERT INTO pipeline_event(job_id,event_type,details_json,created_at) VALUES (?,?,?,?)',
                          (job, 'lease_expired', '{}', stamp(self.now - dt.timedelta(minutes=minutes))))
        return job

    def test_repeated_idle_claims_are_abnormal_but_active_render_is_not(self):
        self.stalled()
        self.assertEqual(self.summary()['channels'][0]['abnormal_reasons'], {'repeated_claim': 1})
        self.store.worker_heartbeat('hk-gpu', 'prepare-1', 'prepare', observation={'phase': 'render'})
        self.assertEqual(self.summary()['totals']['executing'], 1)

    def test_stale_worker_requires_expired_lease(self):
        job = self.stalled(phase='render')
        with self.store.transaction() as c:
            c.execute('UPDATE pipeline_worker SET last_seen_at=?', (stamp(self.now - dt.timedelta(minutes=3)),))
        self.assertEqual(self.summary()['totals']['executing'], 1)
        with self.store.transaction() as c:
            c.execute('UPDATE pipeline_job SET lease_expires_at=? WHERE id=?', (stamp(self.now - dt.timedelta(seconds=1)), job))
        self.assertEqual(self.summary()['channels'][0]['abnormal_reasons'], {'worker_offline': 1})

    def test_old_expirations_do_not_mark_new_claim_as_abnormal(self):
        self.stalled()
        with self.store.transaction() as c:
            c.execute("UPDATE pipeline_event SET created_at=? WHERE event_type='lease_expired'", (stamp(self.now - dt.timedelta(hours=2)),))
        self.assertEqual(self.summary()['totals']['executing'], 1)

    def test_ownership_and_missing_identity_schedule(self):
        self.job('other-user', owner_user_id='bob')
        self.job('other-tenant', owner_tenant_key='other')
        self.job('missing-identity', resource_keys_json='[]')
        self.job('missing-schedule', scheduled_at='')
        result = self.summary()
        self.assertEqual(result['totals']['waiting'], 0)
        self.assertEqual(result['totals']['abnormal'], 2)

    def test_summary_is_read_only_and_does_not_make_platform_calls(self):
        self.stalled()
        with contextlib.closing(self.store.connect()) as c:
            before = list(c.iterdump())
        with patch('socket.socket', side_effect=AssertionError('no platform network')):
            self.summary()
        with contextlib.closing(self.store.connect()) as c:
            self.assertEqual(before, list(c.iterdump()))

    def test_indexed_active_query_and_overflow_is_unknown(self):
        for n in range(3): self.job(str(n))
        with contextlib.closing(self.store.connect()) as c:
            plans = c.execute("EXPLAIN QUERY PLAN SELECT id FROM pipeline_job j WHERE channel IN ('x','fb','tt','youtube') AND status IN ('queued','ready','running','legacy_running') LIMIT 10001").fetchall()
        self.assertTrue(any('SEARCH' in r[3] and 'INDEX' in r[3] for r in plans))
        with patch.object(queue_states, 'LIMIT', 2):
            result = self.summary()
        self.assertIsNone(result['totals']['waiting'])
        self.assertIsNone(result['totals']['abnormal'])
        self.assertTrue(any('超过读取上限' in w for w in result['warnings']))


if __name__ == '__main__': unittest.main()
