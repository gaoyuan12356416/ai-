import copy
import datetime as dt
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from collector import local_time, map_lineage, Catalog, FIELDS
from analysis import metrics, build_report, validate_pool


def material(identity='1',created='2026-10-18 10:00:00',source='cover_synthesis',backfill=False,deleted=0):
    return dict(id=identity,name='test',url='https://example.com/image.jpg',source=source,owner='张三',content_id='drama',
                language='en',ratio='16:9',category='16:9',created_at=created,generated_at='2026-09-01 10:00:00',is_backfill=backfill,is_delete=deleted,
                product='Drama-AI素材',type=1,data_source=6,data_source_id='drama',content_sign='',identity=identity)


def fact(kind,identity='1',day='2026-10-18',spend=10,revenue=5,**extra):
    row=dict(kind=kind,id=identity,dt=day,spend=spend,af_revenue_d0=revenue,impressions=1000,clicks=50,installs=5,af_installs=4,purchases=2,
             product='Drama-AI素材',platform=0,app_id='Dramawave',optimizer=123,resource_type=1,latest_updated='2026-10-19 08:45:00')
    row.update(extra);return row


def facts():
    return [fact('ai'),fact('pool'),fact('market',spend=30,revenue=12,impressions=3000,clicks=150,installs=15,af_installs=12,purchases=6)]


class AnalysisTests(unittest.TestCase):
    def report(self,mapped=None,rows=None):
        return build_report(mapped or {'1':material()},[],facts() if rows is None else rows,'2026-10-18','2026-10-19T09:00:00+08:00',{'123':'李四'})

    def test_aggregate_ratios_not_average(self):
        m=metrics([dict(spend=10,af_revenue_d0=10),dict(spend=90,af_revenue_d0=0)])
        self.assertEqual(m['roas'],10)

    def test_zero_denominators(self):
        m=metrics([])
        self.assertIsNone(m['roas']);self.assertIsNone(m['ctr']);self.assertIsNone(m['cpi'])

    def test_backfill_day_not_original_creation(self):
        r=self.report({'1':material(),'2':material('2','2026-10-19 16:00:00','youtube_cover',True)})
        self.assertEqual(r['summary']['production']['new_count'],1)
        self.assertEqual(len(r['materials']),1)
        self.assertEqual(r['summary']['production']['backfill_count'],0)

    def test_backfill_on_upload_day(self):
        r=self.report({'1':material(source='youtube_cover',backfill=True)})
        self.assertEqual(r['summary']['production']['backfill_count'],1)

    def test_zero_spend_counts_as_production(self):
        r=self.report({'1':material(),'2':material('2')})
        self.assertEqual(r['summary']['production']['new_count'],2)
        self.assertEqual(r['summary']['performance']['spending_materials'],1)

    def test_deleted_keeps_spend(self):
        r=self.report({'1':material(deleted=1)})
        self.assertEqual(r['summary']['performance']['spend'],10)
        self.assertEqual(r['materials'][0]['is_delete'],1)

    def test_owner_and_optimizer_separate(self):
        r=self.report()
        self.assertEqual(r['groups']['owners'][0]['name'],'张三')
        self.assertEqual(r['groups']['optimizers'][0]['name'],'李四')

    def test_other_images_excludes_ai(self):
        r=self.report()
        self.assertEqual(r['comparisons']['other_images']['spend'],20)
        self.assertEqual(r['comparisons']['other_images']['af_revenue_d0'],7)

    def test_negative_comparison_fails(self):
        rows=facts();rows[-1]['spend']=1
        with self.assertRaisesRegex(ValueError,'negative_comparison'):
            self.report(rows=rows)

    def test_unmatched_pool_not_in_ai(self):
        r=self.report(rows=facts()+[fact('pool','99')])
        self.assertEqual(r['summary']['performance']['spend'],10)
        self.assertEqual(r['validation']['unknown_spending_materials'],1)

    def test_pool_mismatch_blocks_publish(self):
        rows=facts();rows[1]['spend']=11
        with self.assertRaisesRegex(ValueError,'pool_conservation'):
            self.report(rows=rows)

    def test_pre_ingestion_spend_rejected(self):
        with self.assertRaisesRegex(ValueError,'spend_precedes'):
            self.report({'1':material(created='2026-10-19 10:00:00')})

    def test_trend_revenue_backfill_visible(self):
        rows=facts()+[fact('ai',day='2026-10-17',revenue=8),fact('pool',day='2026-10-17',revenue=8),fact('market',day='2026-10-17',spend=30,revenue=15)]
        r=self.report(rows=rows)
        self.assertEqual(r['trend'][-2]['ai']['af_revenue_d0'],8)

    def test_timezone_cross_day(self):
        self.assertEqual(local_time('2026-10-18T20:00:00Z'),'2026-10-19 04:00:00')


