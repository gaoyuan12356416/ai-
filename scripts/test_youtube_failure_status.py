"""Read-only status projection and deployed-outbox compatibility regressions."""
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.youtube_auto_publish.failure_notifications import failure_notification_status
from scripts import test_youtube_failure_notifications as deployed
from scripts import test_youtube_auto_service as workflow


def deployed_key(body, ledger=None):
    """Independent copy of a292b352's exact five-field event fingerprint."""
    preparation = body['status'] in ('generation_failed', 'enqueue_failed')
    phase = ('cover' if body['status'] == 'generation_failed' else 'enqueue') if preparation else ledger.get('reviewed_phase') or 'upload'
    code = str((body.get('error') or {}).get('code', body['status'])) if preparation else str(ledger.get('error_code') or 'publish_failed')
    stamp = body['updated_at'] if preparation else ledger.get('updated_at_utc')
    return hashlib.sha256(json.dumps([body['id'], body['current_version'], phase, code, stamp], separators=(',', ':')).encode()).hexdigest()


class FailureStatusTests(unittest.TestCase):
    def setUp(self):
        self.fixture = deployed.FailureNoticeTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.notifier = self.fixture.notifier
        self.outbox = self.notifier.outbox_db

    def body_ledger(self, task='task-1'):
        with deployed.connect(self.fixture.db) as db:
            db.row_factory = sqlite3.Row
            row = db.execute('SELECT * FROM youtube_auto_preparation WHERE id=?', (task,)).fetchone()
            body = json.loads(row['body'])
            body.update(id=row['id'], current_version=row['version'], status=row['state'], updated_at=row['updated_at'])
            ledger = db.execute('SELECT * FROM drama_youtube_publish WHERE preparation_id=?', (task,)).fetchone()
            return body, dict(ledger) if ledger else None

    def insert(self, event, state='sent', created=100):
        with self.notifier._outbox() as db:
            db.execute('INSERT INTO failure_notice(event_key,payload,state,created_at,message_id) VALUES(?,?,?,?,?)',
                       (event['event_key'], json.dumps(event), state, created, 'original-message-receipt'))

    def test_missing_database_never_creates_parent_or_file(self):
        path = self.fixture.root/'missing-parent'/'missing.sqlite3'
        self.assertIsNone(failure_notification_status(path, {'id':'missing'}, None))
        self.assertFalse(path.parent.exists())

    def test_missing_table_returns_none_without_schema_write(self):
        path = self.fixture.root/'empty.sqlite3'
        with deployed.connect(path) as db: db.execute('CREATE TABLE untouched(value TEXT)')
        before = path.read_bytes()
        self.assertIsNone(failure_notification_status(path, {'id':'missing'}, None))
        self.assertEqual(before, path.read_bytes())

    def test_no_record_returns_none(self):
        self.fixture.add()
        self.assertIsNone(failure_notification_status(self.outbox, *self.body_ledger()))

    def test_original_preparation_event_keys_are_unchanged(self):
        for state in ('generation_failed', 'enqueue_failed'):
            with self.subTest(state=state):
                self.fixture.add(task=state, state=state)
                body, ledger = self.body_ledger(state)
                event = next(e for e in self.notifier.candidates() if e['task_id'] == state)
                self.assertEqual(event['event_key'], deployed_key(body, ledger))
                self.insert(event)
                dto = failure_notification_status(self.outbox, body, ledger)
                self.assertTrue(dto['is_current'])
                self.assertEqual(dto['event_id'], event['event_key'])
                self.assertEqual(len(dto['event_id']), 64)

    def test_original_publish_and_comment_event_keys_are_unchanged(self):
        for phase in ('upload','thumbnail','processing','public','comment'):
            self.fixture.add(task=phase,phase=phase,status='published' if phase=='comment' else 'failed',comment='unknown' if phase=='comment' else 'queued')
        for event in self.notifier.candidates():
            with self.subTest(stage=event['phase']):
                body, ledger = self.body_ledger(event['task_id'])
                self.assertEqual(event['event_key'], deployed_key(body,ledger))
                self.insert(event)
                dto = failure_notification_status(self.outbox,body,ledger)
                self.assertEqual(dto['stage'],event['phase'])
                self.assertTrue(dto['is_current'])

    def test_every_stored_state_is_read_only_and_no_receipt_is_exposed(self):
        self.fixture.add()
        body, ledger = self.body_ledger()
        event = self.notifier.candidates()[0]
        self.insert(event)
        for state in ('pending','sending','sent','failed','unknown','superseded'):
            with self.subTest(state=state):
                with self.notifier._outbox() as db: db.execute('UPDATE failure_notice SET state=?', (state,))
                before = self.outbox.read_bytes()
                dto = failure_notification_status(self.outbox,body,ledger)
                self.assertEqual(dto['status'],state)
                self.assertTrue(dto['message'])
                self.assertNotIn('message_id',dto)
                self.assertEqual(before,self.outbox.read_bytes())

    def test_current_event_wins_over_later_inserted_old_history(self):
        self.fixture.add()
        body, ledger = self.body_ledger()
        current = self.notifier.candidates()[0]
        old_ledger = dict(ledger,updated_at_utc='2026-09-09T10:00:00Z')
        old = dict(current,event_key=deployed_key(body,old_ledger),time=old_ledger['updated_at_utc'])
        self.insert(current,created=100)
        self.insert(old,state='unknown',created=200)
        dto = failure_notification_status(self.outbox,body,ledger)
        self.assertEqual(dto['event_id'],current['event_key'])
        self.assertEqual(dto['status'],'sent')
        self.assertTrue(dto['is_current'])
        recovered = dict(ledger,status='published',comment_status='published')
        history = failure_notification_status(self.outbox,body,recovered)
        self.assertEqual(history['event_id'],old['event_key'])
        self.assertFalse(history['is_current'])

    def test_other_tasks_do_not_leak_into_history(self):
        self.fixture.add()
        self.insert(self.notifier.candidates()[0])
        self.assertIsNone(failure_notification_status(self.outbox,{'id':'different-task','status':'published'},None))

    def test_locked_outbox_fails_fast_without_claiming_delivery_failure(self):
        self.fixture.add()
        self.insert(self.notifier.candidates()[0])
        lock = sqlite3.connect(self.outbox)
        try:
            lock.execute('BEGIN EXCLUSIVE')
            start = time.monotonic()
            self.assertIsNone(failure_notification_status(self.outbox,*self.body_ledger()))
            self.assertLess(time.monotonic()-start,.3)
        finally:
            lock.rollback();lock.close()

    def test_corrupt_current_payload_cannot_break_status_read(self):
        self.fixture.add()
        self.insert(self.notifier.candidates()[0])
        for payload in ('bad json', '[]', 'null'):
            with self.subTest(payload=payload):
                with self.notifier._outbox() as db: db.execute('UPDATE failure_notice SET payload=?',(payload,))
                self.assertIsNone(failure_notification_status(self.outbox,*self.body_ledger()))

    def test_candidate_recheck_error_defers_same_key_without_sending(self):
        self.fixture.add()
        candidates = self.notifier.candidates()
        before = self.fixture.db.read_bytes()
        with patch.object(self.notifier,'candidates',side_effect=[candidates,sqlite3.OperationalError('source busy')]):
            result = self.notifier.run_once()
        self.assertEqual(result['state'],'pending')
        self.assertTrue(result['deferred'])
        self.assertFalse(self.fixture.sent)
        with self.notifier._outbox() as db:
            row = db.execute('SELECT event_key,state,lease_until FROM failure_notice').fetchone()
            self.assertEqual(tuple(row),(candidates[0]['event_key'],'pending',0))
        self.assertEqual(self.notifier.run_once()['state'],'sent')
        self.assertFalse(self.notifier.run_once()['claimed'])
        self.assertEqual(len(self.fixture.sent),1)
        self.assertEqual(self.fixture.sent[0]['event_key'],candidates[0]['event_key'])
        self.assertEqual(before,self.fixture.db.read_bytes())


