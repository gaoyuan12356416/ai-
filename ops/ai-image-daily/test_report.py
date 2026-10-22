import copy
import datetime as dt
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from collector import (local_time, map_lineage, merge_upload_lineage, read_upload_ledger,
                       collect_lineage, collect_days, Catalog, FIELDS)
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


def upload_fixture(identity='2', migration=False, legacy_ids=None, source_kind='screenshot'):
    source_key = 'screenshot:j1:square_1x1' if source_kind == 'screenshot' else 'youtube-cover:t1:v1:a1'
    source = dict(source_kind=source_kind, source_key=source_key, app_id=1479, content_id='drama',
                  owner_name='张三', generated_at='2026-10-17T01:00:00Z', sha256='a'*64,
                  category='16:9', name='新封面', url='https://example.com/new.jpg',
                  language='en', country='US', is_backfill=migration,
                  backfill_kind='migration' if migration else '', legacy_final_ids=legacy_ids or [])
    request = dict(app_id=1479, content_id='drama', content_sign='signature-' + identity, category='16:9',
                   name=source['name'], user_id=248, initiator=248, country='US', language='en')
    row = dict(key=source_key + ':' + source['sha256'], source=source, request=request,
               state='confirmed', api_id=int(identity), final_id=int(identity))
    final = material(identity)
    final.update(product='Dramawave', data_source=1, content_sign=request['content_sign'],
                 name=request['name'], user_id=248, initiator=248, country='US', language='en')
    return row, final


