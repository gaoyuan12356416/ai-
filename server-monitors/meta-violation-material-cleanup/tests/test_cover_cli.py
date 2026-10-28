from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import cover
from meta_violation_cleanup.cover_store import CoverStore
from meta_violation_cleanup.runtime import Runtime


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.store=CoverStore(Path(self.folder.name)/'cover.sqlite3')
        self.audit=Runtime(Path(self.folder.name)/'audit')
        self.addCleanup(self.audit.close)
        self.store.begin_batch('batch',{'new_ads':2})
        for aid in ('200','201'):
            self.store.enqueue('batch',{'account_id':'100','ad_id':aid,'old_creative_id':'300',
                                       'root_id':'1479','material_ids':['400'],'user_id':'803'})
        self.store.seal_batch('batch')
        self.tasks=self.store.ready_tasks()
        self.store.claim_task(self.tasks[0]['id'])
        self.store.finish_task(self.tasks[0]['id'],'failed',{'reason':'known_failure'})

    def test_first_cohort_reports_remaining_backlog(self):
        sent=[]
        class Sender:
            def __init__(self,*args): pass
            def send(self,bid,phase,body): sent.append(body)
        with patch.object(cover,'Notifier',Sender):
            cover._notify_completed(self.store,self.audit,{'feishu_config':'unused'},include_pending=True)
            cover._notify_completed(self.store,self.audit,{'feishu_config':'unused'},include_pending=True)
        self.assertEqual(len(sent),1)
        self.assertIn('广告积压：1',sent[0])
        self.assertIn('失败 1',sent[0])

    def test_retry_marks_only_the_counts_in_frozen_message(self):
        audit=self.audit
        sent=[]
        fail=[True]
        class Sender:
            def __init__(self,*args): pass
            def send(self,bid,phase,body):
                audit.claim_notice(bid,phase,body)
                audit.start_notice(bid,phase)
                if fail[0]:
                    audit.finish_notice(bid,phase,'failed',{'code':1})
                    raise RuntimeError('delivery_failed')
                audit.finish_notice(bid,phase,'sent',{'code':0,'message_id':'fixture'})
                sent.append(body)
        with patch.object(cover,'Notifier',Sender):
            cover._notify_completed(self.store,audit,{'feishu_config':'unused'},True)
            self.store.claim_task(self.tasks[1]['id'])
            self.store.finish_task(self.tasks[1]['id'],'failed',{'reason':'another_failure'})
            audit.db.execute("UPDATE notices SET updated='2020-01-01T00:00:00+08:00'")
            audit.db.commit()
            fail[0]=False
            cover._notify_completed(self.store,audit,{'feishu_config':'unused'},True)
            cover._notify_completed(self.store,audit,{'feishu_config':'unused'},True)
        self.assertEqual(len(sent),2)
        self.assertIn('失败 1',sent[0])
        self.assertIn('失败 2',sent[1])


if __name__=='__main__': unittest.main()