class WorkflowStatusTests(unittest.TestCase):
    def setUp(self):
        self.fixture = workflow.WorkflowCase()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.service = self.fixture.service
        self.outbox = self.fixture.root/'independent.sqlite3'
        self.service.failure_status = lambda body,ledger: failure_notification_status(self.outbox,body,ledger)

    def test_service_empty_cover_and_missing_notification_database(self):
        task = self.fixture.create()
        self.assertIsNone(task['cover_preview'])
        self.assertEqual(task['cover_url'],'')
        self.assertIsNone(task['failure_notification'])
        self.assertFalse(self.outbox.exists())

    def test_v2_failure_previews_only_v1_and_reads_existing_outbox_receipt(self):
        task = self.fixture.create()
        self.service.run_once()
        ready = self.service.get_task(self.fixture.actor,task['id'])['task']
        first_cover = ready['cover_url']
        self.service.review(self.fixture.actor,task['id'],{'action':'reject','version':1,'feedback':'QA change'})
        self.fixture.generate.side_effect = RuntimeError('QA failed generation')
        for _ in range(3):
            self.service.run_once()
            task = self.service.get_task(self.fixture.actor,task['id'])['task']
            if task['status']=='generation_failed':break
        self.assertEqual(task['status'],'generation_failed')
        self.assertEqual(task['current_version'],2)
        self.assertEqual(task['cover_url'],'')
        self.assertEqual(task['cover_preview'],{'url':first_cover,'version':1,'is_current':False})
        with self.service.db() as db: _,body = self.service._row(db,task['id'])
        key = deployed_key(body)
        event = {'task_id':task['id'],'phase':'cover','code':body['error']['code']}
        with deployed.connect(self.outbox) as db:
            db.execute('CREATE TABLE failure_notice(event_key TEXT PRIMARY KEY,payload TEXT,state TEXT,created_at REAL,message_id TEXT)')
            db.execute('INSERT INTO failure_notice VALUES(?,?,?,?,?)',(key,json.dumps(event),'sent',time.time(),'existing-receipt'))
        before = self.outbox.read_bytes()
        dto = self.service.get_task(self.fixture.actor,task['id'])['task']
        self.assertEqual(dto['failure_notification']['status'],'sent')
        self.assertEqual(dto['failure_notification']['event_id'],key)
        self.assertTrue(dto['failure_notification']['is_current'])
        self.assertEqual(before,self.outbox.read_bytes())


if __name__ == '__main__': unittest.main()
