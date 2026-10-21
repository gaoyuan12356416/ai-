"""Offline Excel parser, preview, transaction and compatibility acceptance tests."""
import base64
import copy
import io
import json
import sys
import unittest
import uuid
import zipfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
from xml.sax.saxutils import escape

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.test_fb_video_publish_core import Fixture
from features.fb_video_publish.core import DomainError
from features.fb_video_publish.excel import HEADERS, read_workbook
from features.fb_video_publish.service import PREFIX
from features.fb_video_publish.client import route, GatewayError


def xlsx(rows, headers=HEADERS):
    """Tiny OOXML fixture, deliberately independent of the production parser."""
    ns = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    output = io.BytesIO()
    data = []
    for rownum, row in enumerate([headers, *rows], 1):
        cells = []
        for col, value in enumerate(row):
            if value is None:
                continue
            ref = chr(65 + col) + str(rownum)
            if isinstance(value, tuple) and value[0] == 'formula':
                cells.append(f'<c r="{ref}"><f>{escape(value[1])}</f><v>11</v></c>')
            elif isinstance(value, tuple) and value[0] == 'date':
                cells.append(f'<c r="{ref}" s="1"><v>{value[1]}</v></c>')
            elif isinstance(value, (int, float)):
                cells.append(f'<c r="{ref}" t="n"><v>{value}</v></c>')
            else:
                cells.append(f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{escape(str(value))}</t></is></c>')
        data.append(f'<row r="{rownum}">' + ''.join(cells) + '</row>')
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('[Content_Types].xml', '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>')
        archive.writestr('_rels/.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        archive.writestr('xl/workbook.xml', f'<workbook xmlns="{ns}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Worksheet" sheetId="1" r:id="rId1"/></sheets></workbook>')
        archive.writestr('xl/_rels/workbook.xml.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>')
        archive.writestr('xl/styles.xml', f'<styleSheet xmlns="{ns}"><numFmts count="1"><numFmt numFmtId="164" formatCode="yyyy-mm-dd hh:mm:ss"/></numFmts><fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts><fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')
        archive.writestr('xl/worksheets/sheet1.xml', f'<worksheet xmlns="{ns}"><sheetData>{"".join(data)}</sheetData></worksheet>')
    return base64.b64encode(output.getvalue()).decode()


def row(pid='11', mid='21', mode='', credential='', caption='', time=None, title='', product='Dramawave'):
    return [product, pid, mode, credential, mid, caption, time, title]


class PageMap(dict):
    def owner_credential_counts(self, ids, owner):
        if owner != '100':
            raise AssertionError('Must use trusted internal operator identity')
        return {pid: 1 for pid in ids}


class ExcelTests(Fixture):
    def setUp(self):
        super().setUp()
        self.sources.pages = PageMap(self.sources.pages)
        self.sources.resolve_materials = lambda ids, **kwargs: {
            'items': copy.deepcopy({i: self.sources.materials[i] for i in ids if i in self.sources.materials}),
            'errors': {i: {'code': 'material_not_found', 'message': '素材ID不存在'} for i in ids if i not in self.sources.materials}}

    def preview_excel(self, rows=None, **changes):
        payload = dict(operation_id=uuid.uuid4().hex, filename='batch.xlsx', content_base64=xlsx([row()] if rows is None else rows))
        payload.update(changes)
        return self.service.handle('POST', PREFIX + '/excel/preview', self.actor, payload)

    def submit_excel(self, preview, allow_repeat=False):
        return self.service.handle('POST', PREFIX + '/excel/batches', self.actor,
            {'request': preview['request'], 'preview_token': preview['preview_token'], 'allow_repeat': allow_repeat})

    def test_preview_is_read_only_and_defaults_and_caption(self):
        preview = self.preview_excel()
        self.assertEqual(preview['summary'], {'total': 1, 'valid': 1, 'skipped': 0})
        item = preview['items'][0]
        self.assertEqual((item['processing_mode'], item['credential_mode'], item['schedule_kind']), ('original', 'page_pool', 'immediate'))
        self.assertIn('测试剧', item['caption'])
        with self.store.connect() as conn:
            for table in ('fb_video_batch', 'fb_video_task', 'fb_video_link', 'fb_video_attempt'):
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0], 0)

    def test_mixed_processing_per_row_order_title_and_owner(self):
        preview = self.preview_excel([row(mid='21', mode='随机模板', credential='否', time='2026-10-22 12:00:00', title='标题'),
                                      row(mid='22', time='2026-10-21 12:00:00')])
        result = self.submit_excel(preview)
        self.assertEqual(result['batch']['processing_mode'], 'mixed')
        tasks = {t['material_id']: t for t in self.tasks()}
        self.assertEqual(tasks['22']['page_sequence'], 1)
        self.assertEqual(tasks['21']['page_sequence'], 2)
        self.assertEqual(tasks['21']['processing_mode'], 'random_overlay')
        self.assertEqual(tasks['21']['prepare_state'], 'planned')
        self.assertEqual(tasks['22']['prepare_state'], 'ready')
        snap = json.loads(tasks['21']['snapshot_json'])
        self.assertEqual((snap['row_number'], snap['title'], snap['credential_mode'], snap['credential_owner_id']), (2, '标题', 'operator', '100'))
        self.assertEqual(tasks['21']['planned_at_utc'], '2026-10-22T04:00:00+00:00')

    def test_same_material_different_pages_independent_time(self):
        result = self.submit_excel(self.preview_excel([row(pid='11', time='2026-10-22 12:00'), row(pid='12', time='2026-10-23 12:00')]))
        self.assertEqual(len(result['batch']['items']), 2)
        self.assertNotEqual(result['batch']['items'][0]['planned_at_utc'], result['batch']['items'][1]['planned_at_utc'])

    def test_unconfigured_page_all_rows_skip_other_page_continues(self):
        preview = self.preview_excel([row(pid='99'), row(pid='99', mid='22'), row(pid='12')])
        self.assertEqual(preview['summary'], {'total': 3, 'valid': 1, 'skipped': 2})
        self.assertEqual([x['code'] for x in preview['skipped']], ['page_not_configured'] * 2)
        self.submit_excel(preview)
        self.assertNotIn('99', self.store.configs(self.actor))

    def test_invalid_source_and_duplicate_first_valid_only(self):
        preview = self.preview_excel([row(mode='broken'), row(), row(), row(mid='999')])
        self.assertEqual([x['row_number'] for x in preview['items']], [3])
        self.assertEqual([x['code'] for x in preview['skipped']], ['invalid_processing_mode', 'duplicate_pair', 'material_not_found'])

    def test_invalid_language_and_macro_are_partial(self):
        self.sources.materials['22']['language'] = 'en'
        preview = self.preview_excel([row(mid='22'), row(caption='{unsupported}'), row(pid='12')])
        self.assertEqual(preview['summary']['valid'], 1)
        self.assertEqual({x['code'] for x in preview['skipped']}, {'language_mismatch', 'unknown_macro'})

    def test_private_credentials_missing_skips_only_own_rows(self):
        with patch.object(self.sources.pages, 'owner_credential_counts', return_value={}):
            preview = self.preview_excel([row(credential='否'), row(pid='12')])
        self.assertEqual(preview['summary']['valid'], 1)
        self.assertEqual(preview['skipped'][0]['code'], 'operator_authorization_unavailable')

    def test_private_credentials_removed_between_preview_and_submit(self):
        preview = self.preview_excel([row(credential='否')])
        with patch.object(self.sources.pages, 'owner_credential_counts', return_value={}):
            with self.assertRaises(DomainError) as caught:
                self.submit_excel(preview)
        self.assertEqual(caught.exception.code, 'operator_authorization_unavailable')
        self.assertEqual(self.tasks(), [])

    def test_caption_override_frozen_does_not_edit_page(self):
        old = self.store.configs(self.actor)
        result = self.submit_excel(self.preview_excel([row(caption='本行 {name} {url}')]))
        self.assertIn('本行 测试剧 https://', result['batch']['items'][0]['caption'])
        self.assertEqual(old, self.store.configs(self.actor))

    def test_time_passed_and_invalid_token_enum_skip(self):
        preview = self.preview_excel([row(time='2026-10-19 01:00'), row(credential='no'), row(pid='12')])
        self.assertEqual(preview['summary']['valid'], 1)
        self.assertEqual({x['code'] for x in preview['skipped']}, {'invalid_schedule', 'invalid_credential_mode'})

    def test_all_invalid_creates_nothing(self):
        preview = self.preview_excel([row(pid='99'), row(product='HotDrama')])
        self.assertEqual(preview['items'], [])
        self.assertIsNone(preview['request'])
        self.assertEqual(preview['preview_token'], '')
        self.assertEqual(self.tasks(), [])

    def test_historical_unconfirmed_skips_and_idempotency_binds_policy(self):
        first = self.submit()
        with self.store.connect() as conn:
            conn.execute("UPDATE fb_video_task SET status='published',graph_post_id='test-post'")
        preview = self.preview_excel([row(), row(mid='22')])
        self.assertTrue(preview['repeat_required'])
        result = self.submit_excel(preview)
        self.assertEqual([x['material_id'] for x in result['batch']['items']], ['22'])
        self.assertEqual(result['batch']['excel']['skipped'][0]['code'], 'repeat_not_confirmed')
        self.assertEqual(self.submit_excel(preview)['batch_id'], result['batch_id'])
        with self.assertRaises(DomainError) as caught:
            self.submit_excel(preview, True)
        self.assertEqual(caught.exception.code, 'operation_conflict')
        self.assertNotEqual(first['batch_id'], result['batch_id'])

    def test_confirmed_historical_and_only_unconfirmed(self):
        self.submit()
        with self.store.connect() as conn:
            conn.execute("UPDATE fb_video_task SET status='published',graph_post_id='test-post'")
        preview = self.preview_excel()
        with self.assertRaises(DomainError) as caught:
            self.submit_excel(preview)
        self.assertEqual(caught.exception.code, 'no_rows_selected')
        result = self.submit_excel(preview, True)
        self.assertEqual(len(result['batch']['items']), 1)

    def test_accepted_retry_survives_expiry_and_closed_gate(self):
        preview = self.preview_excel()
        result = self.submit_excel(preview)
        self.now += timedelta(hours=1)
        self.service.excel_enabled = False
        with patch.object(self.sources, 'get_materials', side_effect=AssertionError('must return saved operation')):
            retry = self.submit_excel(preview)
        self.assertTrue(retry['idempotent'])
        self.assertEqual(result['batch_id'], retry['batch_id'])
        self.assertEqual(len(self.tasks()), 1)

    def test_tampered_policy_and_title_rejected(self):
        for field, value in [('credential_mode', 'operator'), ('title', 'changed')]:
            preview = self.preview_excel()
            preview['request']['pairs'][0][field] = value
            with self.assertRaises(DomainError) as caught:
                self.submit_excel(preview)
            self.assertEqual(caught.exception.code, 'preview_changed')
        self.assertEqual(self.tasks(), [])

    def test_config_change_or_source_change_rejects_atomically(self):
        preview = self.preview_excel()
        self.sources.materials['21']['material_name'] = 'updated'
        with self.assertRaises(DomainError) as caught:
            self.submit_excel(preview)
        self.assertEqual(caught.exception.code, 'preview_changed')
        self.assertEqual(self.tasks(), [])

    def test_immediate_time_is_computed_at_commit(self):
        preview = self.preview_excel()
        self.now += timedelta(minutes=1)
        result = self.submit_excel(preview)
        self.assertEqual(result['batch']['items'][0]['planned_at_utc'], self.now.isoformat(timespec='seconds'))

    def test_old_routes_reject_new_schema_and_excel_reads_reject_old_batch(self):
        preview = self.preview_excel()
        with self.assertRaises(DomainError):
            self.preflight(preview['request'])
        result = self.submit()
        with self.assertRaises(DomainError) as caught:
            self.service.handle('GET', PREFIX + '/excel/batches/' + result['batch_id'], self.actor)
        self.assertEqual(caught.exception.code, 'batch_not_found')

    def test_operation_and_batch_lookup_actor_scope(self):
        preview = self.preview_excel()
        result = self.submit_excel(preview)
        found = self.service.handle('GET', PREFIX + '/excel/operations/' + result['operation_id'], self.actor)
        self.assertEqual(found['batch_id'], result['batch_id'])
        stranger = dict(self.actor, user_id='u2')
        for path in ('/excel/operations/' + result['operation_id'], '/excel/batches/' + result['batch_id']):
            with self.assertRaises(DomainError):
                self.service.handle('GET', PREFIX + path, stranger)

    def test_capacity_and_gate_blocks_without_writes(self):
        self.store.daily_limit = 0
        with self.assertRaises(DomainError) as caught:
            self.preview_excel()
        self.assertEqual(caught.exception.code, 'daily_capacity_exceeded')
        self.service.excel_enabled = False
        with self.assertRaises(DomainError) as caught:
            self.preview_excel()
        self.assertEqual(caught.exception.code, 'excel_gate_closed')


