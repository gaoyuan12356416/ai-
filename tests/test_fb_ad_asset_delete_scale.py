import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from features.fb_ad_asset_delete.graph import GraphClient, GraphError, AD_FIELDS
from features.fb_ad_asset_delete.store import Store
from features.fb_ad_asset_delete.service import Service
from features.fb_ad_asset_delete.source import SqlSource, q
from test_fb_ad_asset_delete_core_graph import Response, FakeTransport, account_probe, obj


class BatchReadTests(unittest.TestCase):
    def test_batch_uses_exact_ids_and_one_use_data_then_fresh_read(self):
        entries = [dict(obj('ad', str(i)), creative_ids=['201']) for i in (101, 102)]
        nodes = {o['object_id']: dict(id=o['object_id'], account_id='444', creative={'id':'201'}) for o in entries}
        transport = FakeTransport([account_probe(), Response(nodes), Response(nodes['101'])])
        graph = GraphClient(lambda users: 'synthetic-token', transport=transport)
        graph.prefetch_preview_nodes(entries)
        for item in entries:
            self.assertEqual(graph.inspect(item, check_references=False)[0], 'pending')
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(transport.calls[1]['params']['ids'], '101,102')
        graph.inspect(entries[0], check_references=False)
        self.assertEqual(len(transport.calls), 3)
        self.assertTrue(all(c['method']=='GET' for c in transport.calls))

    def test_batch_errors_and_account_mismatch_stay_blocked(self):
        entries = [dict(obj('ad', str(i)), creative_ids=['201']) for i in (101, 102)]
        transport = FakeTransport([account_probe(), Response({'101': {'error': {'code': 100, 'message':'denied'}},
            '102': {'id':'102', 'account_id':'999', 'creative':{'id':'201'}}})])
        graph = GraphClient(lambda users:'synthetic-token', transport=transport)
        graph.prefetch_preview_nodes(entries)
        for item, code in zip(entries, ('100', 'account_mismatch')):
            with self.assertRaises(GraphError) as caught:
                graph.inspect(item, check_references=False)
            self.assertEqual(caught.exception.code, code)

    def test_root_path_is_restricted_to_bounded_read_only_ids(self):
        transport = FakeTransport([])
        graph = GraphClient(lambda users:'synthetic-token', transport=transport)
        for method, ids in [('DELETE','101'), ('POST','101'), ('GET','101/ads'), ('GET', ','.join(str(n) for n in range(1,52)))]:
            with self.assertRaises(GraphError):
                graph.request(method, '', 'synthetic-token', {'ids':ids, 'fields':AD_FIELDS})
        self.assertEqual(transport.calls, [])


class PageTests(unittest.TestCase):
    def test_only_page_payloads_are_decoded_and_full_counts_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp)/'tasks.sqlite3')
            objects = [dict(kind='video', object_id=str(n), status='blocked' if n%2 else 'deleted') for n in range(1,1002)]
            store.create_job(dict(job_id='job', preview_id='preview', actor='test', products=[], objects=objects))
            with patch.object(store, '_object_data', wraps=store._object_data) as decode:
                page = store.get_job_page('job', 2, 50, 'video', 'pending')
                self.assertEqual(decode.call_count, 50)
            public = Service._public(page)
            self.assertEqual(public['total'], 501)
            self.assertEqual(public['summary']['total'], 1001)
            self.assertEqual(len(public['objects']), 50)
            self.assertEqual(public['objects'][0]['object_id'], '101')
            self.assertEqual(store.get_job_page('job',1,50,'video','blocked')['total'],0)


class ReferenceBatchTests(unittest.TestCase):
    def test_account_scoped_batches_keep_cross_product_references_and_all_ids(self):
        calls=[]
        source=SqlSource(lambda sql,timeout: [])
        def read(sql,columns,timeout):
            calls.append(sql)
            return [{'ad_id':'999','product_id':'outside','account_id':'444','creative_id':'1001','video_ids_raw':''}] if q('1001') in sql else []
        source.read=read
        objects=[dict(kind='creative',object_id=str(1000+n),account_ids=['444'],account_verified=True) for n in range(1,502)]
        objects.append(dict(kind='creative',object_id='9001',account_ids=['555'],account_verified=True))
        refs=source.shared_references(objects)
        self.assertEqual(len(calls),3)
        self.assertEqual([(r['key'],r['product_id']) for r in refs],[('creative:1001','outside')])
        for sql in calls:
            self.assertIn('FORCE INDEX (ad_account_id)',sql)
            self.assertNotIn('product IN',sql)
            self.assertFalse(q('444') in sql and q('555') in sql)
        for obj in objects:
            self.assertTrue(any(q(obj['object_id']) in sql for sql in calls))


if __name__ == '__main__':
    unittest.main()