class LineageTests(unittest.TestCase):
    def fixtures(self):
        job=dict(job_id='j1',content_id='drama',creator_name='张三',created_at='2026-10-18 01:00:00',finished_at='2026-10-18 01:10:00',square_1x1_url='https://x/image.jpg')
        mid=str(int(hashlib.sha1(b'j1:square_1x1').hexdigest()[:15],16))
        intake=dict(id=99,type=2,data_source_id=mid,name='test',category='16:9',remark='1')
        return job,intake,material()

    def test_intake_is_not_a_material(self):
        j,i,f=self.fixtures();m,u=map_lineage([j],[i],[f],[],[])
        self.assertEqual(list(m),['1']);self.assertFalse(u)

    def test_legacy_name_fallback(self):
        j,i,f=self.fixtures();i['remark']='ai drama';m,u=map_lineage([j],[i],[f],[],[])
        self.assertEqual(len(m),1)

    def test_wrong_drama_fails_closed(self):
        j,i,f=self.fixtures();f['data_source_id']='other';m,u=map_lineage([j],[i],[f],[],[])
        self.assertFalse(m);self.assertEqual(len(u),1)

    def test_conflicting_sources_not_double_counted(self):
        j,i,f=self.fixtures()
        y=dict(key='yt1',state='confirmed',final_id=1,material_id='external',source=json.dumps(dict(content_id='drama',owner_name='other',asset_created_at='2026-10-18T01:00:00Z',source_scope='approved')))
        m,u=map_lineage([j],[i],[f],[y],[])
        self.assertFalse(m);self.assertIn('多个',u[0]['reason'])

    def test_unconfirmed_youtube_excluded(self):
        y=dict(key='yt1',state='accepted',final_id=None,material_id='external',source=json.dumps(dict(content_id='drama',title='title')))
        m,u=map_lineage([],[],[],[y],[])
        self.assertFalse(m);self.assertEqual(len(u),1)

    def test_generic_fallback_deduplicates_catalog_rows(self):
        f=material();f['content_sign']='asset'
        a=dict(asset_id='asset',source_api_id='',name='test',created_at='2026-10-18 00:00:00',creator_name='张三')
        m,u=map_lineage([],[],[f,copy.deepcopy(f)],[],[a])
        self.assertEqual(list(m),['1']);self.assertFalse(u)


class CatalogTests(unittest.TestCase):
    def test_write_endpoint_rejected(self):
        with self.assertRaises(ValueError):
            Catalog(dict(ADMIN_MAPPING_MYSQL_HOST='101.32.56.53',ADMIN_MAPPING_MYSQL_PORT='63353'))

    def test_failed_read_never_becomes_empty(self):
        cat=Catalog(dict(ADMIN_MAPPING_MYSQL_HOST='101.32.56.53',ADMIN_MAPPING_MYSQL_PASSWORD='secret',ADMIN_MAPPING_MYSQL_USER='user'))
        with patch('collector.subprocess.run') as run:
            run.return_value.returncode=1;run.return_value.stderr='ERROR 3024 timeout'
            with self.assertRaisesRegex(RuntimeError,'code_3024'):
                cat.query('SELECT 1')
            self.assertEqual(run.call_args[0][0][0],'/usr/bin/mysql')
            self.assertNotIn('secret',' '.join(run.call_args[0][0]))
            self.assertIn('TRANSACTION READ ONLY',run.call_args[1]['input'])


class PublishTests(unittest.TestCase):
    def fixture(self,state):
        from report import write_json,sha
        run=state/'runs'/'r1';run.mkdir(parents=True)
        (run/'index.html').write_bytes(b'<html>daily report</html>')
        report=dict(generated_at='2026-10-19T09:00:00+08:00',validation={'status':'passed'},summary={})
        write_json(run/'report.json',report)
        entry=dict(date='2026-10-18',url='https://example.com/daily')
        write_json(state/'pending.json',dict(run=str(run),report_date='2026-10-18',sha256=sha((run/'index.html').read_bytes()),history_entry=entry))
        return run

    def publisher(self):
        class Fake:
            def __init__(self):self.objects={};self.puts=[];self.fail=False
            def get(self,key):return self.objects.get(key)
            def put(self,key,data):self.objects[key]=data;self.puts.append(key)
            def verify(self,key,data):
                from report import sha
                if self.fail and '/daily/' not in key:raise RuntimeError('verification_failed')
                assert self.objects[key]==data
                return dict(url='https://example.com/'+key,sha256=sha(data),bytes=len(data),http_status=200)
        return Fake()

    def test_unknown_publication_recovers_without_reupload(self):
        from report import publish_pending,load_json
        with tempfile.TemporaryDirectory() as folder:
            state=Path(folder);self.fixture(state);pub=self.publisher();pub.fail=True
            with self.assertRaises(RuntimeError):publish_pending(state,pub)
            self.assertTrue((state/'pending.json').exists());self.assertFalse((state/'history.json').exists())
            pub.fail=False;before=len(pub.puts)
            publish_pending(state,pub)
            self.assertEqual(before,len(pub.puts));self.assertFalse((state/'pending.json').exists())
            self.assertEqual(len(load_json(state/'history.json')),1)

    def test_immutable_archive_conflict_preserves_previous(self):
        from report import publish_pending,PREFIX
        with tempfile.TemporaryDirectory() as folder:
            state=Path(folder);self.fixture(state);pub=self.publisher()
            pub.objects[PREFIX+'/daily/2026-10-18/index.html']=b'previous'
            with self.assertRaisesRegex(ValueError,'immutable'):publish_pending(state,pub)
            self.assertFalse(pub.puts)

    def test_artifact_tampering_rejected(self):
        from report import publish_pending
        with tempfile.TemporaryDirectory() as folder:
            state=Path(folder);run=self.fixture(state);pub=self.publisher()
            (run/'index.html').write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError,'changed'):publish_pending(state,pub)
            self.assertFalse(pub.puts)

    def test_old_date_cannot_replace_latest(self):
        from report import publish_pending,write_json
        with tempfile.TemporaryDirectory() as folder:
            state=Path(folder);self.fixture(state);pub=self.publisher()
            write_json(state/'history.json',[dict(date='2026-10-19')])
            with self.assertRaisesRegex(ValueError,'older_than_latest'):publish_pending(state,pub)
            self.assertFalse(pub.puts)


if __name__=='__main__':
    unittest.main()
