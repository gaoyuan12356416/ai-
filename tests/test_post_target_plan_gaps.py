"""Regression coverage for TT frozen target plans in read-only backlog counts."""
import hashlib
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from features.post_daily_report.common import Window
from features.post_daily_report.plans import _CACHE, collect_plan_gaps


AUTO_SCHEMA = """
CREATE TABLE tt_auto_template(id INTEGER PRIMARY KEY,current_version INTEGER);
CREATE TABLE tt_auto_template_version(template_id INTEGER,version INTEGER,config_json TEXT,config_sha256 TEXT);
CREATE TABLE tt_auto_event(event_type TEXT,details_json TEXT,created_at TEXT);
CREATE TABLE tt_auto_random_plan(template_id INTEGER,template_version INTEGER,shanghai_date TEXT,publish_times_json TEXT);
CREATE TABLE tt_auto_run(id INTEGER PRIMARY KEY,template_id INTEGER,template_version INTEGER,trigger_type TEXT,
 shanghai_date TEXT,publish_time TEXT,scheduled_at_utc TEXT,status TEXT,error_code TEXT,target_account_id TEXT,schedule_policy TEXT);
CREATE TABLE tt_auto_task(id INTEGER PRIMARY KEY,run_id INTEGER,account_id TEXT,status TEXT);
"""
TARGET_SCHEMA = """
CREATE TABLE social_random_policy(policy TEXT PRIMARY KEY,effective_date TEXT);
CREATE TABLE social_random_day(source_key TEXT,local_date TEXT,config_version TEXT,config_json TEXT,targets_json TEXT,created_at TEXT);
CREATE TABLE social_random_slot(source_key TEXT,local_date TEXT,target_id TEXT,ordinal INTEGER,planned_at TEXT,native_kind TEXT,native_id TEXT);
INSERT INTO social_random_policy VALUES('target_midnight_v1','2026-11-11');
"""


class TargetPlanGapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = {key: Path(self.tmp.name) / (key + '.db') for key in ('tt', 'tt_auto')}
        sqlite3.connect(self.paths['tt']).close()
        self.db = sqlite3.connect(self.paths['tt_auto'])
        self.addCleanup(self.db.close)
        self.db.executescript(AUTO_SCHEMA + TARGET_SCHEMA)
        self.window = Window.for_date('2026-11-11')

    def template(self, accounts=('a', 'b'), *, template_id=1, count=2, mode='random',
                 enabled_at='2026-11-10T16:00:00Z', version=1, times=('09:00',)):
        config = {'account_ids': list(accounts), 'schedule': {'mode': mode, 'daily_count': count, 'times': list(times)}}
        self.db.execute('INSERT OR REPLACE INTO tt_auto_template VALUES(?,?)', (template_id, version))
        self.db.execute('INSERT INTO tt_auto_template_version VALUES(?,?,?,?)',
                        (template_id, version, json.dumps(config), 'frozen'))
        self.event(template_id, version, 'template_enabled', enabled_at)
        return config

    def event(self, template_id, version, kind, stamp):
        self.db.execute('INSERT INTO tt_auto_event VALUES(?,?,?)',
                        (kind, json.dumps({'template_id': template_id, 'version': version}), stamp))

    def freeze(self, config, targets, *, template_id=1, day='2026-11-11', version=1):
        window = Window.for_date(day)
        frozen = []
        for account, times in targets.items():
            slots = [{'ordinal': n, 'publish_time': time, 'scheduled_at': window.local_slot(time).isoformat()}
                     for n, time in enumerate(times)]
            frozen.append({'target_id': account, 'times': times, 'slots': slots})
            self.db.executemany('INSERT INTO social_random_slot VALUES(?,?,?,?,?,?,?)',
                                [('tt_auto:' + str(template_id), day, account, slot['ordinal'], slot['scheduled_at'], '', '')
                                 for slot in slots])
        self.db.execute('INSERT INTO social_random_day VALUES(?,?,?,?,?,?)',
                        ('tt_auto:' + str(template_id), day, str(version), json.dumps(config), json.dumps(frozen), window.start.isoformat()))

    def add_run(self, run_id, account='', time='09:00', *, template_id=1, version=1, day='2026-11-11',
            policy='target_midnight_v1', trigger='auto', task=False):
        self.db.execute('INSERT INTO tt_auto_run VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                        (run_id, template_id, version, trigger, day, time,
                         Window.for_date(day).local_slot(time).isoformat(), 'failed', '', account, policy))
        if task:
            self.db.execute('INSERT INTO tt_auto_task VALUES(?,?,?,?)', (run_id, run_id, account, 'failed'))

    def gaps(self, cutoff=None):
        self.db.commit()
        _CACHE.clear()
        return collect_plan_gaps(self.paths, cutoff or self.window.end, ['tt'])

    def test_sixty_four_real_failed_tasks_do_not_create_seven_hundred_gaps(self):
        run_id = 0
        for template_id, size in ((1, 13), (2, 3)):
            accounts = [str(n) for n in range(size)]
            config = self.template(accounts, template_id=template_id, count=4)
            targets = {account: [f'{hour:02d}:{int(account):02d}' for hour in (1, 6, 12, 18)] for account in accounts}
            self.freeze(config, targets, template_id=template_id)
            for account, times in targets.items():
                for time in times:
                    run_id += 1
                    self.add_run(run_id, account, time, template_id=template_id, task=True)
        result = self.gaps()
        self.assertTrue(result['available'])
        self.assertEqual(result['items'], [])
        self.assertEqual(run_id, 64)

    def test_slot_run_and_task_are_one_plan_each_even_at_equal_minutes(self):
        config = self.template(('a', 'b', 'c'), count=1)
        self.freeze(config, {account: ['09:00'] for account in ('a', 'b', 'c')})
        self.add_run(2, 'b')
        self.add_run(3, 'c', task=True)
        result = self.gaps()
        self.assertTrue(result['available'])
        self.assertEqual(sorted(r['account_id'] for r in result['items']), ['a', 'b'])

    def test_frozen_old_version_survives_midday_change_to_fixed(self):
        old = self.template(('a',), count=1)
        self.freeze(old, {'a': ['17:00']})
        self.event(1, 1, 'template_updated', '2026-11-11T02:00:00Z')
        self.template(('a', 'b', 'c'), version=2, mode='fixed', times=('15:00', '20:00'),
                      enabled_at='2026-11-11T02:00:00Z')
        result = self.gaps()
        self.assertTrue(result['available'])
        self.assertEqual([(r['account_id'], r['due_at']) for r in result['items']],
                         [('a', self.window.local_slot('17:00').isoformat())])

    def test_legacy_day_and_new_day_are_both_counted_from_their_own_plan(self):
        config = self.template(('a',), count=1, enabled_at='2026-11-09T16:00:00Z')
        self.db.execute('INSERT INTO tt_auto_random_plan VALUES(1,1,?,?)', ('2026-11-10', '["09:00"]'))
        self.freeze(config, {'a': ['12:00']})
        result = self.gaps()
        self.assertTrue(result['available'])
        self.assertEqual({r['due_at'] for r in result['items']},
                         {Window.for_date('2026-11-10').local_slot('09:00').isoformat(), self.window.local_slot('12:00').isoformat()})

    def test_missing_new_plan_is_unknown_without_old_seed_or_whole_account_expansion(self):
        self.template()
        self.add_run(1, 'a', task=True)
        result = self.gaps()
        self.assertFalse(result['channel_available']['tt'])
        self.assertEqual(result['items'], [])
        self.assertTrue(any('日计划缺失' in message for message in result['warnings']))

    def test_midday_first_enable_starts_tomorrow(self):
        self.template(enabled_at='2026-11-11T01:00:00Z')
        result = self.gaps()
        self.assertTrue(result['available'])
        self.assertEqual(result['items'], [])

    def test_new_run_missing_target_does_not_expand_configured_accounts(self):
        self.template()
        self.add_run(1)
        result = self.gaps()
        self.assertFalse(result['available'])
        self.assertEqual(result['items'], [])
        self.assertTrue(any('缺少目标账号' in message for message in result['warnings']))

    def test_missing_policy_table_with_target_run_does_not_restore_old_seed(self):
        self.template()
        self.db.execute('DROP TABLE social_random_policy')
        self.add_run(1, 'a', task=True)
        result = self.gaps()
        self.assertFalse(result['available'])
        self.assertEqual(result['items'], [])

    def test_missing_frozen_config_keeps_known_run_target(self):
        self.add_run(1, 'a')
        result = self.gaps()
        self.assertEqual([r['account_id'] for r in result['items']], ['a'])

    def test_old_database_random_and_manual_scopes_remain_compatible(self):
        for table in ('social_random_policy', 'social_random_day', 'social_random_slot'):
            self.db.execute('DROP TABLE ' + table)
        self.template(count=1)
        self.db.execute('INSERT INTO tt_auto_random_plan VALUES(1,1,?,?)', ('2026-11-11', '["09:00"]'))
        self.add_run(1, policy='', trigger='manual', time='10:00')
        self.db.execute("INSERT INTO tt_auto_task VALUES(1,1,'a','failed')")
        result = self.gaps()
        self.assertTrue(result['available'])
        self.assertEqual(len(result['items']), 3)
        self.assertEqual(sorted(r['account_id'] for r in result['items']), ['a', 'b', 'b'])

    def test_fixed_plan_still_runs_after_target_policy_activation(self):
        self.template(mode='fixed')
        result = self.gaps()
        self.assertTrue(result['available'])
        self.assertEqual(sorted(r['account_id'] for r in result['items']), ['a', 'b'])

    def test_target_cutoff_is_exclusive_and_reads_do_not_modify_database(self):
        config = self.template(('a',), count=3)
        self.freeze(config, {'a': ['09:00', '10:00', '11:00']})
        self.db.commit()
        before = hashlib.sha256(self.paths['tt_auto'].read_bytes()).hexdigest()
        result = self.gaps(self.window.local_slot('10:00'))
        self.assertEqual(len(result['items']), 1)
        self.assertEqual(result['items'][0]['due_at'], self.window.local_slot('09:00').isoformat())
        self.assertEqual(before, hashlib.sha256(self.paths['tt_auto'].read_bytes()).hexdigest())

    def test_malformed_frozen_plan_is_unknown_and_does_not_seed_a_replacement(self):
        config = self.template()
        self.freeze(config, {'a': ['09:00']})
        self.db.execute("UPDATE social_random_day SET targets_json='broken'")
        result = self.gaps()
        self.assertFalse(result['available'])
        self.assertEqual(result['items'], [])

    def test_x_runs_keep_their_existing_frozen_template_scope(self):
        from tests.test_post_daily_report_x import PRIMARY_SCHEMA, AUTO_SCHEMA as X_SCHEMA
        paths = {key: Path(self.tmp.name) / (key + '.db') for key in ('x', 'x_auto')}
        with closing(sqlite3.connect(paths['x'])) as primary:
            primary.executescript(PRIMARY_SCHEMA)
        with closing(sqlite3.connect(paths['x_auto'])) as auto:
            auto.executescript(X_SCHEMA)
            auto.execute('INSERT INTO x_auto_template_version VALUES(1,1,?)',
                         (json.dumps({'account_ids': ['a', 'b']}),))
            auto.execute("INSERT INTO x_auto_run VALUES(1,1,1,'auto','2026-11-11','09:00','failed','')")
            auto.execute("INSERT INTO x_auto_task(id,run_id,account_id,status) VALUES(1,1,'a','failed')")
            auto.commit()
        _CACHE.clear()
        result = collect_plan_gaps(paths, self.window.end, ['x'])
        self.assertTrue(result['available'])
        self.assertEqual([(row['channel'], row['account_id']) for row in result['items']], [('x', 'b')])


if __name__ == '__main__':
    unittest.main()
