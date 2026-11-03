from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, MagicMock

import cover
from meta_violation_cleanup.cover_store import CoverStore
from meta_violation_cleanup.runtime import Runtime
from meta_violation_cleanup.sql_source import SqlSourceError


class ScanRecoveryTests(unittest.TestCase):
    def test_disconnect_restarts_whole_scan_and_rehomes_only_unsent_partial_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=CoverStore(Path(tmp)/'cover.sqlite3')
            source=MagicMock()
            source.last_diagnostic={'id':'diagnostic','code':'sql_stream_incomplete','mysql_errors':[{'errno':2013}]}
            bids=[]
            candidate={'account_id':'100','ad_id':'200','old_creative_id':'300','user_id':'1'}
            def scan(adapter,ledger,bid,*args):
                bids.append(bid);ledger.begin_batch(bid,{})
                if len(bids)==2:
                    self.assertEqual(ledger.ready_tasks(),[])
                    self.assertEqual(ledger.known_keys(),set())
                self.assertTrue(ledger.enqueue(bid,candidate))
                if len(bids)==1:raise SqlSourceError('sql_stream_incomplete')
                ledger.seal_batch(bid);return {'new_ads':1}
            with patch.object(cover,'CoverSource',return_value=source), patch.object(cover,'scan',scan), \
                 patch.object(cover,'notify_completed'),patch.object(cover.time,'sleep'):
                cover.do_scan({},store,MagicMock(),lambda:False)
            self.assertEqual(len(set(bids)),2)
            self.assertEqual(store.get_task(1)['batch_id'],bids[1])
            self.assertEqual(store.get_task(1)['request_sent'],0)
            self.assertEqual(len(store.ready_tasks()),1)
            self.assertEqual(source.close.call_count,2)

    def test_retry_is_bounded_and_does_not_retry_statement_limits_or_data_errors(self):
        for errno,code,expected in ((2013,'sql_stream_incomplete',2),
                                    (3024,'sql_stream_incomplete',1),
                                    (2013,'ad_lineage_incomplete',1)):
            with self.subTest(errno=errno,code=code),tempfile.TemporaryDirectory() as tmp:
                store=CoverStore(Path(tmp)/'cover.sqlite3');source=MagicMock()
                source.last_diagnostic={'id':'diagnostic','code':'sql_stream_incomplete','mysql_errors':[{'errno':errno}]}
                bids=[]
                def scan(adapter,ledger,bid,*args):
                    bids.append(bid);ledger.begin_batch(bid,{})
                    raise SqlSourceError(code)
                with patch.object(cover,'CoverSource',return_value=source),patch.object(cover,'scan',scan), \
                     patch.object(cover,'notify_completed'),patch.object(cover.time,'sleep'):
                    with self.assertRaises(SqlSourceError):
                        cover.do_scan({},store,MagicMock(),lambda:False)
                self.assertEqual(len(bids),expected)
                self.assertTrue(all(b['status']=='failed' for b in store.get_batches()))


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
