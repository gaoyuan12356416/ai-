import datetime as dt
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from scripts import publish_scheduler_health as health
from scripts.fb_short_link_health import collect_progress, summarize
from features.pipeline_dispatch.support_health import read_snapshot, LABELS

NOW = dt.datetime(2026, 11, 6, 4, tzinfo=dt.timezone.utc)


def row(key='pending', **values):
    return dict(dict(task_id=key, status='queued', unknown_outcome=0, held=0,
                     planned_at_utc=(NOW - dt.timedelta(hours=2)).isoformat(),
                     prepare_state='ready', link_state='pending'), **values)


class ProgressTests(unittest.TestCase):
    def test_stall_requires_continuous_observation_and_clears_on_ready(self):
        value = None
        for seconds in range(0, 301, 60):
            value = summarize([row()], NOW.timestamp() + seconds, value)
            self.assertEqual(value['overdue_pending'], int(seconds >= 300))
        value = summarize([row(link_state='ready')], NOW.timestamp() + 360, value)
        self.assertTrue(value['ok'])
        self.assertEqual(value['due_ready'], 1)
        self.assertEqual(value['pending_since'], {})

    def test_future_platform_unknown_holds_and_rendering_are_not_link_stalls(self):
        rows = [row('future', planned_at_utc=(NOW + dt.timedelta(days=2)).isoformat()),
                row('platform', status='submitted'), row('unknown', status='unknown'),
                row('held', held=1), row('render', prepare_state='preparing'),
                row('running', status='running'), row('bad_schedule', planned_at_utc='')]
        result = summarize(rows, NOW.timestamp())
        self.assertEqual(result['due_pending'], 0)
        for key in ('future_scheduled','platform_processing','outcome_unknown','held',
                    'preparing','executing','schedule_unknown'):
            self.assertEqual(result[key], 1)

    def test_observation_gap_or_clock_reversal_does_not_fabricate_elapsed_time(self):
        first = summarize([row()], NOW.timestamp())
        for delta in (200, -60):
            value = summarize([row()], NOW.timestamp() + delta, first)
            self.assertEqual(value['oldest_pending_seconds'], 0)

    def test_missing_database_is_unknown_and_not_created(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'missing.db'
            self.assertFalse(collect_progress(path)['available'])
            self.assertFalse(path.exists())

    def test_database_read_preserves_all_task_and_hold_facts(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'tasks.db'
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE fb_video_task(task_id TEXT,status TEXT,unknown_outcome INTEGER,planned_at_utc TEXT,prepare_state TEXT,link_state TEXT)')
                db.execute('CREATE TABLE pipeline_canonical_hold(source_key TEXT)')
                db.execute('INSERT INTO fb_video_task VALUES(?,?,?,?,?,?)', ('held','queued',0,NOW.isoformat(),'ready','pending'))
                db.execute("INSERT INTO pipeline_canonical_hold VALUES('fb:video:held')")
            db.close()
            before = path.read_bytes()
            result = collect_progress(path, NOW.timestamp())
            self.assertEqual(result['held'], 1)
            self.assertEqual(result['due_pending'], 0)
            self.assertEqual(before, path.read_bytes())


class RestoreTests(unittest.TestCase):
    def baseline(self):
        specs = {'main.service': {'kind': 'daemon', 'lane': 'fb'},
                 'worker.service': {'kind': 'daemon', 'lane': 'fb'},
                 'paused.service': {'kind': 'daemon', 'lane': 'fb'},
                 'job.service': {'kind': 'oneshot', 'lane': 'fb'},
                 'job.timer': {'kind': 'timer', 'lane': 'fb'}}
        states = {u: self.running() for u in specs}
        states['paused.service']['ActiveState'] = 'inactive'
        states['job.service']['ActiveState'] = 'activating'
        return dict(lanes=['fb'], specs=specs, states=states)

    def running(self):
        return dict(LoadState='loaded', ActiveState='active', SubState='running', MainPID='42')

    def test_restore_only_missing_previously_active_daemon_not_paused_or_oneshot(self):
        before = self.baseline()
        current = {u: self.running() for u in before['specs']}
        current['worker.service'] = dict(self.running(), ActiveState='inactive', MainPID='0')
        with patch.object(health, 'show', side_effect=[current, {u: self.running() for u in current}]), patch.object(health.subprocess, 'run') as run:
            run.return_value.returncode = 0
            result = health.restore_snapshot(before, 0)
        self.assertTrue(result['ok'])
        self.assertEqual(result['started'], ['worker.service'])
        self.assertEqual(run.call_args.args[0], ['systemctl', '--job-mode=ignore-dependencies', 'start', 'worker.service'])

    def test_complete_discovery_finds_extra_daemon_and_timer(self):
        def show(units):
            result = {u: dict(self.running(), Type='oneshot' if u.endswith('-plan.service') else 'simple') for u in units}
            for u in units:
                if u == 'fb-auto-post-service.service':
                    result[u]['RequiredBy'] = 'extra.service old-publish.service'
                if u == 'old-publish.service':
                    result[u].update(Type='oneshot', TriggeredBy='old-publish.timer')
            return result
        with patch.object(health, 'show', side_effect=show):
            baseline = health.deployment_snapshot(['fb'])
        selected = health.restore_targets(baseline)
        self.assertIn('extra.service', selected)
        self.assertIn('old-publish.timer', selected)
        self.assertNotIn('old-publish.service', selected)

    def test_forward_failure_and_success_both_restore_and_record(self):
        for fail in (False, True):
            with tempfile.TemporaryDirectory() as folder, patch.object(health, 'deployment_snapshot', return_value=self.baseline()), patch.object(health, 'restore_snapshot', return_value={'ok': True}) as restore:
                try:
                    with health.deployment_guard(['fb'], folder):
                        if fail:
                            raise RuntimeError('deployment_failed_fixture')
                except RuntimeError as exc:
                    self.assertEqual(str(exc), 'deployment_failed_fixture')
                restore.assert_called_once()
                self.assertEqual(json.loads((Path(folder) / 'support-restored.json').read_text())['deployment_failed'], fail)

    def test_restore_failure_is_reported_without_masking_original_error(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(health, 'deployment_snapshot', return_value=self.baseline()), patch.object(health, 'restore_snapshot', return_value={'ok': False}):
            with self.assertRaisesRegex(ValueError, 'original'):
                with health.deployment_guard(['fb'], folder):
                    raise ValueError('original')
            self.assertFalse(json.loads((Path(folder) / 'support-restored.json').read_text())['ok'])

    def test_fb_forward_and_rollback_entrypoint_use_guard(self):
        from scripts import deploy_publish_queue_isolation as deploy
        with tempfile.TemporaryDirectory() as folder, patch.object(health, 'deployment_snapshot', return_value=self.baseline()), patch.object(health, 'restore_snapshot', return_value={'ok': True}) as restore, patch.object(deploy, '_switch', side_effect=RuntimeError('switch_failed')):
            for rollback in (False, True):
                with self.assertRaisesRegex(RuntimeError, 'switch_failed'):
                    deploy.switch(Path(folder), 'fb', rollback)
            self.assertEqual(restore.call_count, 2)


class SnapshotTests(unittest.TestCase):
    def snapshot(self):
        return dict(at_utc=NOW.isoformat(), checks=[dict(unit=u,lane='fb',ok=True) for u in LABELS],
                    fb_short_links=summarize([row()], NOW.timestamp()), states={'secret': 'must_not_expose'})

    def read(self, value, now=NOW):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'health.json'
            path.write_text(json.dumps(value), encoding='utf-8')
            return read_snapshot(now, {'is_admin': True}, ['fb'], path)

    def test_nonadmin_or_channel_filter_does_not_read_global_snapshot(self):
        with patch.object(Path, 'read_text', side_effect=AssertionError('must not read')):
            self.assertIsNone(read_snapshot(NOW, {'is_admin': False}, ['fb']))
            self.assertIsNone(read_snapshot(NOW, {'is_admin': True}, ['x']))

    def test_public_snapshot_has_no_internal_ids_or_system_state(self):
        value = self.read(self.snapshot())
        self.assertTrue(value['ok'])
        self.assertEqual(value['progress']['due_pending'], 1)
        self.assertNotIn('pending_since', json.dumps(value))
        self.assertNotIn('must_not_expose', json.dumps(value))

    def test_stale_corrupt_and_missing_progress_are_unknown(self):
        self.assertEqual(self.read(self.snapshot(), NOW + dt.timedelta(seconds=151))['code'], 'snapshot_stale')
        self.assertFalse(self.read({})['available'])

    def test_stopped_worker_is_an_alert_and_future_jobs_stay_separate(self):
        data = self.snapshot()
        data['checks'][1]['ok'] = False
        data['fb_short_links'] = summarize([row(planned_at_utc=(NOW + dt.timedelta(days=2)).isoformat())], NOW.timestamp())
        value = self.read(data)
        self.assertFalse(value['ok'])
        self.assertEqual(value['progress']['future_scheduled'], 1)
        self.assertEqual(value['progress']['overdue_pending'], 0)
        self.assertIn('短链', value['alerts'][0])


if __name__ == '__main__':
    unittest.main()
