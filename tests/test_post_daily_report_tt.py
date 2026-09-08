import json
import importlib.util
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from features.post_daily_report.tt import collect


class TTReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.paths = {key: str(Path(self.tmp.name) / (key + '.sqlite')) for key in ('tt', 'tt_auto')}
        self.window = SimpleNamespace(date='2026-09-07', start=datetime(2026, 9, 6, 16, tzinfo=timezone.utc),
                                      end=datetime(2026, 9, 7, 16, tzinfo=timezone.utc),
                                      cutoff=datetime(2026, 9, 8, 2, tzinfo=timezone.utc))
        self.auto = sqlite3.connect(self.paths['tt_auto'])
        self.legacy = sqlite3.connect(self.paths['tt'])
        self.auto.executescript('''
          CREATE TABLE tt_auto_template(id INTEGER,current_version INTEGER,enabled INTEGER,enabled_at_utc TEXT,updated_at TEXT);
          CREATE TABLE tt_auto_template_version(template_id INTEGER,version INTEGER,config_json TEXT,config_sha256 TEXT);
          CREATE TABLE tt_auto_event(id INTEGER,event_type TEXT,details_json TEXT,created_at TEXT);
          CREATE TABLE tt_auto_random_plan(template_id INTEGER,template_version INTEGER,shanghai_date TEXT,publish_times_json TEXT);
          CREATE TABLE tt_auto_run(id INTEGER,template_id INTEGER,template_version INTEGER,trigger_type TEXT,shanghai_date TEXT,scheduled_at_utc TEXT);
          CREATE TABLE tt_auto_task(id INTEGER,run_id INTEGER,template_id INTEGER,template_version INTEGER,account_id TEXT,status TEXT,scheduled_at_utc TEXT,published_at_utc TEXT,publish_id TEXT,error_code TEXT,error_message TEXT,unknown_outcome INTEGER);
        ''')
        self.legacy.executescript('''
          CREATE TABLE tt_post_daily_schedule(account_id TEXT,enabled INTEGER,version INTEGER,schedule_mode TEXT,publish_times_json TEXT,updated_at TEXT);
          CREATE TABLE tt_post_random_daily_plan(account_id TEXT,shanghai_date TEXT,config_version INTEGER,publish_times_json TEXT);
          CREATE TABLE tt_post_schedule_run(id INTEGER,queue_id INTEGER,trigger_type TEXT,account_id TEXT,shanghai_date TEXT,scheduled_at_utc TEXT,status TEXT,error_code TEXT,error_message TEXT);
          CREATE TABLE tt_post_queue(id INTEGER,account_id TEXT,scheduled_at_utc TEXT,status TEXT,publish_id TEXT,error_code TEXT,error_message TEXT,unknown_outcome INTEGER);
          CREATE TABLE tt_post_event(id INTEGER,queue_id INTEGER,event_type TEXT,to_status TEXT,created_at TEXT);
          CREATE TABLE tt_post_direct_test(id INTEGER,account_id TEXT,created_at TEXT,published_at_utc TEXT,status TEXT,publish_id TEXT,error_code TEXT,error_message TEXT,unknown_outcome INTEGER);
          CREATE TABLE tt_post_recurring_pool(id INTEGER,account_id TEXT,status TEXT,routing_language TEXT);
          CREATE TABLE tt_post_account_setting(account_id TEXT,drama_language TEXT);
        ''')

    def tearDown(self):
        self.auto.close()
        self.legacy.close()
        self.tmp.cleanup()

    def report(self):
        self.auto.commit()
        self.legacy.commit()
        return collect(self.paths, self.window)

    def row(self, source):
        return next(r for r in self.report()['rows'] if r['source'] == source)

    def template(self, count=2):
        cfg = {'account_ids': ['a'], 'schedule': {'mode': 'random', 'daily_count': count}, 'video_template': 'random_overlay'}
        self.auto.execute('INSERT INTO tt_auto_template VALUES(1,1,1,?,?)', ('2026-08-01T00:00:00Z', '2026-08-01T00:00:00Z'))
        self.auto.execute('INSERT INTO tt_auto_template_version VALUES(1,1,?,?)', (json.dumps(cfg), 'frozen'))
        self.auto.execute('INSERT INTO tt_auto_random_plan VALUES(1,1,?,?)', ('2026-09-07', '["01:00","03:00"]'))

    def test_random_plan_counts_without_run_or_queue(self):
        self.template()
        self.legacy.execute('INSERT INTO tt_post_daily_schedule VALUES(?,1,1,?,?,?)', ('b', 'random', '[]', '2026-08-01T00:00:00Z'))
        self.legacy.execute('INSERT INTO tt_post_random_daily_plan VALUES(?,?,1,?)', ('b', '2026-09-07', '["01:00","03:00"]'))
        self.assertEqual(self.row('auto_template:random_overlay')['expected'], 2)
        row = self.row('manual_material_schedule')
        self.assertEqual((row['expected'], row['published'], row['pending']), (2, 0, 2))
        self.assertEqual(row['reasons'][0]['confidence'], 'inferred')

    def test_late_unknown_and_prior_completions_use_beijing_boundaries(self):
        self.template()
        self.auto.executemany('INSERT INTO tt_auto_run VALUES(?,1,1,?,?,?)', [(1,'auto','2026-09-07','2026-09-06T17:00:00Z'),(2,'auto','2026-09-07','2026-09-06T19:00:00Z')])
        self.auto.executemany('INSERT INTO tt_auto_task VALUES(?,?,1,1,?,?,?,?,?,?,?,?)', [
            (1,1,'a','published','2026-09-06T17:00:00Z','2026-09-07T16:00:00Z','p1','','',0),
            (2,2,'a','reconciling','2026-09-06T19:00:00Z','','p2','','',0),
            (3,0,'a','published','2026-09-06T15:59:00Z','2026-09-06T16:00:00Z','p3','','',0)])
        row = self.row('auto_template:random_overlay')
        self.assertEqual((row['expected'], row['published'], row['late'], row['unknown']), (2,0,1,1))
        self.assertEqual(self.report()['prior_completed'], 1)

    def test_preflight_is_counted_without_queue_and_manual_run_not_double_counted(self):
        self.legacy.execute('INSERT INTO tt_post_daily_schedule VALUES(?,1,1,?,?,?)', ('b','fixed','["01:00"]','2026-08-01T00:00:00Z'))
        self.legacy.executemany('INSERT INTO tt_post_schedule_run VALUES(?,?,?,?,?,?,?,?,?)', [
            (1,None,'auto','b','2026-09-07','2026-09-06T17:00:00Z','preflight_failed','media_invalid','bad video'),
            (2,7,'manual','c','2026-09-07','2026-09-06T18:00:00Z','published','','')])
        self.legacy.execute('INSERT INTO tt_post_queue VALUES(7,?,?,?,?,?,?,?)', ('c','2026-09-06T18:00:00Z','published','pub','','',0))
        self.legacy.execute('INSERT INTO tt_post_event VALUES(1,7,?,?,?)', ('publish_reconciled','published','2026-09-06T18:01:00Z'))
        self.assertEqual(self.row('manual_material_schedule')['failed'],1)
        manual = self.row('manual_material_once')
        self.assertEqual((manual['expected'],manual['published']), (1,1))

    def test_pending_uses_older_banned_account_blocker(self):
        self.template()
        self.auto.execute('INSERT INTO tt_auto_run VALUES(1,1,1,?,?,?)', ('auto','2026-09-07','2026-09-06T17:00:00Z'))
        self.auto.executemany('INSERT INTO tt_auto_task VALUES(?,?,1,1,?,?,?,?,?,?,?,?)', [
            (1,1,'a','pending','2026-09-06T17:00:00Z','','','','',0),
            (2,0,'a','retry_wait','2026-09-05T17:00:00Z','','','tt_upstream_rejected','spam_risk_user_banned_from_posting token=never-echo-me',0)])
        row = self.row('auto_template:random_overlay')
        reason = next(r for r in row['reasons'] if r['code'] == 'account_posting_banned')
        self.assertEqual(reason['confidence'], 'inferred')
        self.assertNotIn('never-echo-me', json.dumps(self.report()))

    def test_template_disabled_midday_uses_audit_not_current_flag(self):
        self.template()
        self.auto.execute("UPDATE tt_auto_template SET enabled=0,enabled_at_utc='',updated_at='2026-09-06T18:00:00Z'")
        self.auto.executemany('INSERT INTO tt_auto_event VALUES(?,?,?,?)', [
            (1,'template_enabled','{"template_id":1,"version":1}','2026-08-01T00:00:00Z'),
            (2,'template_disabled','{"template_id":1,"version":1}','2026-09-06T18:00:00Z')])
        self.assertEqual(self.row('auto_template:random_overlay')['expected'],1)

    def test_legacy_changed_without_history_is_unknown(self):
        self.legacy.execute('INSERT INTO tt_post_daily_schedule VALUES(?,0,2,?,?,?)', ('b','fixed','["01:00"]','2026-09-07T01:00:00Z'))
        self.assertIsNone(self.row('manual_material_schedule')['expected'])

    def test_persisted_random_plan_respects_audited_disable(self):
        self.legacy.execute('INSERT INTO tt_post_daily_schedule VALUES(?,0,2,?,?,?)', ('b','random','[]','2026-09-06T18:00:00Z'))
        self.legacy.execute('INSERT INTO tt_post_random_daily_plan VALUES(?,?,1,?)', ('b','2026-09-07','["01:00","03:00"]'))
        self.legacy.execute('CREATE TABLE tt_post_daily_schedule_audit(id INTEGER,account_id TEXT,created_at TEXT,snapshot_json TEXT)')
        for index, (stamp, enabled, version) in enumerate([('2026-08-01T00:00:00Z',1,1),('2026-09-06T18:00:00Z',0,2)]):
            snapshot = {'account_id':'b','enabled':enabled,'version':version,'schedule_mode':'random','publish_times_json':'[]'}
            self.legacy.execute('INSERT INTO tt_post_daily_schedule_audit VALUES(?,?,?,?)', (index,'b',stamp,json.dumps(snapshot)))
        self.assertEqual(self.row('manual_material_schedule')['expected'],1)

    def test_first_audit_baseline_midday_does_not_invent_prior_history(self):
        self.legacy.execute('INSERT INTO tt_post_daily_schedule VALUES(?,1,1,?,?,?)', ('b','fixed','["01:00","03:00"]','2026-08-01T00:00:00Z'))
        self.legacy.execute('CREATE TABLE tt_post_daily_schedule_audit(id INTEGER,account_id TEXT,created_at TEXT,snapshot_json TEXT)')
        snapshot = {'account_id':'b','enabled':1,'version':1,'schedule_mode':'fixed','publish_times_json':'["01:00","03:00"]'}
        self.legacy.execute('INSERT INTO tt_post_daily_schedule_audit VALUES(1,?,?,?)', ('b','2026-09-06T18:00:00Z',json.dumps(snapshot)))
        self.assertIsNone(self.row('manual_material_schedule')['expected'])

    def test_audit_installed_today_preserves_yesterdays_persisted_plan(self):
        self.legacy.execute('INSERT INTO tt_post_daily_schedule VALUES(?,1,1,?,?,?)', ('b','random','[]','2026-08-01T00:00:00Z'))
        self.legacy.execute('INSERT INTO tt_post_random_daily_plan VALUES(?,?,1,?)', ('b','2026-09-07','["01:00","03:00"]'))
        before = self.row('manual_material_schedule')['expected']
        self.legacy.execute('CREATE TABLE tt_post_daily_schedule_audit(id INTEGER,account_id TEXT,created_at TEXT,snapshot_json TEXT)')
        snapshot = {'account_id':'b','enabled':1,'version':1,'schedule_mode':'random','publish_times_json':'[]'}
        self.legacy.execute('INSERT INTO tt_post_daily_schedule_audit VALUES(1,?,?,?)', ('b','2026-09-08T03:00:00Z',json.dumps(snapshot)))
        self.assertEqual(before, 2)
        self.assertEqual(self.row('manual_material_schedule')['expected'], before)

    def test_unavailable_database_does_not_become_zero(self):
        self.paths['tt'] = str(Path(self.tmp.name) / 'absent.sqlite')
        report = self.report()
        row = next(r for r in report['rows'] if r['source'] == 'unavailable:tt')
        self.assertIsNone(row['expected'])
        self.assertTrue(report['warnings'])
        self.assertFalse(Path(self.paths['tt']).exists())

    def test_audit_hook_preserves_existing_publish_config_and_logs_changes(self):
        # Load only the store and relative stdlib helpers, never package service
        # initialization (which depends on unrelated deployment-only modules).
        root = Path(__file__).resolve().parents[1] / 'features' / 'tt_posts'
        package = ModuleType('_tt_report_hook_test')
        package.__path__ = [str(root)]
        with patch.dict(sys.modules, {'_tt_report_hook_test': package}):
            spec = importlib.util.spec_from_file_location('_tt_report_hook_test.core', root / 'core.py')
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            TTPostStore = module.TTPostStore
        path = str(Path(self.tmp.name) / 'full.sqlite')
        TTPostStore(path)
        conn = sqlite3.connect(path)
        try:
            conn.execute("INSERT INTO tt_post_daily_schedule(account_id,enabled,version,user_consent,consent_version,consented_at_utc,created_at,updated_at) VALUES('a',0,1,1,'v1','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')")
            conn.execute("UPDATE tt_post_daily_schedule SET enabled=1,version=2 WHERE account_id='a'")
            conn.commit()
            snapshots = [json.loads(r[0]) for r in conn.execute('SELECT snapshot_json FROM tt_post_daily_schedule_audit ORDER BY id')]
            self.assertEqual([s['enabled'] for s in snapshots], [0,1])
            self.assertEqual([s['version'] for s in snapshots], [1,2])
        finally:
            conn.close()
        TTPostStore(path)
        conn = sqlite3.connect(path)
        try:
            self.assertEqual(conn.execute('SELECT count(*) FROM tt_post_daily_schedule_audit').fetchone()[0],2)
        finally:
            conn.close()


if __name__ == '__main__':
    unittest.main()
