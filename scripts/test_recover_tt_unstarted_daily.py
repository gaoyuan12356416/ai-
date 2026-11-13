import datetime as dt
from pathlib import Path
import unittest

from features.pipeline_dispatch.core import DispatchStore
from scripts import recover_tt_unstarted_daily as recovery
from scripts import test_pipeline_tt_selection_recovery as fixtures


class RecoveryTests(unittest.TestCase):
    def fixture(self):
        f = fixtures.TTSelectionRecoveryTests.fixture(self)
        central = Path(f.fixture.temp.name) / 'central.db'
        store = DispatchStore(central, f.fixture.clock)
        with f.bridge.connect() as c:
            c.execute("UPDATE tt_auto_task SET status='failed',error_code='tt_auto_metric_window_not_ready',scheduled_at_utc='2026-08-05T18:00:00+00:00'")
        store.offer(dict(f.adapter.project(f.adapter.read(f.task.id)), status='failed'))
        return f, store, central

    def plan(self, f, central):
        return recovery.make_plan(f.store.db_path, central, ['2026-08-05'], f.fixture.clock())

    def test_recovery_preserves_future_appointment_and_original_run_day(self):
        f, store, central = self.fixture()
        plan = self.plan(f, central)
        self.assertEqual(len(plan['items']), 1)
        with store.transaction() as c:
            c.execute("INSERT INTO pipeline_delivery VALUES('old-failure',?,0,'claim','request',?)", (plan['items'][0]['job_id'], recovery.dump({'accepted':True,'duplicate':True,'status':'failed'})))
        result = recovery.apply_plan(f.store.db_path, central, plan, plan['sha256'], Path(f.fixture.temp.name)/'backup', f.fixture.clock())
        self.assertEqual(result['applied'], 1)
        row = f.adapter.read(f.task.id)
        self.assertEqual(row['status'], 'pending')
        self.assertEqual(row['scheduled_at_utc'], '2026-08-05T18:00:00+00:00')
        with f.bridge.connect() as c:
            self.assertEqual(c.execute('SELECT shanghai_date FROM tt_auto_run').fetchone()[0], '2026-08-05')
            self.assertEqual(c.execute("SELECT count(*) FROM tt_auto_event WHERE event_type='authorized_unstarted_daily_recovery'").fetchone()[0], 1)
        self.assertEqual(store.get_job(plan['items'][0]['job_id'])['status'], 'queued')
        with store.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM pipeline_delivery').fetchone()[0], 1)
        with self.assertRaisesRegex(ValueError, 'already_recorded'):
            recovery.apply_plan(f.store.db_path, central, plan, plan['sha256'], Path(f.fixture.temp.name)/'backup-again', f.fixture.clock())

    def test_unknown_publish_attempt_binding_and_central_operation_are_excluded(self):
        for change in ('unknown_outcome=1', "publish_id='pub-id'", 'publish_attempt_count=1', "claim_token='active'", "material_id='123'"):
            with self.subTest(change=change):
                f, store, central = self.fixture()
                with f.bridge.connect() as c:
                    c.execute('UPDATE tt_auto_task SET '+change)
                self.assertEqual(self.plan(f, central)['items'], [])
        f, store, central = self.fixture()
        plan = self.plan(f, central); job = plan['items'][0]['job_id']
        with store.transaction() as c:
            c.execute("INSERT INTO pipeline_operation(operation_id,job_id,generation,node_id,operation,request_sha256,account_resource,created_at,updated_at) VALUES('op',?,1,'node','tt_initialize_video','hash','tt:account:640',?,?)", (job, store.now(), store.now()))
        self.assertEqual(self.plan(f, central)['items'], [])
        with self.assertRaisesRegex(ValueError, 'scope_changed'):
            recovery.apply_plan(f.store.db_path, central, plan, plan['sha256'], Path(f.fixture.temp.name)/'blocked-backup', f.fixture.clock())
        self.assertEqual(f.store.get_task(f.task.id).status, 'failed')

    def test_expired_plan_never_changes_task(self):
        f, store, central = self.fixture(); plan = self.plan(f, central)
        with self.assertRaisesRegex(ValueError, 'window_expired'):
            recovery.apply_plan(f.store.db_path, central, plan, plan['sha256'], Path(f.fixture.temp.name)/'expired', f.fixture.clock()+dt.timedelta(hours=3))
        self.assertEqual(f.store.get_task(f.task.id).status, 'failed')


if __name__ == '__main__':
    unittest.main()
