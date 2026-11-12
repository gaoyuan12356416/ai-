"""Daily quotas follow frozen account plans across the midnight rollout."""
import hashlib
import json
import unittest
from pathlib import Path

from features.post_daily_report.common import Window
from tests import test_post_daily_report_tt as legacy_tests


class TargetPlanReportTests(unittest.TestCase):
    tearDown = legacy_tests.TTReportTests.tearDown
    report = legacy_tests.TTReportTests.report
    row = legacy_tests.TTReportTests.row

    def setUp(self):
        legacy_tests.TTReportTests.setUp(self)
        self.window = Window.for_date('2026-11-11')
        self.auto.executescript('''
          ALTER TABLE tt_auto_run ADD COLUMN target_account_id TEXT DEFAULT '';
          ALTER TABLE tt_auto_run ADD COLUMN schedule_policy TEXT DEFAULT '';
          CREATE TABLE social_random_policy(policy TEXT,effective_date TEXT);
          INSERT INTO social_random_policy VALUES('target_midnight_v1','2026-11-11');
          CREATE TABLE social_random_day(source_key TEXT,local_date TEXT,config_version TEXT,config_json TEXT,targets_json TEXT);
        ''')
        self.next_run = 1

    def plan(self, template=1, count=2, daily=1, form='random_overlay', tasks=True, runs=True):
        accounts = ['t%s-a%s' % (template, n) for n in range(count)]
        cfg = {'account_ids': accounts, 'schedule': {'mode': 'random', 'daily_count': daily}, 'video_template': form}
        self.auto.execute('INSERT INTO tt_auto_template VALUES(?,1,1,?,?)', (template, '2026-09-01T00:00:00Z', '2026-09-01T00:00:00Z'))
        self.auto.execute('INSERT INTO tt_auto_template_version VALUES(?,1,?,?)', (template, json.dumps(cfg), 'frozen'))
        targets = []
        for index, account in enumerate(accounts):
            slots = []
            for ordinal in range(daily):
                when = self.window.start.replace(hour=17, minute=(index * daily + ordinal) % 60).isoformat()
                slots.append({'ordinal': ordinal, 'scheduled_at': when})
                if runs:
                    run_id = self.next_run
                    self.next_run += 1
                    self.auto.execute('INSERT INTO tt_auto_run VALUES(?,?,1,?,?,?,?,?)', (run_id, template, 'auto', self.window.date, when, account, 'target_midnight_v1'))
                    if tasks:
                        self.auto.execute('INSERT INTO tt_auto_task VALUES(?,?,?,1,?,?,?,?,?,?,?,?)', (run_id, run_id, template, account, 'failed', when, '', '', 'metric_not_ready', '', 0))
            targets.append({'target_id': account, 'slots': slots})
        self.auto.execute('INSERT INTO social_random_day VALUES(?,?,?,?,?)', ('tt_auto:' + str(template), self.window.date, '1', json.dumps(cfg), json.dumps(targets)))
        return cfg, targets

    def test_incident_64_real_tasks_do_not_become_776(self):
        self.plan(count=13, daily=4)
        self.plan(template=2, count=3, daily=4, form='direct_outro')
        report = self.report()
        rows = {r['source']: r for r in report['rows']}
        for form, expected in [('random_overlay', 52), ('direct_outro', 12)]:
            row = rows['auto_template:' + form]
            self.assertEqual((row['expected'], row['failed'], row['pending'], row['unqueued']), (expected, expected, 0, 0))

    def test_frozen_plan_without_runs_keeps_real_missing_tasks(self):
        self.plan(runs=False)
        row = self.row('auto_template:random_overlay')
        self.assertEqual((row['expected'], row['pending'], row['unqueued']), (2, 2, 2))

    def test_run_without_task_counts_its_target_once(self):
        self.plan(tasks=False)
        row = self.row('auto_template:random_overlay')
        self.assertEqual((row['expected'], row['pending']), (2, 2))

    def test_same_time_on_two_accounts_is_two_targets(self):
        cfg, targets = self.plan(runs=False)
        targets[1]['slots'][0]['scheduled_at'] = targets[0]['slots'][0]['scheduled_at']
        self.auto.execute('UPDATE social_random_day SET targets_json=?', (json.dumps(targets),))
        self.assertEqual(self.row('auto_template:random_overlay')['expected'], 2)

    def test_same_day_edit_retains_old_frozen_scope_and_form(self):
        cfg, _ = self.plan()
        cfg.update(account_ids=['new-a', 'new-b', 'new-c'], video_template='direct_outro', schedule={'mode': 'fixed', 'times': ['09:00']})
        self.auto.execute('INSERT INTO tt_auto_template_version VALUES(1,2,?,?)', (json.dumps(cfg), 'new'))
        self.auto.execute("UPDATE tt_auto_template SET current_version=2,updated_at='2026-11-11T01:00:00Z'")
        self.auto.execute("INSERT INTO tt_auto_event VALUES(1,'template_updated','{\"template_id\":1,\"version\":2}','2026-11-11T01:00:00Z')")
        row = self.row('auto_template:random_overlay')
        self.assertEqual((row['expected'], row['failed']), (2, 2))
        self.assertFalse(any(r['source'] == 'auto_template:direct_outro' for r in self.report()['rows']))

    def test_missing_new_plan_is_unknown_and_never_falls_back_to_old_seed(self):
        self.plan(count=1)
        self.auto.execute('DELETE FROM social_random_day')
        row = self.row('auto_template:random_overlay')
        self.assertIsNone(row['expected'])
        self.assertEqual((row['failed'], row['pending']), (1, 0))
        self.assertTrue(row['warnings'])

    def test_run_policy_without_policy_table_still_blocks_old_seed(self):
        self.plan(count=1)
        self.auto.execute('DELETE FROM social_random_day')
        self.auto.execute('DROP TABLE social_random_policy')
        row = self.row('auto_template:random_overlay')
        self.assertIsNone(row['expected'])
        self.assertEqual(row['pending'], 0)

    def test_midday_enable_starts_tomorrow(self):
        self.plan(runs=False)
        self.auto.execute('DELETE FROM social_random_day')
        self.auto.execute("UPDATE tt_auto_template SET enabled_at_utc='2026-11-11T01:00:00Z',updated_at='2026-11-11T01:00:00Z'")
        self.auto.execute("INSERT INTO tt_auto_event VALUES(1,'template_enabled','{\"template_id\":1,\"version\":1}','2026-11-11T01:00:00Z')")
        row = self.row('auto_template:random_overlay')
        self.assertEqual((row['expected'], row['pending']), (0, 0))

    def test_fixed_plan_after_activation_keeps_legacy_scope(self):
        cfg, _ = self.plan(runs=False)
        self.auto.execute('DELETE FROM social_random_day')
        cfg['schedule'] = {'mode': 'fixed', 'times': ['01:00']}
        self.auto.execute('UPDATE tt_auto_template_version SET config_json=?', (json.dumps(cfg),))
        row = self.row('auto_template:random_overlay')
        self.assertEqual((row['expected'], row['pending']), (2, 2))

    def test_manual_run_keeps_its_full_frozen_scope(self):
        self.plan()
        self.auto.execute('INSERT INTO tt_auto_run VALUES(99,1,1,?,?,?,?,?)', ('manual', self.window.date, self.window.start.isoformat(), '', ''))
        row = self.row('auto_template_manual:random_overlay')
        self.assertEqual((row['expected'], row['pending']), (2, 2))
        self.assertEqual(self.row('auto_template:random_overlay')['expected'], 2)

    def test_target_run_without_account_does_not_expand_template(self):
        self.plan(tasks=False)
        self.auto.execute("UPDATE tt_auto_run SET target_account_id=''")
        row = self.row('auto_template:random_overlay')
        self.assertIsNone(row['expected'])
        self.assertEqual(row['pending'], 2)

    def test_reporting_preserves_source_database_bytes(self):
        self.plan()
        self.auto.commit()
        self.legacy.commit()
        before = {k: hashlib.sha256(Path(v).read_bytes()).hexdigest() for k, v in self.paths.items()}
        self.report()
        after = {k: hashlib.sha256(Path(v).read_bytes()).hexdigest() for k, v in self.paths.items()}
        self.assertEqual(before, after)

    def test_empty_targets_with_positive_quota_stays_unknown(self):
        self.plan(runs=False)
        self.auto.execute("UPDATE social_random_day SET targets_json='[]'")
        self.assertIsNone(self.row('auto_template:random_overlay')['expected'])

    def test_missing_slot_preserves_known_subtotal_with_unknown_quota(self):
        _, targets = self.plan(runs=False)
        targets[1]['slots'] = []
        self.auto.execute('UPDATE social_random_day SET targets_json=?', (json.dumps(targets),))
        row = self.row('auto_template:random_overlay')
        self.assertIsNone(row['expected'])
        self.assertEqual(row['pending'], 1)


if __name__ == '__main__':
    unittest.main()
