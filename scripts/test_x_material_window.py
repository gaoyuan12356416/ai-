"""Material window boundary, frozen policy and real queue/idempotency contracts."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from features.x_material_effects import reuse, window
from features.x_auto_posts.validation import normalize_template_payload, ValidationError
from features.x_auto_posts.publisher import selector_rules
from features.x_posts.service import XPostStore, XPostError
from scripts.test_x_auto_post_validation import valid_payload
from scripts.test_x_post_priority_manual_store import material_candidate

NOW = datetime.now(timezone.utc)
OLD = (NOW-timedelta(days=31)).isoformat()
RECENT = (NOW-timedelta(days=29)).isoformat()


def source(account='2', **extra):
    return dict(account_id=account, _pool_run=0, _effect_policy=False, _lane='tag',
                _material_window_days=30, **extra)


class WindowTests(unittest.TestCase):
    def test_default_validation_and_frozen_legacy_policy(self):
        config = normalize_template_payload(valid_payload())
        self.assertEqual(config['material_window_days'], 30)
        self.assertEqual(selector_rules(config).material_window_days, 30)
        self.assertIsNone(selector_rules(valid_payload()).material_window_days)
        for value in (0, -1, 3651, 1.5, True, None):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                normalize_template_payload(valid_payload(material_window_days=value))
        config = normalize_template_payload(valid_payload(material_window_days=7))
        self.assertEqual(selector_rules(config).as_dict()['material_window_days'], 7)
        self.assertEqual(config['cooldown_days'], 7)

    def test_exact_window_uses_receipt_time_and_handles_timezones(self):
        for age, expected in ((29, True), (30, False), (31, False)):
            completed = (NOW-timedelta(days=age)).astimezone(timezone(timedelta(hours=8))).isoformat()
            self.assertEqual(window.history_conflict(source(), '2', completed, NOW), expected)
        for value in ('', 'invalid', '2020-01-01T00:00:00'):
            self.assertTrue(window.history_conflict(source(), '2', value, NOW))
        self.assertFalse(window.history_conflict(source(), '3', RECENT, NOW))
        self.assertTrue(window.history_conflict(dict(account_id='2'), '2', OLD, NOW))

    def test_selector_checks_window_twice_and_freezes_it_without_effect_filters(self):
        from scripts.test_x_auto_post_selector import FakeSource, FakeMetricStore, FakeStore, drama, material, selector, request, rules
        catalog = FakeSource([drama('C1')], {'C1': [material('101','C1')]})
        ledger = FakeStore()
        with patch.object(reuse, 'client', return_value={'ok':True,'blocked':[]}) as broker:
            selector(catalog, FakeMetricStore([]), store=ledger).select_and_reserve(request(rules(material_window_days=17)))
        self.assertEqual(broker.call_count, 2)
        self.assertTrue(all(c.kwargs['material_window_days']==17 for c in broker.call_args_list))
        self.assertEqual(ledger.reserve_calls[0]['selection_snapshot']['material_window'], dict(policy=window.POLICY, days=17))


class QueueWindowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'posts.sqlite3'
        self.publisher = XPostStore(self.path)
        self.actor = dict(user_id='admin', name='Admin')
        first = self.publisher.create_manual_run(['701'],[2],'first',self.actor)
        plan = self.publisher.create_manual_plan(first['id'],[material_candidate(first,2,'701')])
        self.qid = plan['queues'][0]['id']
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE x_post_queue SET status='published',updated_at=?", (OLD,))
            # Actual publisher schema, including immutable original identity.
            db.execute("INSERT INTO x_post_publish_log(queue_id,account_id,status,x_post_id,published_at,created_at,updated_at) VALUES(?,2,'published','original-post',?,?,?)",(self.qid,OLD,OLD,OLD))
            db.commit()
        self.context = patch.object(reuse, 'publisher_task', return_value=source())
        self.context.start();self.addCleanup(self.context.stop)

    def create_auto(self):
        return self.publisher.create_auto_template_run('701',2,'new-window-task','template',2,
            '{{drama_name}}\n{{desc}}',self.actor,reuse_context=dict(lane='tag',task_id=2))

    def test_expired_history_creates_one_new_queue_and_replay_reuses_it(self):
        new = self.create_auto()
        candidate = material_candidate(new,2,'701')
        with patch.object(reuse,'client',return_value={'ok':True}):
            first = self.publisher.create_manual_plan(new['id'],[candidate],'auto_template')
            again = self.publisher.create_manual_plan(new['id'],[candidate],'auto_template')
        self.assertEqual(first['queues'][0]['id'],again['queues'][0]['id'])
        self.assertEqual(self.create_auto()['id'],new['id'])
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM x_post_queue').fetchone()[0],2)
            self.assertEqual(db.execute('SELECT x_post_id FROM x_post_publish_log').fetchone()[0],'original-post')

    def test_recent_success_and_old_active_or_unknown_remain_blocked(self):
        cases = [('published','published',0,RECENT),('uploading','uploading',0,OLD),
                 ('failed','failed',1,OLD),('published','post_creating',0,OLD),
                 ('published','published',0,'')]
        for queue_status, log_status, unknown, at in cases:
            with self.subTest(status=queue_status,unknown=unknown,at=at), closing(sqlite3.connect(self.path)) as db:
                db.execute('UPDATE x_post_queue SET status=?',(queue_status,))
                db.execute('UPDATE x_post_publish_log SET status=?,unknown_outcome=?,published_at=?',(log_status,unknown,at));db.commit()
                with self.assertRaises(XPostError):self.create_auto()

    def test_cross_lane_reads_use_window_and_hold_unknown_on_other_accounts(self):
        auto = Path(self.temp.name)/'auto.sqlite3'
        with closing(sqlite3.connect(auto)) as db:
            db.executescript('CREATE TABLE x_auto_task(id INTEGER,account_id TEXT,material_id TEXT,status TEXT,published_at_utc TEXT,finished_at_utc TEXT,updated_at TEXT,unknown_outcome INTEGER DEFAULT 0);')
            db.execute("INSERT INTO x_auto_task VALUES(1,'2','701','published',?,?,?,0)",(OLD,OLD,OLD));db.commit()
        with patch.object(reuse,'LEDGERS',[str(self.path)]),patch.object(reuse,'AUTO_PATHS',{'tag':str(auto)}):
            self.assertEqual(reuse.conflicts(['701'],'2',source=source()),set())
            with closing(sqlite3.connect(auto)) as db:
                db.execute("UPDATE x_auto_task SET status='unknown'");db.commit()
            self.assertEqual(reuse.conflicts(['701'],'3',source=source('3')),{'701'})
            with closing(sqlite3.connect(auto)) as db:
                db.execute("UPDATE x_auto_task SET status='failed',unknown_outcome=1");db.commit()
            self.assertEqual(reuse.conflicts(['701'],'3',source=source('3')),{'701'})

    def test_database_guard_rechecks_a_receipt_arriving_after_preview(self):
        new = self.create_auto()
        with closing(sqlite3.connect(self.path)) as db:
            self.assertFalse(window.install_queue_guard(db))
            db.execute('UPDATE x_post_publish_log SET published_at=?',(RECENT,));db.commit()
        # Simulate the last application read missing a concurrent recent receipt.
        # The SQLite write transaction must still prevent a duplicate queue.
        with patch.object(reuse,'client',return_value={'ok':True}),patch.object(reuse,'local_conflict',return_value=False):
            with self.assertRaises(XPostError):
                self.publisher.create_manual_plan(new['id'],[material_candidate(new,2,'701')],'auto_template')
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM x_post_queue').fetchone()[0],1)

    def test_client_cannot_replace_the_frozen_window(self):
        frozen, _ = reuse.freeze_context(dict(lane='tag', task_id=2, material_window_days=1))
        self.assertEqual(frozen['material_window_days'],30)


class GrantWindowTests(unittest.TestCase):
    def test_auto_store_reserves_expired_material_through_real_frozen_broker(self):
        from features.x_auto_posts.core import XAutoPostStore, XAutoPostStoreError
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);auto=XAutoPostStore(root/'auto.sqlite3')
            XPostStore(root/'posts.sqlite3')
            template=auto.create_template(name='window',config=normalize_template_payload(valid_payload(account_ids=['2'])),confirmation={'accepted':True})
            auto.set_template_enabled(template.id,True,expected_version=template.version)
            def new_task(key):
                run=auto.create_run(run_key=key,template_id=template.id,template_version=template.version,
                    trigger_type='auto',scheduled_at_utc=NOW.isoformat(),shanghai_date=NOW.date().isoformat(),publish_time='08:00')
                return auto.create_task(run_id=run.id,account_id='2',language='en',account_snapshot_version=1,
                    account_snapshot=dict(language_source='account',drama_language='en'))
            first=new_task('first');second=new_task('second')
            with patch.object(reuse,'AUTO_PATHS',{'tag':str(root/'auto.sqlite3')}),patch.object(reuse,'LEDGERS',[str(root/'posts.sqlite3')]),patch.object(reuse,'lane',return_value='tag'):
                def broker(action,**payload):
                    result=reuse.handle(dict(action=action,**payload),root)
                    if not result['ok']:raise RuntimeError(result['code'])
                    return result
                with patch.object(reuse,'client',side_effect=broker):
                    frozen={'material_window':dict(policy=window.POLICY,days=30)}
                    auto.reserve_material(task_id=first.id,material_id='701',content_id='42',series_code='42',selection_snapshot=frozen)
                    with self.assertRaises(XAutoPostStoreError):
                        auto.reserve_material(task_id=second.id,material_id='701',content_id='42',series_code='42',selection_snapshot=frozen)
                    with closing(sqlite3.connect(root/'auto.sqlite3')) as db:
                        db.execute("UPDATE x_auto_task SET status='published',published_at_utc=?,finished_at_utc=? WHERE id=?",(OLD,OLD,first.id));db.commit()
                    auto.reserve_material(task_id=second.id,material_id='701',content_id='42',series_code='42',selection_snapshot=frozen)
                    auto.reserve_material(task_id=second.id,material_id='701',content_id='42',series_code='42',selection_snapshot=frozen)
                with closing(sqlite3.connect(root/'auto.sqlite3')) as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM x_auto_material_ledger').fetchone()[0],2)
                auto.update_template(template.id,expected_version=template.version,
                    config=normalize_template_payload(valid_payload(account_ids=['2'],material_window_days=7)))
                self.assertEqual(reuse.handle(dict(action='context',lane='tag',task_id=second.id))['item']['_material_window_days'],30)

    def test_expired_grant_rechecks_receipt_and_freezes_new_identity(self):
        sources = {1:source(id=1,status='selecting',material_id=''),2:source(id=2,status='selecting',material_id='')}
        with tempfile.TemporaryDirectory() as directory, patch.object(reuse,'task',side_effect=lambda lane,tid:sources[tid]),patch.object(reuse,'conflicts',return_value=set()):
            one = dict(action='reserve',lane='tag',task_id=1,account_id='2',material_id='701')
            self.assertTrue(reuse.handle(one,directory)['ok'])
            self.assertFalse(reuse.handle(dict(one,task_id=2),directory)['ok'])
            sources[1].update(status='published',material_id='701',published_at_utc=RECENT)
            self.assertFalse(reuse.handle(dict(one,task_id=2),directory)['ok'])
            sources[1]['published_at_utc']=OLD
            self.assertTrue(reuse.handle(dict(one,task_id=2),directory)['ok'])
            self.assertTrue(reuse.handle(dict(one,task_id=2),directory)['ok'])
            with closing(sqlite3.connect(Path(directory)/'reuse.sqlite3')) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM grants').fetchone()[0],2)


if __name__=='__main__':unittest.main()
