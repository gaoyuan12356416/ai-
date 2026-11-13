import json
import sqlite3
import unittest
from pathlib import Path

from tests.test_post_daily_report_x import XCollectorTests, PRIMARY_SCHEMA, AUTO_SCHEMA
from features.post_daily_report.common import Window
from features.post_daily_report.plans import collect_plan_gaps
from features.post_daily_report.report import validate_channel


class ProfileTargetTests(unittest.TestCase):
    insert = XCollectorTests.insert
    plan = XCollectorTests.plan
    result = XCollectorTests.result

    def setUp(self):
        XCollectorTests.setUp(self)
        self.window = Window.for_date('2026-11-12')
        self.db.execute("UPDATE x_post_schedule_random_plan SET run_date=?", (self.window.date,))
        self.auto.executescript('''
            ALTER TABLE x_auto_run ADD COLUMN schedule_policy TEXT;
            ALTER TABLE x_auto_run ADD COLUMN run_key TEXT;
            ALTER TABLE x_auto_run ADD COLUMN scheduled_at_utc TEXT;
            CREATE TABLE social_random_policy(policy TEXT,effective_date TEXT);
            INSERT INTO social_random_policy VALUES('target_midnight_v1','2026-11-11');
            CREATE TABLE social_random_day(source_key TEXT,local_date TEXT,config_version TEXT,config_json TEXT,targets_json TEXT);
        ''')

    def target_plan(self, accounts=('1', '2'), runs=True, tasks=True):
        config = {'account_ids': list(accounts), 'schedule': {'mode': 'random', 'daily_count': 1}}
        self.insert(self.auto, 'x_auto_template', id=1, enabled=1, created_at='2026-09-01T00:00:00Z')
        self.insert(self.auto, 'x_auto_template_version', template_id=1, version=1, config_json=json.dumps(config))
        targets = []
        for n, account in enumerate(accounts, 1):
            due = '2026-11-12T01:00:00Z'  # Different accounts can share a minute.
            targets.append({'target_id': account, 'slots': [{'ordinal': 0, 'scheduled_at': due}]})
            if runs:
                self.insert(self.auto, 'x_auto_run', id=n, template_id=1, template_version=1, trigger_type='auto',
                            shanghai_date=self.window.date, publish_time='09:00', scheduled_at_utc=due,
                            status='completed', schedule_policy='target_midnight_v1',
                            run_key=f'x-auto:auto:target_midnight_v1:1:{self.window.date}:{account}:0')
            if tasks:
                self.insert(self.auto, 'x_auto_task', id=n, run_id=n, account_id=account, status='failed', error_code='x_account_not_publishable')
        self.insert(self.auto, 'social_random_day', source_key='x:auto:1', local_date=self.window.date,
                    config_version='1', config_json=json.dumps(config), targets_json=json.dumps(targets))

    def profile(self):
        for key, schema in [('x_tag', PRIMARY_SCHEMA), ('x_tag_auto', AUTO_SCHEMA)]:
            self.paths[key] = str(Path(self.tmp.name) / (key + '.db'))
            conn = sqlite3.connect(self.paths[key]); self.addCleanup(conn.close)
            conn.executescript(schema)
            setattr(self, key, conn)
        for source in ['material', 'drama']:
            self.insert(self.x_tag, 'x_post_schedule_random_plan', source_type=source, run_date=self.window.date,
                        account_ids_json='[]', publish_times_json='[]')

    def test_new_runs_count_targets_not_whole_template(self):
        self.target_plan(accounts=tuple(str(n) for n in range(85)))
        result, rows = self.result()
        self.assertEqual((rows['auto_template']['expected'], rows['auto_template']['failed']), (85, 85))
        self.assertEqual(rows['auto_template']['pending'], 0)
        gaps = collect_plan_gaps(self.paths, self.window.cutoff, ('x',))
        self.assertEqual(gaps['items'], [])
        validate_channel(result)

    def test_target_without_task_has_one_gap(self):
        self.target_plan(tasks=False)
        _, rows = self.result()
        self.assertEqual((rows['auto_template']['expected'], rows['auto_template']['blocked']), (2, 2))
        gaps = collect_plan_gaps(self.paths, self.window.cutoff, ('x',))
        self.assertEqual(len(gaps['items']), 2)

    def test_frozen_targets_without_runs_not_lost(self):
        self.target_plan(runs=False, tasks=False)
        _, rows = self.result()
        self.assertEqual((rows['auto_template']['expected'], rows['auto_template']['pending']), (2, 2))

    def test_unknown_target_does_not_expand_template(self):
        self.target_plan(tasks=False)
        self.auto.execute('DELETE FROM social_random_day')
        self.auto.execute("UPDATE x_auto_run SET run_key='' WHERE id=1")
        _, rows = self.result()
        self.assertIsNone(rows['auto_template']['expected'])
        self.assertEqual(rows['auto_template']['blocked'], 1)

    def test_cancelled_task_is_not_shared_paused_failure(self):
        self.target_plan(accounts=('1',))
        self.auto.execute("UPDATE x_auto_task SET status='canceled',error_code='x_ordinary_lane_retired'")
        self.paths['_accounting'] = {'jobs': {'x:auto:1': [{'id': 1, 'stage': 'publish', 'status': 'backlog_paused', 'hold_reason': 'cutover_overdue'}]}}
        _, rows = self.result()
        self.assertEqual((rows['auto_template']['policy_skipped'], rows['auto_template']['blocked']), (1, 0))

    def test_profile_post_cost_and_prior_arrival_are_included(self):
        self.profile()
        self.db.execute('ALTER TABLE x_post_publish_log ADD COLUMN post_text TEXT')
        self.x_tag.execute('ALTER TABLE x_post_publish_log ADD COLUMN post_text TEXT')
        self.insert(self.x_tag, 'x_post_manual_run', id=90, trigger_source='manual', run_date=self.window.date,
                    created_at='2026-11-12T01:00:00Z', account_ids_json='[1]', expected_count=1, status='completed')
        for qid, day in [(90, self.window.date), (91, '2026-11-11')]:
            self.insert(self.x_tag, 'x_post_queue', id=qid, account_id=1, run_date=day, status='published',
                        delivery_mode='direct', manual_run_id=90 if qid == 90 else None)
            self.insert(self.x_tag, 'x_post_publish_log', id=qid, queue_id=qid, status='published',
                        x_post_id=str(qid), published_at='2026-11-12T01:01:00Z', post_text='https://example.com')
        self.x_tag.commit(); self.x_tag_auto.commit()
        result, rows = self.result()
        self.assertEqual(rows['profile_manual_immediate']['published'], 1)
        self.assertEqual(result['prior_completed'], 1)
        self.assertEqual(result['cost']['counts']['post_with_url'], 2)
        self.assertEqual(result['cost']['display_usd'], '0.40')

    def test_profile_bridge_uses_own_queue_even_when_ids_overlap(self):
        self.profile()
        self.insert(self.x_tag_auto, 'x_auto_template_version', template_id=1, version=1,
                    config_json=json.dumps({'account_ids': ['1']}))
        self.insert(self.x_tag_auto, 'x_auto_run', id=1, template_id=1, template_version=1, trigger_type='manual',
                    shanghai_date=self.window.date, publish_time='09:00', status='completed')
        self.insert(self.x_tag_auto, 'x_auto_task', id=1, run_id=1, account_id='1', status='published',
                    execution_queue_id=1, publish_id='9001', published_at_utc='2026-11-12T01:01:00Z')
        self.insert(self.x_tag, 'x_post_manual_run', id=1, trigger_source='auto_template', run_date=self.window.date)
        self.insert(self.x_tag, 'x_post_queue', id=1, account_id=1, run_date=self.window.date, status='failed', manual_run_id=1)
        self.insert(self.x_tag, 'x_post_publish_log', id=1, queue_id=1, status='failed', unknown_outcome=1)
        self.x_tag.commit(); self.x_tag_auto.commit()
        result, rows = self.result()
        self.assertEqual((rows['profile_auto_template']['published'], rows['profile_auto_template']['unknown']), (0, 1))

    def test_same_post_across_lanes_is_counted_once_with_cost(self):
        self.profile()
        for conn in (self.db, self.x_tag):
            conn.execute('ALTER TABLE x_post_publish_log ADD COLUMN post_text TEXT')
            self.insert(conn, 'x_post_manual_run', id=1, trigger_source='manual', run_date=self.window.date,
                        created_at='2026-11-12T01:00:00Z', account_ids_json='[1]', expected_count=1)
            self.insert(conn, 'x_post_queue', id=1, account_id=1, run_date=self.window.date, status='published', manual_run_id=1)
            self.insert(conn, 'x_post_publish_log', id=1, queue_id=1, status='published', x_post_id='same-post',
                        published_at='2026-11-12T01:01:00Z', post_text='https://example.com')
            conn.commit()
        self.x_tag_auto.commit()
        result, rows = self.result()
        self.assertEqual(sum(r['published'] for r in rows.values()), 1)
        self.assertEqual(rows['profile_manual_immediate']['expected'], 0)
        self.assertEqual(result['cost']['counts']['post_with_url'], 1)
        validate_channel(result)

    def test_same_minute_pool_targets_keep_both_runs(self):
        self.db.executescript('CREATE TABLE social_random_day(source_key TEXT,local_date TEXT,config_version TEXT,config_json TEXT,targets_json TEXT);')
        targets = [{'target_id': str(a), 'slots': [{'ordinal': 0, 'scheduled_at': '2026-11-12T01:00:00Z'}]} for a in (1, 2)]
        self.insert(self.db, 'social_random_day', source_key='x:pool:material', local_date=self.window.date,
                    config_version='1', config_json=json.dumps({'account_ids': [1, 2]}), targets_json=json.dumps(targets))
        for a in (1, 2):
            self.insert(self.db, 'x_post_schedule_run', id=a, source_type='material', run_date=self.window.date,
                        publish_time='09:00', account_ids_json=json.dumps([a]), status='completed')
            self.insert(self.db, 'x_post_queue', id=a, account_id=a, run_date=self.window.date, status='published', schedule_run_id=a)
            self.insert(self.db, 'x_post_publish_log', id=a, queue_id=a, status='published', x_post_id=str(a), published_at='2026-11-12T01:01:00Z')
        result, rows = self.result()
        self.assertEqual((rows['material_pool']['expected'], rows['material_pool']['published']), (2, 2))
        self.assertEqual(collect_plan_gaps(self.paths, self.window.cutoff, ('x',))['items'], [])
        validate_channel(result)

    def test_profile_missing_ledger_is_unknown_not_zero(self):
        self.profile(); self.x_tag.commit(); self.x_tag_auto.commit()
        self.paths['x_tag_auto'] = str(Path(self.tmp.name) / 'missing.db')
        result, rows = self.result()
        self.assertFalse(rows['profile_auto_template']['data_available'])
        self.assertIn('x_tag_auto', result['cost']['unreadable_sources'])