class UploadLineageTests(unittest.TestCase):
    def merge(self, rows, finals, mapped=None, unresolved=None):
        return merge_upload_lineage(mapped or {}, unresolved or [], rows, finals, {'1479': 'Dramawave'})

    def test_original_product_maps_without_legacy_source_code(self):
        row, final = upload_fixture()
        mapped, unresolved = self.merge([row], [final])
        self.assertEqual(list(mapped), ['2'])
        self.assertEqual(mapped['2']['content_id'], 'drama')
        self.assertEqual(mapped['2']['source'], 'cover_synthesis')
        self.assertFalse(unresolved)

    def test_unconfirmed_states_are_visible_not_zero_or_success(self):
        for state in ('accepted', 'sending', 'unknown', 'failed'):
            with self.subTest(state=state):
                row, final = upload_fixture()
                row['state'] = state
                mapped, unresolved = self.merge([row], [final])
                self.assertFalse(mapped)
                self.assertIn(state, unresolved[0]['reason'])

    def test_mismatched_formal_metadata_rejected(self):
        for field, value in [('content_sign', 'wrong'), ('product', 'FreeReels'),
                             ('type', 2), ('data_source_id', 'wrong'), ('category', '1:1'),
                             ('name', 'another-name'), ('user_id', 249), ('initiator', 249),
                             ('language', 'fr')]:
            with self.subTest(field=field):
                row, final = upload_fixture()
                final[field] = value
                mapped, unresolved = self.merge([row], [final])
                self.assertFalse(mapped)
                self.assertEqual(len(unresolved), 1)

    def test_duplicate_sign_and_missing_final_fail_closed(self):
        row, final = upload_fixture()
        other = dict(final, id='3')
        for finals in ([], [final, other]):
            mapped, unresolved = self.merge([row], finals)
            self.assertFalse(mapped)
            self.assertEqual(len(unresolved), 1)

    def test_requested_app_or_content_mismatch_rejected(self):
        for field, value in [('app_id', 979), ('content_id', 'another')]:
            row, final = upload_fixture()
            row['request'][field] = value
            self.assertFalse(self.merge([row], [final])[0])

    def test_confirmed_with_invalid_present_api_receipt_id_rejected(self):
        for value in (0, -1, '2', True):
            row, final = upload_fixture()
            row['api_id'] = value
            self.assertFalse(self.merge([row], [final])[0])

    def test_signature_readback_can_confirm_without_api_receipt(self):
        row, final = upload_fixture()
        row['api_id'] = None
        mapped, unresolved = self.merge([row], [final])
        self.assertEqual(list(mapped), ['2'])
        self.assertFalse(unresolved)
        self.assertIsNone(row['api_id'])

    def test_missing_api_receipt_still_requires_complete_unique_formal_readback(self):
        for field in ('content_sign', 'name', 'user_id', 'initiator', 'language'):
            row, final = upload_fixture()
            row['api_id'] = None
            final.pop(field)
            self.assertFalse(self.merge([row], [final])[0])
        row, final = upload_fixture()
        row['api_id'] = None
        self.assertFalse(self.merge([row], [final, dict(final, id='3')])[0])

    def test_formal_language_normalization_matches_upload_contract(self):
        row, final = upload_fixture()
        final.update(country='us', language='en_US', user_id='248', initiator='248')
        self.assertFalse(self.merge([row], [final])[1])
        row['request']['language'] = 'zh-tw'
        final['language'] = 'zh_Hant'
        self.assertFalse(self.merge([row], [final])[1])
        final['language'] = 'zh-Hans'
        self.assertFalse(self.merge([row], [final])[0])

    def test_catalog_country_override_or_blank_preserves_actual_value(self):
        for country in ('GB', '', None, 'us'):
            with self.subTest(country=country):
                row, final = upload_fixture()
                final['country'] = country
                row['api_id'] = None
                mapped, unresolved = self.merge([row], [final])
                self.assertFalse(unresolved)
                self.assertEqual(mapped['2']['country'], country)
                self.assertEqual(mapped['2']['requested_country'], 'US')
                self.assertEqual(mapped['2']['country_policy'], 'drama_catalog_override')
                self.assertEqual(row['request']['country'], 'US')
                report = build_report(mapped, [], [], '2026-10-18', '2026-10-19T09:00:00+08:00')
                self.assertEqual(report['materials'][0]['country'], country)
                self.assertEqual(report['materials'][0]['requested_country'], 'US')

    def test_catalog_country_override_does_not_allow_language_mismatch(self):
        row, final = upload_fixture()
        final.update(country='', language='fr')
        self.assertFalse(self.merge([row], [final])[0])

    def test_each_upload_keeps_its_own_requested_country(self):
        first, first_final = upload_fixture()
        second, second_final = upload_fixture('3', source_kind='youtube')
        second['request']['country'] = 'RU'
        second_final['country'] = 'ru'
        mapped, unresolved = self.merge([first, second], [first_final, second_final])
        self.assertFalse(unresolved)
        self.assertEqual(mapped['2']['requested_country'], 'US')
        self.assertEqual(mapped['3']['requested_country'], 'RU')
        self.assertEqual(mapped['2']['country'], 'US')
        self.assertEqual(mapped['3']['country'], 'ru')

    def test_incomplete_request_metadata_cannot_match_missing_formal_fields(self):
        for field in ('name', 'user_id', 'initiator', 'country', 'language'):
            row, final = upload_fixture()
            row['request'].pop(field)
            final.pop(field)
            self.assertFalse(self.merge([row], [final])[0])

    def test_four_product_upload_does_not_expand_dramawave(self):
        for app_id in (979, 3360, 2373):
            row, final = upload_fixture()
            row['source']['app_id'] = row['request']['app_id'] = app_id
            self.assertEqual(self.merge([row], [final]), ({}, []))

    def test_new_only_source_does_not_keep_old_unconfirmed_warning(self):
        job, intake, old = LineageTests().fixtures()
        mapped, unresolved = map_lineage([job], [], [], [], [])
        self.assertTrue(unresolved)
        row, final = upload_fixture()
        mapped, unresolved = self.merge([row], [final], mapped, unresolved)
        self.assertEqual(list(mapped), ['2'])
        self.assertFalse(unresolved)

    def test_legacy_id_alias_preserves_both_formal_ids(self):
        job, intake, old = LineageTests().fixtures()
        mapped, unresolved = map_lineage([job], [intake], [old], [], [])
        row, final = upload_fixture(migration=True, legacy_ids=['1'])
        mapped, unresolved = self.merge([row], [final], mapped, unresolved)
        self.assertEqual(set(mapped), {'1', '2'})
        self.assertEqual(mapped['1']['identity'], mapped['2']['identity'])
        self.assertEqual(mapped['2']['backfill_kind'], 'migration')
        self.assertNotEqual(mapped['1'].get('backfill_kind'), 'migration')
        self.assertFalse(unresolved)

    def test_same_formal_id_not_reported_as_new_migration(self):
        job, intake, old = LineageTests().fixtures()
        mapped, unresolved = map_lineage([job], [intake], [old], [], [])
        row, final = upload_fixture('1', migration=True, legacy_ids=['1'])
        mapped, unresolved = self.merge([row], [final], mapped, unresolved)
        self.assertEqual(list(mapped), ['1'])
        self.assertFalse(mapped['1']['is_backfill'])
        self.assertEqual(mapped['1']['backfill_kind'], '')
        self.assertFalse(unresolved)

    def test_unrelated_legacy_source_cannot_be_claimed(self):
        old = material()
        old['identity'] = 'other-job:square_1x1'
        row, final = upload_fixture(migration=True, legacy_ids=['1'])
        mapped, unresolved = self.merge([row], [final], {'1': old})
        self.assertEqual(list(mapped), ['1'])
        self.assertEqual(len(unresolved), 1)

    def test_two_versions_cannot_claim_one_legacy_id(self):
        row, final = upload_fixture(migration=True, legacy_ids=['1'])
        other, other_final = upload_fixture('3', migration=True, legacy_ids=['1'])
        other['source']['sha256'] = 'b'*64
        mapped, unresolved = self.merge([row, other], [final, other_final])
        self.assertFalse(mapped)
        self.assertEqual(len(unresolved), 2)

    def test_missing_old_alias_is_visible_while_new_formal_remains_verified(self):
        row, final = upload_fixture(migration=True, legacy_ids=['1'])
        mapped, unresolved = self.merge([row], [final])
        self.assertEqual(list(mapped), ['2'])
        self.assertEqual(unresolved[0]['id'], '1')
        self.assertIn('旧正式素材尚未验证', unresolved[0]['reason'])

    def test_youtube_identity_uses_same_shared_ledger(self):
        row, final = upload_fixture(source_kind='youtube')
        mapped, unresolved = self.merge([row], [final])
        self.assertEqual(mapped['2']['source'], 'youtube_cover')
        self.assertTrue(mapped['2']['identity'].startswith('youtube-cover:t1:v1:a1:'))
        self.assertFalse(unresolved)


