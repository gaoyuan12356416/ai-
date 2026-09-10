import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.youtube_auto_publish.failure_notifications import FailureNotifications, build_failure_notifications

@contextmanager
def connect(path):
    db=sqlite3.connect(path)
    try:
        with db:yield db
    finally:db.close()

class FailureNoticeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / 'source.sqlite3'
        with connect(self.db) as db:
            db.executescript('''CREATE TABLE youtube_auto_preparation(id TEXT,version INT,body TEXT,state TEXT,created_at TEXT,updated_at TEXT);
                CREATE TABLE drama_youtube_publish(preparation_id TEXT,workflow TEXT,status TEXT,comment_status TEXT,reviewed_phase TEXT,
                error_code TEXT,error_message TEXT,updated_at_utc TEXT,video_id TEXT);''')
        self.sent = []
        def send(event):
            self.sent.append(event)
            return 'message-' + event['event_key'][:8]
        self.notifier = FailureNotifications(self.db, self.root/'outbox.sqlite3', send)

    def tearDown(self):
        self.tmp.cleanup()

    def add(self, task='task-1', state='uploading', phase='thumbnail', status='unknown', comment='queued', workflow='reviewed_thumbnail'):
        body={'title':'测试任务','channel':{'name':'测试频道'},'creator':{'open_id':'owner-1','user_id':'u1'},
              'error':{'code':'cover_invalid','message':'图片解码失败'}}
        with connect(self.db) as db:
            db.execute('INSERT INTO youtube_auto_preparation VALUES(?,2,?,?,?,?)',(task,json.dumps(body),state,'2026-09-10T10:00:00Z','2026-09-10T11:00:00Z'))
            if state == 'uploading':
                db.execute('INSERT INTO drama_youtube_publish VALUES(?,?,?,?,?,?,?,?,?)',
                    (task,workflow,status,comment,phase,'youtube_video_reconcile_unknown','YouTube视频状态无法确认','2026-09-10T11:00:00Z','video-id'))

    def test_current_failure_is_sent_once_and_source_unchanged(self):
        self.add()
        before=self.db.read_bytes()
        self.assertEqual(self.notifier.run_once()['state'],'sent')
        self.assertFalse(self.notifier.run_once()['claimed'])
        self.assertEqual(len(self.sent),1)
        self.assertEqual(self.sent[0]['receive_id'],'owner-1')
        self.assertTrue(self.sent[0]['unknown'])
        self.assertEqual(before,self.db.read_bytes())

    def test_every_publication_phase(self):
        for phase in ('upload','thumbnail','processing','public','comment'):
            self.add(task=phase,phase=phase,status='failed')
        for _ in range(5):self.notifier.run_once()
        self.assertEqual({e['phase'] for e in self.sent},{'upload','thumbnail','processing','public','comment'})

    def test_generation_and_enqueue_failures(self):
        self.add(task='gen',state='generation_failed')
        self.add(task='enqueue',state='enqueue_failed')
        self.notifier.run_once();self.notifier.run_once()
        self.assertEqual({e['phase'] for e in self.sent},{'cover','enqueue'})

    def test_no_success_review_or_legacy_notifications(self):
        self.add(task='done',status='published',comment='published')
        self.add(task='review',state='review')
        self.add(task='legacy',workflow='legacy')
        self.assertFalse(self.notifier.run_once()['claimed'])

    def test_comment_failure_on_published_video(self):
        self.add(status='published',comment='failed',phase='comment')
        self.notifier.run_once()
        self.assertEqual(self.sent[0]['phase'],'comment')
        self.assertFalse(self.sent[0]['unknown'])

    def test_retry_failure_new_event(self):
        self.add();self.notifier.run_once()
        with connect(self.db) as db:db.execute("UPDATE drama_youtube_publish SET updated_at_utc='2026-09-10T11:20:00Z'")
        self.notifier.run_once()
        self.assertEqual(len(self.sent),2)
        self.assertNotEqual(self.sent[0]['event_key'],self.sent[1]['event_key'])

    def test_delivery_unknown_not_blindly_replayed(self):
        self.add()
        def fail(event):self.sent.append(event);raise TimeoutError()
        self.notifier.sender=fail
        self.assertEqual(self.notifier.run_once()['state'],'unknown')
        self.notifier.run_once()
        self.assertEqual(len(self.sent),1)

    def test_expired_send_unknown(self):
        self.add();event=self.notifier.candidates()[0]
        with self.notifier._outbox() as db:
            db.execute("INSERT INTO failure_notice(event_key,payload,state,lease_until,created_at) VALUES(?,?,'sending',?,?)",
                (event['event_key'],json.dumps(event),time.time()-1,time.time()-10))
        self.assertFalse(self.notifier.run_once()['claimed'])
        with self.notifier._outbox() as db:self.assertEqual(db.execute('SELECT state FROM failure_notice').fetchone()[0],'unknown')
        self.assertFalse(self.sent)

    def test_recovered_pending_is_superseded(self):
        self.add();event=self.notifier.candidates()[0]
        with self.notifier._outbox() as db:
            db.execute("INSERT INTO failure_notice(event_key,payload,state,created_at) VALUES(?,?,'pending',?)",(event['event_key'],json.dumps(event),time.time()))
        with connect(self.db) as db:db.execute("UPDATE drama_youtube_publish SET status='processing'")
        self.assertEqual(self.notifier.run_once()['state'],'superseded')
        self.assertFalse(self.sent)

    def test_outbox_cannot_be_the_source(self):
        with self.assertRaises(ValueError):FailureNotifications(self.db,self.db,lambda e:'bad')

    def test_real_sender_contract_uses_owner_receipt_and_stable_uuid(self):
        self.add()
        calls=[]
        class Session:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def post(self,url,**kwargs):
                calls.append((url,kwargs))
                return SimpleNamespace(status_code=200,json=lambda:{'code':0,'data':{'message_id':'receipt-1'}})
        app=SimpleNamespace(JOB_DB_PATH=self.db,PUBLIC_BASE_URL='https://example.test/drama-materials',
            FEISHU_MESSAGE_URL='https://open.feishu.cn/open-apis/im/v1/messages',get_feishu_tenant_access_token=lambda:'test-only')
        with patch.dict('os.environ',{'YOUTUBE_AUTO_STORAGE_ROOT':str(self.root/'sender')}),patch('features.youtube_auto_publish.failure_notifications.requests.Session',Session):
            notifier=build_failure_notifications(app)
            self.assertEqual(notifier.run_once()['state'],'sent')
            self.assertFalse(notifier.run_once()['claimed'])
        payload=calls[0][1]['json'];card=json.loads(payload['content'])
        self.assertEqual(payload['receive_id'],'owner-1')
        self.assertEqual(len(payload['uuid']),32)
        self.assertEqual(card['header']['template'],'red')
        self.assertIn('上传后视频状态核对',card['elements'][0]['text']['content'])
        self.assertEqual(card['elements'][1]['actions'][0]['url'],'https://example.test/youtube-publish.html?task_id=task-1')


if __name__ == '__main__':unittest.main()
