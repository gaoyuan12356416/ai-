import json
import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from features.fb_video_publish.sources import SourceRepository
from features.story_copy import alerts, sources, store, worker
from tests import test_story_copy as fixtures
from ops.story_copy.recover_mapping import safe, metadata


class Catalog:
    schema = 'kunlunads_dev'
    blacklist_schema = 'ads_setting'

    def __init__(self, descriptions=None):
        self.calls = []
        self.descriptions = descriptions or [dict(app_id=1479, content_id='drama', language='ja', drama_description='正しい物語')]

    def select(self, sql, params):
        self.calls.append((sql, params))
        if 'ads_drama_info' in sql:
            return [dict(app_id=1479, content_id='drama', language='ja', drama_name='正しい劇名')]
        return self.descriptions


class MetadataMappingTests(unittest.TestCase):
    def test_social_bucket_reads_dramawave_without_rewriting_identity(self):
        catalog = Catalog()
        repo = SourceRepository(None, SimpleNamespace(mysql=catalog))
        rows = [dict(source_app_id=3214, content_id='drama', language='ja'),
                dict(source_app_id=3360, content_id='drama', language='ja'),
                dict(source_app_id=3214, content_id='drama', language='en')]
        result = repo._enrich(rows, strict=False, need_desc=True, manual=True)
        self.assertEqual(result[0]['source_app_id'], 3214)
        self.assertEqual(result[0]['description_status'], 'matched')
        self.assertEqual(result[0]['drama_name'], '正しい劇名')
        self.assertEqual([r['description_status'] for r in result[1:]], ['missing', 'missing'])
        for sql, _ in catalog.calls:
            self.assertIn('IN (1479,3360)', sql)
            self.assertNotIn('3214', sql)

    def test_ambiguous_metadata_stays_empty(self):
        catalog = Catalog([dict(app_id=1479, content_id='drama', language='ja', drama_description=text) for text in ['one', 'two']])
        repo = SourceRepository(None, SimpleNamespace(mysql=catalog))
        row = repo._enrich([dict(source_app_id=3214, content_id='drama', language='ja')], strict=False, need_desc=True, manual=True)[0]
        self.assertEqual(row['description_status'], 'ambiguous')
        self.assertEqual(row['drama_description'], '')


class PublishBatchAlertTests(unittest.TestCase):
    setUp = fixtures.CaptionTests.setUp
    task = fixtures.CaptionTests.task
    def test_native_batch_survives_task_adapter(self):
        row = dict(_ord=1,task_id='one',batch_id='native',content_id='drama',material_id='3',snapshot_json='{}')
        self.assertEqual(sources.fb_task('fv',row)['publish_batch'],'fb:video:native')

    def test_waits_and_combines_more_than_eight_generation_batches(self):
        with store.connection() as c:
            tasks = [self.task(i) for i in range(1, 11)]
            for i,t in enumerate(tasks):
                t['publish_batch']='fb:video:native'
                store.enqueue(c,t)
                c.execute("UPDATE jobs SET state=? WHERE key=?", ('generating' if i==9 else 'failed',t['key']))
                if i<9: alerts.add(c,'model:'+str(i),[worker.alert_item(t,'insufficient_evidence')])
            c.commit()
            sent=[]
            alerts.deliver(c,lambda *args: sent.append(args) or 'ok')
            self.assertEqual(sent,[])
            c.execute("UPDATE jobs SET state='failed' WHERE key=?",(tasks[-1]['key'],))
            alerts.add(c,'model:9',[worker.alert_item(tasks[-1],'quota_unavailable')])
            c.commit()
            alerts.deliver(c,lambda *args: sent.append(args) or 'ok')
            self.assertEqual(len(sent),1)
            self.assertIn('影响 10 条',sent[0][1])
            self.assertEqual(c.execute('SELECT count(*) FROM alerts WHERE sent IS NOT NULL').fetchone()[0],10)
            alerts.deliver(c,lambda *_: self.fail('resent delivered batch'))

    def test_mixed_submissions_remain_separate_and_timeout_reuses_packet(self):
        with store.connection() as c:
            items=[]
            for i in range(2):
                t=self.task(i+1);t['publish_batch']='fb:video:'+str(i)
                items.append(worker.alert_item(t,'insufficient_evidence'))
            alerts.add(c,'model:1',items);c.commit()
            packets=[]
            def timeout(event,body):
                packets.append((event,body))
                raise TimeoutError()
            alerts.deliver(c,timeout)
            self.assertEqual(len(packets),2)
            c.execute('UPDATE alerts SET next_try=0');c.commit()
            retries=[]
            alerts.deliver(c,lambda event,body: retries.append((event,body)) or 'ok')
            self.assertEqual(retries,packets)


class RecoveryAdmissionTests(unittest.TestCase):
    def test_operator_metadata_adapter_matches_repository_contract(self):
        class Connection:
            def cursor(self): return self
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def execute(self,sql,params=()): self.sql=sql
            def fetchone(self): return {'ro':1}
            def fetchall(self):
                field='drama_name' if 'ads_drama_info' in self.sql else 'drama_description'
                return [dict(app_id=1479,content_id='drama',language='en',**{field:'Verified facts'})]
            def rollback(self): pass
            def close(self): pass
        with patch.dict('sys.modules',{'pymysql':SimpleNamespace(connect=lambda **kw:Connection(),cursors=SimpleNamespace(DictCursor=object))}), patch('pathlib.Path.read_text',return_value='FB_AUTO_MYSQL_USER=fixture\nFB_AUTO_MYSQL_PASSWORD=fixture\n'):
            result=metadata([dict(material_id='1',source_app_id=3214,content_id='drama',language='en')])
        self.assertEqual(result[0]['description_status'],'matched')
        self.assertEqual(result[0]['source_app_id'],3214)

    def test_only_owned_hold_without_any_publish_evidence_can_recover(self):
        c=sqlite3.connect(':memory:');self.addCleanup(c.close);c.row_factory=sqlite3.Row
        c.executescript('CREATE TABLE pipeline_canonical_hold(source_key TEXT,reason TEXT);'
                       'CREATE TABLE fb_video_attempt(task_id TEXT);CREATE TABLE story_copy_receipt(task_key TEXT);')
        record=dict(batch='batch',reason='repair')
        task=dict(task_id='one',batch_id='batch',status='queued',graph_post_id='',unknown_outcome=0,execution_lease='')
        self.assertFalse(safe(c,task,record))
        c.execute("INSERT INTO pipeline_canonical_hold VALUES('fb:video:one','repair')")
        self.assertTrue(safe(c,task,record))
        for field,value in [('status','published'),('graph_post_id','123'),('unknown_outcome',1),('execution_lease','active'),('batch_id','other')]:
            self.assertFalse(safe(c,dict(task,**{field:value}),record))
        c.execute("INSERT INTO fb_video_attempt VALUES('one')")
        self.assertFalse(safe(c,task,record))
        c.execute('DELETE FROM fb_video_attempt')
        c.execute("INSERT INTO story_copy_receipt VALUES('fb:video:one')")
        self.assertFalse(safe(c,task,record))


if __name__ == '__main__':
    unittest.main()
