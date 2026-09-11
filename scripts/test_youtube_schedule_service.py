"""Offline preparation scheduling, ownership, idempotency and expiry behavior."""
import copy
import json
import sys
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts import test_youtube_auto_service as fixtures
from scripts import test_youtube_failure_notifications as notice_fixtures
png=fixtures.png
from features.youtube_auto_publish.scheduling import normalize_publish_at
from features.youtube_auto_publish.failure_notifications import _failure_event_fields
from features.drama_synthesis.core import DramaSynthesisStore
from features.youtube_auto_publish.templates import WorkflowError

FUTURE='2099-10-10T12:30:00Z'
PAST='2000-01-01T00:00:00Z'


class ScheduleServiceCase(unittest.TestCase):
    setUp=fixtures.WorkflowCase.setUp
    payload=fixtures.WorkflowCase.payload
    create=fixtures.WorkflowCase.create
    assert_error=fixtures.WorkflowCase.assert_error
    cover=fixtures.WorkflowCase.cover

    def mutate(self,task,**changes):
        with self.service.db(True) as c:
            _,body=self.service._row(c,task['id'])
            body.update(changes)
            self.service._save(c,body)

    def command(self,task,action='reschedule',at=FUTURE,**kw):
        payload=dict(action=action,publish_at=at,schedule_version=task.get('schedule_version',0),operation_id='schedule_operation_0001')
        payload.update(kw)
        return self.service.schedule(self.actor,task['id'],payload)['task']

    def test_timezone_and_invalid_input(self):
        self.assertEqual(normalize_publish_at('2099-10-10T20:30:00+08:00'),FUTURE)
        for value in ('2099-10-10T20:30',123,True,{},'bad'):
            self.assert_error('invalid_publish_at',lambda:normalize_publish_at(value))
        self.assert_error('publish_at_past',lambda:self.create(publish_at=PAST))
        self.assertEqual(self.store.enqueues,[])

    def test_blank_omitted_idempotent_and_conflicting_schedule(self):
        payload=self.payload()
        first=self.service.create_task(self.actor,payload)['task']
        self.assertEqual(first['publish_at'],'')
        payload['publish_at']=''
        self.assertEqual(self.service.create_task(self.actor,payload)['task']['id'],first['id'])
        payload['publish_at']=FUTURE
        self.assert_error('idempotency_conflict',lambda:self.service.create_task(self.actor,payload))

    def test_scheduled_idempotency_survives_elapsed_time(self):
        payload=self.payload(publish_at=FUTURE)
        task=self.service.create_task(self.actor,payload)['task']
        self.mutate(task,publish_at=PAST)
        self.assertEqual(self.service.create_task(self.actor,payload)['task']['id'],task['id'])

    def test_expired_before_generation_pauses_and_explicit_immediate_resumes(self):
        task=self.create(publish_at=FUTURE)
        self.mutate(task,publish_at=PAST)
        self.service.run_once()
        missed=self.service.get_task(self.actor,task['id'])['task']
        self.assertEqual(missed['status'],'schedule_missed')
        self.assertFalse(missed['can_retry'])
        self.assertEqual(self.store.enqueues,[])
        self.assert_error('retry_unavailable',lambda:self.service.retry(self.actor,task['id']))
        resumed=self.command(missed,'immediate')
        self.assertEqual(resumed['publish_at'],'')
        self.assertEqual(resumed['status'],'queued_generation')

    def test_missed_cover_still_reviewable_but_no_upload(self):
        task=self.create(publish_at=FUTURE)
        self.service.run_once()
        self.mutate(task,publish_at=PAST)
        self.service._claim()
        missed=self.service.get_task(self.actor,task['id'])['task']
        self.assertTrue(missed['can_review'])
        reviewed=self.service.review(self.actor,task['id'],dict(action='approve',version=1))['task']
        self.assertEqual(reviewed['status'],'schedule_missed')
        self.assertFalse(reviewed['can_review'])
        self.assertEqual(self.store.enqueues,[])
        resumed=self.command(reviewed)
        self.assertEqual(resumed['status'],'enqueue_pending')
        self.service.run_once()
        self.service.run_once()
        self.assertEqual(self.store.enqueues[0]['publish_at'],FUTURE)
        self.assertEqual(self.store.enqueues[0]['schedule_version'],1)

    def test_commands_owner_cas_idempotency_and_cancel(self):
        task=self.create()
        result=self.command(task)
        self.assertEqual(result['schedule_version'],1)
        self.assertEqual(self.command(task)['schedule_version'],1)
        self.assert_error('idempotency_conflict',lambda:self.command(task,'immediate'))
        self.assert_error('schedule_conflict',lambda:self.command(task,operation_id='schedule_operation_0002'))
        self.assert_error('not_found',lambda:self.service.schedule(self.other,task['id'],dict(action='cancel',schedule_version=1,operation_id='schedule_operation_0003')))
        canceled=self.command(result,'cancel',operation_id='schedule_operation_0004')
        self.assertEqual(canceled['status'],'cancelled')
        self.assertFalse(canceled['can_schedule'])
        self.assertFalse(canceled['can_review'])
        self.service.run_once()
        self.assertEqual(self.store.enqueues,[])

    def test_active_enqueue_rejects_change(self):
        task=self.create(cover_source='local',cover_asset_id=self.cover()['id'])
        with self.service.db(True) as c:
            c.execute('UPDATE youtube_auto_preparation SET lease_until=? WHERE id=?',(time.time()+300,task['id']))
        self.assert_error('schedule_busy',lambda:self.command(task))

    def test_cancel_during_generation_cannot_enqueue(self):
        task=self.create()
        def generate(*_):
            self.command(task,'cancel')
            return png()
        self.service.generate=generate
        self.service.run_once()
        self.assertEqual(self.service.get_task(self.actor,task['id'])['task']['status'],'cancelled')
        self.service.run_once()
        self.assertEqual(self.store.enqueues,[])

    def test_missed_notification_identity_stable_across_cover_versions(self):
        error={'code':'schedule_missed','message':'missed','missed_at':PAST}
        one=_failure_event_fields('task',1,'schedule_missed',PAST,error,None)
        two=_failure_event_fields('task',2,'schedule_missed',FUTURE,error,None)
        self.assertEqual(one['event_key'],two['event_key'])
        self.assertEqual(one['phase'],'schedule')

    def test_missed_review_keeps_the_original_notice_event(self):
        task=self.create(publish_at=FUTURE)
        self.service.run_once()
        self.mutate(task,publish_at=PAST)
        with patch('features.youtube_auto_publish.service.now',return_value='2026-09-11T10:00:00Z'):
            self.service._claim()
        with self.service.db() as c:
            _,before=self.service._row(c,task['id'])
        with patch('features.youtube_auto_publish.service.now',return_value='2026-09-11T10:05:00Z'):
            self.service.review(self.actor,task['id'],dict(action='approve',version=1))
        with self.service.db() as c:
            _,after=self.service._row(c,task['id'])
        event=lambda body:_failure_event_fields(body['id'],body['current_version'],body['status'],body['updated_at'],body['error'],None)
        self.assertEqual(event(before)['event_key'],event(after)['event_key'])
        self.assertEqual(after['error']['missed_at'],'2026-09-11T10:00:00Z')
        self.assertEqual(after['schedule_resume_state'],'enqueue_pending')
        self.assertEqual(self.store.enqueues,[])

    def test_preparation_command_retry_after_enqueue_does_not_mutate_ledger(self):
        task=self.create(cover_source='local',cover_asset_id=self.cover()['id'])
        self.command(task)
        self.service.run_once()
        self.assertEqual(len(self.store.enqueues),1)
        ledger=self.store.tasks[1]
        ledger.update(publish_at=FUTURE,schedule_version=1,schedule_status='pending')
        self.store.request_reviewed_youtube_schedule=Mock(side_effect=AssertionError('Already accepted command must not reapply'))
        replay=self.command(task)
        self.assertEqual(replay['id'],task['id'])
        self.assertEqual(replay['schedule_version'],1)
        self.assertEqual(replay['publish_at'],FUTURE)
        self.store.request_reviewed_youtube_schedule.assert_not_called()
        self.assertEqual(len(self.store.enqueues),1)

    def test_competing_preparation_commands_have_one_cas_winner(self):
        task=self.create()
        start=Barrier(2)
        def attempt(index):
            start.wait(timeout=5)
            try:
                self.command(task,operation_id='competing_schedule_op_%02d'%index)
                return 'accepted'
            except WorkflowError as exc:
                return exc.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(attempt,(1,2)))
        self.assertCountEqual(results,['accepted','schedule_conflict'])
        latest=self.service.get_task(self.actor,task['id'])['task']
        self.assertEqual(latest['schedule_version'],1)
        self.assertEqual(latest['publish_at'],FUTURE)

    def test_preparation_transaction_ledger_lookup_stays_on_same_connection(self):
        task=self.create()
        real=DramaSynthesisStore(self.service.db_path)
        real.ensure_storage()
        self.service.engine_store=real
        with patch.object(real,'get_reviewed_youtube_by_preparation',side_effect=AssertionError('Lock inversion through another connection')):
            with self.service.db(True) as c:
                _,body=self.service._row(c,task['id'])
                self.assertIsNone(self.service._ledger(body,c))
        self.assertEqual(self.service.get_task(self.actor,task['id'])['task']['id'],task['id'])

    def test_generation_finishes_after_deadline_preserves_cover_and_pauses(self):
        task=self.create(publish_at=FUTURE)
        def finish_late(*_):
            self.mutate(task,publish_at=PAST)
            return png()
        self.service.generate=finish_late
        self.service.run_once()
        result=self.service.get_task(self.actor,task['id'])['task']
        self.assertEqual(result['status'],'schedule_missed')
        self.assertTrue(result['can_review'])
        self.assertTrue(result['cover_url'])
        self.assertEqual(self.store.enqueues,[])