class UploadLedgerTests(unittest.TestCase):
    def create_ledger(self, path, source=None, request=None):
        row, final = upload_fixture()
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE uploads (key TEXT,source_json TEXT,request_json TEXT,state TEXT,api_id INTEGER,final_id INTEGER)')
            db.execute('INSERT INTO uploads VALUES (?,?,?,?,?,?)',
                       (row['key'], json.dumps(row['source']) if source is None else source,
                        json.dumps(row['request']) if request is None else request, row['state'], 2, 2))
        db.close()

    def test_absent_ledger_is_optional_and_not_created(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, 'not-installed.sqlite3')
            self.assertIsNone(read_upload_ledger(path))
            self.assertFalse(path.exists())

    def test_existing_corrupt_or_missing_schema_is_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, 'upload.sqlite3')
            path.write_bytes(b'not sqlite')
            with self.assertRaises(sqlite3.DatabaseError):
                read_upload_ledger(path)
            path.write_bytes(b'')
            with self.assertRaises(sqlite3.DatabaseError):
                read_upload_ledger(path)

    def test_invalid_json_is_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, 'upload.sqlite3')
            self.create_ledger(path, source='{broken')
            with self.assertRaises(ValueError):
                read_upload_ledger(path)

    def test_reads_ledger_without_changing_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, 'upload.sqlite3')
            self.create_ledger(path)
            before = path.read_bytes()
            rows = read_upload_ledger(path)
            self.assertEqual(rows[0]['source']['app_id'], 1479)
            self.assertEqual(rows[0]['request']['content_sign'], 'signature-2')
            self.assertEqual(before, path.read_bytes())

    def test_collector_integrates_new_ledger_and_limits_root_scope(self):
        row, final = upload_fixture()
        class FakeCatalog:
            def query(self, sql):
                return [dict(id=1479, name='Dramawave')]
            def materials(self, where):
                return [final] if 'content_sign IN' in where else []
        with tempfile.TemporaryDirectory() as folder:
            source, sync, upload = (Path(folder, name) for name in ('source.db', 'sync.db', 'upload.db'))
            with sqlite3.connect(source) as db:
                db.execute('CREATE TABLE drama_screenshot_job (app_id INTEGER)')
                db.execute('CREATE TABLE ad_material_asset (task_id TEXT,status TEXT)')
                db.execute('CREATE TABLE ad_material_task (task_id TEXT,creator_name TEXT,app_id INTEGER)')
            db.close()
            with sqlite3.connect(sync) as db:
                db.execute('CREATE TABLE cover_sync (key TEXT,source TEXT,state TEXT,final_id INTEGER,material_id TEXT)')
            db.close()
            self.create_ledger(upload)
            mapped, unresolved, apps, counts = collect_lineage(FakeCatalog(), source, sync, upload)
            self.assertEqual(apps, ['Dramawave'])
            self.assertEqual(list(mapped), ['2'])
            self.assertFalse(unresolved)
            self.assertTrue(counts['upload_ledger_present'])

    def test_daily_query_independent_aggregate_covers_all_products(self):
        class FakeCatalog:
            sql = ''
            def query(self, sql):
                self.sql = sql
                return [dict(kind='market', dt='2026-10-18')]
        catalog = FakeCatalog()
        collect_days(catalog, {'1': {}, '2': {}}, ['Dramawave'], ['2026-10-18'])
        self.assertIn("'kind','verified_pool'", catalog.sql)
        independent = [sql for sql in catalog.sql.split(';') if "'kind','verified_pool'" in sql][0]
        self.assertNotIn('product=', independent)
        self.assertIn('source_type=3 AND resource_type=1', independent)


