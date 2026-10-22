"""Operator waiver covers selection, preview, submit and execution, never pools."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_fb_video_publish_core import Fixture
from test_fb_excel_publish import ExcelTests, row
from test_fb_pool_ids import Catalog, raw
from features.fb_video_publish.sources import SourceError
from features.fb_video_publish.query_broker import validate_query
from features.fb_video_publish.service import PREFIX


class BlacklistCatalog(Catalog):
    def select(self, sql, params):
        validate_query(dict(sql=sql, params=params, schema=self.schema, blacklist_schema=self.blacklist_schema))
        self_test = sql.count('%s') == len(params)
        if not self_test:
            raise AssertionError('SQL bind count mismatch')
        if 'ads_custom_source' in sql and 'drama_released' not in sql:
            self.calls.append((sql, params))
            enforcing = 'ads_facebook_post_blacklist' in sql
            ids = [str(mid) for mid in params[5 if enforcing else 4:-1]]
            self.last = [self.rows[mid] for mid in reversed(ids) if mid in self.rows
                         and not (enforcing and (self.rows[mid]['content_blocked'] or self.rows[mid]['series_blocked']))]
            return self.last
        return super().select(sql, params)


class SourcePolicyTests(unittest.TestCase):
    def test_both_blacklist_types_allowed_for_manual_but_default_still_blocks(self):
        catalog = BlacklistCatalog({'21': raw('21', content_blocked=1), '22': raw('22', series_blocked=1)})
        source = catalog.source()
        with self.assertRaises(SourceError):
            source.get_materials(['21', '22'])
        self.assertEqual(['21', '22'], list(source.get_materials(['21', '22'], enforce_blacklist=False)))
        self.assertEqual(['21', '22'], list(source.resolve_materials(['21', '22'], enforce_blacklist=False)['items']))
        self.assertEqual('21', source.search_materials({'search': '21'}, enforce_blacklist=False)['items'][0]['material_id'])
        self.assertEqual([], source.search_materials({'search': '21'})['items'])

    def test_other_sql_guards_and_diagnostics_survive(self):
        source = BlacklistCatalog({}).source()
        sql, params = source._conditions(enforce_blacklist=False)
        for guard in ('s.data_source=%s', 's.product=%s', 's.type=2', 's.is_delete=0',
                      's.video_duration>0', 's.video_duration<=3600', 'd.release_status=1', 'd.deploy_time<=%s'):
            self.assertIn(guard, sql)
        self.assertNotIn('blacklist', sql)
        self.assertEqual(sql.count('%s'), len(params))
        with patch.object(source.mysql, 'select', return_value=[raw('21', drama_released=0, content_blocked=1)]) as read:
            self.assertEqual('material_not_released', source._unavailable_materials(['21'], enforce_blacklist=False)['21']['code'])
        self.assertNotIn('ads_facebook_post_blacklist', read.call_args.args[0])


class ManualFlowTests(Fixture):
    def test_picker_preview_submit_and_worker_use_manual_policy(self):
        with patch.object(self.sources, 'search_materials', wraps=self.sources.search_materials) as read:
            self.service.handle('GET', PREFIX + '/materials', self.actor)
            self.assertFalse(read.call_args.kwargs['enforce_blacklist'])
        with patch.object(self.sources, 'get_materials', wraps=self.sources.get_materials) as read:
            self.submit()
            self.ready()
            worker = self.worker()
            worker.execute_next()
            self.assertEqual(1, len(worker.calls))
            self.assertTrue(all(c.kwargs['enforce_blacklist'] is False for c in read.call_args_list))

    def test_pool_execution_keeps_blacklist(self):
        self.submit()
        self.ready()
        with self.store.connect() as conn:
            conn.execute("UPDATE fb_video_task SET source_kind='pool'")
        def guarded(ids, **kwargs):
            self.assertTrue(kwargs['enforce_blacklist'])
            raise SourceError('material_blacklisted', 'blocked', 409)
        with patch.object(self.sources, 'get_materials', side_effect=guarded):
            worker = self.worker()
            worker.execute_next()
        self.assertEqual([], worker.calls)


class ExcelPolicyTests(ExcelTests):
    def test_excel_preview_submit_execution_waive_blacklist(self):
        with patch.object(self.sources, 'resolve_materials', wraps=self.sources.resolve_materials) as read:
            preview = self.preview_excel([row()])
            self.assertFalse(read.call_args.kwargs['enforce_blacklist'])
        with patch.object(self.sources, 'get_materials', wraps=self.sources.get_materials) as read:
            self.submit_excel(preview)
            self.ready()
            worker = self.worker()
            worker.execute_next()
            self.assertEqual(1, len(worker.calls))
            self.assertTrue(all(c.kwargs['enforce_blacklist'] is False for c in read.call_args_list))


if __name__ == '__main__':
    unittest.main()