class ScheduleNoticeCase(unittest.TestCase):
    setUp=notice_fixtures.FailureNoticeTests.setUp
    tearDown=notice_fixtures.FailureNoticeTests.tearDown
    add=notice_fixtures.FailureNoticeTests.add

    def test_readonly_schedule_polling_keeps_one_notification(self):
        self.add(phase='schedule')
        with notice_fixtures.connect(self.db) as db:
            db.execute("ALTER TABLE drama_youtube_publish ADD COLUMN schedule_event_at TEXT NOT NULL DEFAULT ''")
            db.execute("UPDATE drama_youtube_publish SET schedule_event_at='2026-09-11T10:00:00Z',error_code='youtube_schedule_readback_unknown'")
        self.assertEqual(self.notifier.run_once()['state'],'sent')
        for stamp in ('2026-09-11T10:01:00Z','2026-09-11T10:02:00Z'):
            with notice_fixtures.connect(self.db) as db:
                db.execute('UPDATE drama_youtube_publish SET updated_at_utc=?',(stamp,))
            self.assertFalse(self.notifier.run_once()['claimed'])
        self.assertEqual(len(self.sent),1)
        with self.notifier._outbox() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM failure_notice').fetchone()[0],1)
        with notice_fixtures.connect(self.db) as db:
            db.execute("UPDATE drama_youtube_publish SET schedule_event_at='2026-09-11T11:00:00Z'")
        self.assertEqual(self.notifier.run_once()['state'],'sent')
        self.assertEqual(len(self.sent),2)
        self.assertNotEqual(self.sent[0]['event_key'],self.sent[1]['event_key'])


if __name__=='__main__':unittest.main()
