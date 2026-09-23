"""Isolated real SQLite/filesystem tests; no production writes or network."""
import html
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.drama_synthesis.core import DramaSynthesisStore, ImmutableFilesystemPublisher
from features.youtube_auto_publish.manual_links import ManualLinks, DramaCatalog
from features.youtube_auto_publish.templates import WorkflowError


class ManualLinkTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.db=self.root/'db.sqlite3'
        self.store=DramaSynthesisStore(self.db);self.store.ensure_storage()
        (self.root/'public').mkdir();self.publisher=ImmutableFilesystemPublisher(self.root/'public')
        self.channels=Mock();self.channels.validate.return_value={'id':'12','channel_id':'UC'+'a'*22,'name':'频道 & A'}
        self.catalog=Mock();self.catalog.resolve.return_value={'content_id':'Abc1234567','language':'zh-tw','name':'繁體劇名 & Friends'}
        self.user=Mock(return_value='789')
        self.links=ManualLinks(self.db,self.publisher,self.channels,self.catalog,self.user)
        self.actor={'tenant_key':'tenant','user_id':'owner','role':'user'}
        self.payload={'operation_id':str(uuid.uuid4()),'channel_local_id':'12','content_id':'Abc1234567','language':'zh-TW'}

    def rows(self, table):
        with self.links.db() as c:return [dict(r) for r in c.execute('SELECT * FROM '+table)]

    def test_real_wrapper_and_eight_fields_without_publish_task(self):
        result=self.links.create(self.actor,self.payload)['link']
        self.assertEqual(result['status'],'published');row=self.rows('drama_material_short_link')[0]
        q=parse_qs(urlsplit(row['long_url']).query)
        self.assertEqual(set(q),{'c','af_adset','af_adset_id','af_ad','af_ad_id','af_channel','af_c_id','af_dp'})
        self.assertEqual(q['af_channel'],['789']);self.assertEqual(q['af_dp'],['Abc1234567'])
        self.assertEqual(q['af_c_id'],['yt_manual_'+self.payload['operation_id']])
        self.assertEqual(q['af_ad_id'],['none']);self.assertIn('*none*manual_1',q['c'][0])
        self.assertIn('nonezh-tw*繁體劇名 & Friends',q['c'][0])
        self.assertIn(html.escape(row['long_url'],quote=True),(self.root/'public/1.html').read_text())
        self.assertEqual(self.rows('drama_youtube_publish'),[])

    def test_repeat_operation_reuses_even_after_channel_revoked(self):
        first=self.links.create(self.actor,self.payload)
        self.channels.validate.side_effect=AssertionError('must return already completed link')
        self.assertEqual(self.links.create(self.actor,self.payload),first)
        self.assertEqual(len(self.rows('drama_material_short_link')),1)

    def test_new_operation_new_link(self):
        first=self.links.create(self.actor,self.payload)
        second=self.links.create(self.actor,dict(self.payload,operation_id=str(uuid.uuid4())))
        self.assertNotEqual(first['link']['short_url'],second['link']['short_url'])

    def test_concurrent_same_operation_allocates_once(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results=list(pool.map(lambda _:self.links.create(self.actor,self.payload),range(4)))
        self.assertEqual(len({r['link']['short_url'] for r in results}),1)
        self.assertEqual(len(self.rows('drama_material_short_link')),1)

    def test_owner_tenant_and_admin_cannot_read_other_operation(self):
        self.links.create(self.actor,self.payload)
        for actor in [dict(self.actor,user_id='other'),dict(self.actor,tenant_key='other'),dict(self.actor,user_id='admin',role='admin')]:
            with self.assertRaises(WorkflowError) as cm:self.links.get(actor,self.payload['operation_id'])
            self.assertEqual(cm.exception.status,404)

    def test_operation_conflict_does_not_overwrite(self):
        first=self.links.create(self.actor,self.payload)
        with self.assertRaises(WorkflowError):self.links.create(self.actor,dict(self.payload,content_id='Other12345'))
        self.assertEqual(self.links.get(self.actor,self.payload['operation_id']),first)

    def test_failure_reserves_once_then_retries_exact_bytes(self):
        self.links.publisher=Mock();self.links.publisher.publish.side_effect=OSError('private path/secret')
        with self.assertRaises(WorkflowError) as cm:self.links.create(self.actor,self.payload)
        self.assertNotIn('secret',str(cm.exception))
        self.assertEqual(self.links.get(self.actor,self.payload['operation_id'])['link']['short_url'],'')
        old=self.rows('drama_material_short_link')[0]
        self.links.publisher=self.publisher;self.user.return_value='900'
        self.links.create(self.actor,self.payload)
        self.assertEqual(self.rows('drama_material_short_link')[0]['long_url'],old['long_url'])
        self.assertEqual(len(self.rows('drama_material_short_link')),1)

    def test_file_written_before_response_loss_recovers_same_link(self):
        def lost(*args):self.publisher.publish(*args);raise OSError('lost')
        self.links.publisher=Mock();self.links.publisher.publish.side_effect=lost
        with self.assertRaises(WorkflowError):self.links.create(self.actor,self.payload)
        saved=(self.root/'public/1.html').read_bytes()
        self.links.publisher=self.publisher;self.links.create(self.actor,self.payload)
        self.assertEqual((self.root/'public/1.html').read_bytes(),saved)

    def test_revoked_channel_missing_drama_bad_mapping_allocate_nothing(self):
        for adapter in [self.channels.validate,self.catalog.resolve,self.user]:
            adapter.side_effect=WorkflowError('blocked','blocked',409)
            with self.assertRaises(WorkflowError):self.links.create(self.actor,self.payload)
            self.assertEqual(self.rows('drama_material_short_link'),[])
            adapter.side_effect=None
        self.user.return_value='non-numeric'
        with self.assertRaises(WorkflowError):self.links.create(self.actor,self.payload)
        self.assertEqual(self.rows('drama_material_short_link'),[])

    def test_invalid_input_allocate_nothing(self):
        for key,value in [('operation_id','bad'),('channel_local_id','1 OR 1'),('content_id','../x'),('language','en*fake')]:
            with self.assertRaises(WorkflowError):self.links.create(self.actor,dict(self.payload,**{key:value}))
        self.assertEqual(self.rows('drama_material_short_link'),[])

    def test_shared_id_and_old_links_immutable(self):
        old=self.store.ensure_short_link('b'*32,'concat_video','Legacy1',self.publisher)
        before=(self.root/'public/1.html').read_bytes()
        result=self.links.create(self.actor,self.payload)['link']
        self.assertTrue(result['short_url'].endswith('/2.html'))
        self.assertEqual((self.root/'public/1.html').read_bytes(),before)
        self.assertEqual(self.store.short_link('b'*32,'concat_video')['long_url'],old['long_url'])

    def test_conflicting_existing_file_not_overwritten(self):
        (self.root/'public/1.html').write_bytes(b'keep')
        with self.assertRaises(WorkflowError):self.links.create(self.actor,self.payload)
        self.assertEqual((self.root/'public/1.html').read_bytes(),b'keep')
        self.assertEqual(self.links.get(self.actor,self.payload['operation_id'])['link']['status'],'failed')


class CatalogTests(unittest.TestCase):
    def setUp(self):self.query=Mock(return_value=[]);self.catalog=DramaCatalog(self.query)
    @staticmethod
    def row(**kwargs):return (json.dumps(kwargs).encode().hex(),)
    def test_empty_open_no_database_scan(self):
        self.assertTrue(self.catalog.search()['search_required']);self.query.assert_not_called()
    def test_search_boundaries_and_literal_wildcards(self):
        self.catalog.search("a%'_!",2);sql=self.query.call_args.args[0]
        self.assertIn('FORCE INDEX(name)',sql);self.assertIn('FORCE INDEX(content_id)',sql)
        self.assertIn('LIMIT 51 OFFSET 50',sql);self.assertNotIn("a%'_!",sql)
        for value in [0,-1,1001]:
            with self.assertRaises(WorkflowError):self.catalog.search('hello',value)
    def test_pagination_conflicts_and_language(self):
        self.query.return_value=[self.row(content_id=str(i),language='en',name='Drama',ambiguous=i==0) for i in range(51)]
        data=self.catalog.search('Drama');self.assertEqual(len(data['items']),50);self.assertTrue(data['has_more'])
        self.assertFalse(data['items'][0]['selectable'])
    def test_exact_id_case_and_conflicts_fail_closed(self):
        self.query.return_value=[self.row(content_id='ABC',language='en',name='One')]
        with self.assertRaises(WorkflowError):self.catalog.resolve('abc','en')
        self.query.return_value += [self.row(content_id='ABC',language='en',name='Two')]
        with self.assertRaises(WorkflowError):self.catalog.resolve('ABC','en')
    def test_query_failure_sanitized(self):
        self.query.side_effect=RuntimeError('secret')
        with self.assertRaises(WorkflowError) as cm:self.catalog.search('hello')
        self.assertNotIn('secret',str(cm.exception))


if __name__=='__main__':unittest.main()