class ParserTests(unittest.TestCase):
    def test_headers_empty_corrupt_and_size(self):
        for filename, content, code in [('x.xls', xlsx([]), 'invalid_excel_file'),
                ('x.xlsx', xlsx([]), 'excel_empty'), ('x.xlsx', '%%%%', 'invalid_excel_file'),
                ('x.xlsx', xlsx([], headers=['bad']), 'excel_headers_invalid'),
                ('x.xlsx', 'A' * (3 * 1024 * 1024), 'excel_too_large')]:
            with self.subTest(code=code):
                with self.assertRaises(DomainError) as caught:
                    read_workbook(filename, content)
                self.assertEqual(caught.exception.code, code)

    def test_max_rows_no_truncation(self):
        with self.assertRaises(DomainError) as caught:
            read_workbook('x.xlsx', xlsx([row()] * 201))
        self.assertEqual(caught.exception.code, 'excel_rows_exceeded')

    def test_numeric_precision_and_formula(self):
        _, rows, errors, total = read_workbook('x.xlsx', xlsx([row(pid='1234567890123456'), row(pid=1234567890123456),
            row(mid=('formula', 'SUM(10,11)')), row(pid=123456789012345), row(pid='1.234E+15')]))
        self.assertEqual([r['row_number'] for r in rows], [2, 5])
        self.assertEqual([e['code'] for e in errors], ['excel_id_precision', 'excel_formula_not_allowed', 'invalid_id'])
        self.assertEqual(total, 5)

    def test_excel_date_and_blank_row(self):
        _, rows, errors, total = read_workbook('x.xlsx', xlsx([[], row(time=('date', 46317.5))]))
        self.assertEqual(errors, [])
        self.assertEqual(total, 1)
        self.assertEqual(rows[0]['row_number'], 3)
        self.assertIn('T04:00:00+00:00', rows[0]['scheduled_at_utc'])

    def test_zip_expansion_bound(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as z:
            z.writestr('huge.xml', b'a' * (17 * 1024 * 1024))
        with self.assertRaises(DomainError) as caught:
            read_workbook('x.xlsx', base64.b64encode(stream.getvalue()).decode())
        self.assertEqual(caught.exception.code, 'invalid_excel_file')

    def test_routes_use_page_permission(self):
        for method, path, mapping in [('POST', '/excel/preview', True), ('POST', '/excel/batches', True),
              ('GET', '/excel/operations/' + 'a' * 32, False), ('GET', '/excel/batches/' + 'a' * 32, False),
              ('POST', '/pages/1234/pool/items/resolve', True)]:
            self.assertEqual(route(method, PREFIX + path), (('fbPageManagement',), mapping))
        with self.assertRaises(GatewayError):
            route('POST', PREFIX + '/excel/execute')


if __name__ == '__main__':
    unittest.main()
