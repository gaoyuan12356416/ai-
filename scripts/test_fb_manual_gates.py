"""Manual and Excel admission, defaults and capacity regressions; no network writes."""
import base64
import copy
import io
import json
import sys
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_fb_video_publish_core import Fixture
from test_fb_excel_publish import ExcelTests, row, xlsx
from test_fb_manual_blacklist import BlacklistCatalog
from test_fb_pool_ids import raw
from features.fb_video_publish.core import DomainError, normalize_batch
from features.fb_video_publish.excel import read_workbook
from features.fb_video_publish.templates import render_preview, TemplateError


class SourceTests(unittest.TestCase):
    def test_unreleased_and_missing_metadata_only_allowed_for_manual(self):
        catalog = BlacklistCatalog({'21': raw('21', drama_released=0)})
        source = catalog.source()
        catalog.bad_name.add('21')
        catalog.bad_desc.add('21')
        self.assertEqual({}, source.resolve_materials(['21'])['items'])
        material = source.get_materials(['21'], manual=True, enforce_blacklist=False)['21']
        self.assertEqual('', material['drama_name'])
        self.assertEqual('', material['drama_description'])
        manual_queries = catalog.calls[-3:]
        self.assertTrue(all('release_status' not in sql and 'deploy_time' not in sql for sql, _ in manual_queries))
        self.assertEqual('hello {custom}', render_preview('hello {name}{desc}{custom}', material, manual=True))
        with self.assertRaises(TemplateError):
            render_preview('{name}', material)

    def test_manual_large_source_query_remains_chunked(self):
        mids = [str(i) for i in range(1, 206)]
        catalog = BlacklistCatalog({mid: raw(mid) for mid in mids})
        result = catalog.source().resolve_materials(mids, manual=True, enforce_blacklist=False)
        self.assertEqual(mids, list(result['items']))
        queries = [(sql, args) for sql, args in catalog.calls if 'ads_custom_source' in sql]
        self.assertEqual([100, 100, 5], [len(args[2:-1]) for sql, args in queries])


class ManualTests(Fixture):
    def test_unconfigured_page_blank_copy_and_cross_language_freeze(self):
        req = self.req()
        with self.store.connect() as conn:
            conn.execute("UPDATE fb_video_page SET deleted=1 WHERE page_id='11'")
        req['pairs'][0].update(config_instance_id='', config_version=0, caption_template='', title='')
        self.sources.materials['21'].update(language='en', drama_name='', drama_description='')
        result = self.submit(req)
        task = result['batch']['items'][0]
        self.assertEqual(('', ''), (task['caption'], task['title']))
        self.assertIn('*none*', task['long_url'])
        self.assertNotIn('11', self.store.configs(self.actor))
        self.ready()
        worker = self.worker()
        worker.execute_next()
        self.assertEqual(1, len(worker.calls))

    def test_page_title_caption_fallback_and_frozen_defaults(self):
        config = self.store.configs(self.actor)['11']
        self.store.save_configs(self.actor, ['11'], 'zh', 'Page {name}', title='Page title',
                                expected=(config['config_instance_id'], config['version']))
        req = self.req()
        req['pairs'][0].update(caption_template=' ', title=' ')
        preview = self.preflight(req)
        self.assertEqual(('Page 测试剧', 'Page title'), (preview['items'][0]['caption'], preview['items'][0]['title']))
        self.submit(req)
        config = self.store.configs(self.actor)['11']
        self.store.save_configs(self.actor, ['11'], 'zh', 'Changed', title='Changed',
                                expected=(config['config_instance_id'], config['version']))
        snap = json.loads(self.tasks()[0]['snapshot_json'])
        self.assertEqual(('Page {name}', 'Page title'), (snap['caption_template'], snap['title']))

    def test_more_than_200_tasks_and_daily_limit_bypassed(self):
        mids = [str(i) for i in range(1001, 1206)]
        for mid in mids:
            self.sources.materials[mid] = {**self.sources.materials['21'], 'material_id': mid}
        self.store.daily_limit = 1
        result = self.submit(self.req([(mid, '11') for mid in mids]))
        self.assertEqual(205, len(result['batch']['items']))
        self.assertEqual(205, len(self.tasks()))
        with self.store.connect() as conn:
            self.assertEqual(205, conn.execute('SELECT COUNT(*) FROM fb_video_link').fetchone()[0])


class ExcelGatesTests(ExcelTests):
    def test_unconfigured_authorized_page_does_not_write_configuration(self):
        self.sources.pages['99'] = {**self.sources.pages['11'], 'page_id': '99'}
        preview = self.preview_excel([row(pid='99', caption='hello', title='title')])
        self.assertEqual({'total': 1, 'valid': 1, 'skipped': 0}, preview['summary'])
        self.submit_excel(preview)
        self.assertNotIn('99', self.store.configs(self.actor))

    def test_independent_defaults_and_long_text(self):
        cfg = self.store.configs(self.actor)['11']
        self.store.save_configs(self.actor, ['11'], 'zh', 'default caption', title='default title',
                                expected=(cfg['config_instance_id'], cfg['version']))
        title = 't' * 300
        preview = self.preview_excel([row(title=title), row(mid='22', caption='x' * 17000)])
        self.assertEqual('default caption', preview['items'][0]['caption'])
        self.assertEqual(title, preview['items'][0]['title'])
        self.assertEqual('default title', preview['items'][1]['title'])
        result = self.submit_excel(preview)
        self.assertEqual(17000, len(result['batch']['items'][1]['caption']))
        self.assertEqual(title, result['batch']['items'][0]['title'])

    def test_excel_205_distinct_rows_freeze_once_without_quota(self):
        mids = [str(i) for i in range(1001, 1206)]
        for mid in mids:
            self.sources.materials[mid] = {**self.sources.materials['21'], 'material_id': mid}
        self.store.daily_limit = 1
        preview = self.preview_excel([row(mid=mid) for mid in mids])
        self.assertEqual(205, preview['summary']['valid'])
        first = self.submit_excel(preview)
        second = self.submit_excel(preview)
        self.assertEqual(first['batch_id'], second['batch_id'])
        self.assertEqual(205, len(self.tasks()))


class FileTests(unittest.TestCase):
    def test_valid_file_above_two_megabytes_is_read(self):
        stream = io.BytesIO(base64.b64decode(xlsx([row()])))
        with zipfile.ZipFile(stream, 'a', compression=zipfile.ZIP_STORED) as archive:
            archive.writestr('unused.bin', b'x' * (2 * 1024 * 1024 + 1))
        _, rows, errors, total = read_workbook('large.xlsx', base64.b64encode(stream.getvalue()).decode())
        self.assertEqual((len(rows), total, errors), (1, 1, []))


if __name__ == '__main__':
    unittest.main()