class MigrationAnalysisTests(unittest.TestCase):
    def test_migration_counts_ingestion_but_not_new_image_and_sums_both_ids(self):
        old = material('1', '2026-10-17 10:00:00')
        new = material('2', backfill=True)
        old['identity'] = new['identity'] = 'same-source-version'
        new.update(product='Dramawave', backfill_kind='migration', api_version='material_source_v1')
        rows = [fact('ai', '1'), fact('ai', '2', spend=20, revenue=15, product='Dramawave'),
                fact('pool', '1'), fact('verified_pool', spend=30, revenue=20,
                                       impressions=2000, clicks=100, installs=10, af_installs=8, purchases=4),
                fact('market', spend=100, revenue=50, impressions=10000, clicks=500,
                     installs=100, af_installs=80, purchases=50)]
        report = build_report({'1': old, '2': new}, [], rows, '2026-10-18', '2026-10-19T09:00:00+08:00')
        self.assertEqual(report['summary']['production']['new_count'], 1)
        self.assertEqual(report['summary']['production']['migration_count'], 1)
        self.assertEqual(report['summary']['production']['new_image_count'], 0)
        self.assertEqual(report['summary']['performance']['spend'], 30)
        self.assertEqual(report['summary']['performance']['af_revenue_d0'], 20)
        self.assertEqual(report['trend'][-2]['new_image_count'], 1)
        self.assertEqual(report['trend'][-1]['new_image_count'], 0)
        self.assertEqual(len(report['materials']), 2)
        from render import render_report
        html = render_report(report)
        self.assertIn('首次入库原图', html)
        self.assertIn('接口迁移补传', html)
        self.assertIn('不是当日生成张数', html)

    def test_duplicate_legacy_ids_count_one_source_version_on_first_formal_day(self):
        one, two = material('1'), material('2')
        one['identity'] = two['identity'] = 'original-v1'
        report = build_report({'1': one, '2': two}, [], [], '2026-10-18', '2026-10-19T09:00:00+08:00')
        self.assertEqual(report['summary']['production']['new_count'], 2)
        self.assertEqual(report['summary']['production']['new_image_count'], 1)
        self.assertEqual(report['summary']['production']['migration_count'], 0)

    def test_old_generation_recent_first_ingestion_uses_formal_date(self):
        report = build_report({'1': material()}, [], [], '2026-10-18', '2026-10-19T09:00:00+08:00')
        self.assertEqual(report['summary']['production']['new_image_count'], 1)
        self.assertEqual(report['materials'][0]['first_ingestion_at'], '2026-10-18 10:00:00')

    def test_migration_without_resolved_ancestor_does_not_invent_new_original(self):
        new = material('2', backfill=True)
        new['backfill_kind'] = 'migration'
        report = build_report({'2': new}, [], [], '2026-10-18', '2026-10-19T09:00:00+08:00')
        self.assertEqual(report['summary']['production']['new_image_count'], 0)
        self.assertEqual(report['summary']['production']['migration_count'], 1)
        self.assertEqual(report['materials'][0]['first_ingestion_at'], '')

    def test_three_sizes_and_regenerated_versions_remain_distinct(self):
        mapped = {str(i): material(str(i)) for i in range(4)}
        for i, item in enumerate(mapped.values()):
            item['identity'] = 'job:size%s:sha%s' % (i % 3, i)
        report = build_report(mapped, [], [], '2026-10-18', '2026-10-19T09:00:00+08:00')
        self.assertEqual(report['summary']['production']['new_image_count'], 4)

    def test_new_product_metrics_must_pass_independent_pool(self):
        row = material()
        row['api_version'] = 'material_source_v1'
        with self.assertRaisesRegex(ValueError, 'verified_pool_missing'):
            validate_pool(facts(), {'1': row})
        with self.assertRaisesRegex(ValueError, 'verified_pool_conservation'):
            validate_pool(facts() + [fact('verified_pool', spend=11)], {'1': row})



if __name__=='__main__':
    unittest.main()
