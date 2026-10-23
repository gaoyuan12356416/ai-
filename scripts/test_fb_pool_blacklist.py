"""Pool admission, scheduling and execution with both blacklist types; no real writes."""
import json
import unittest
import uuid
from datetime import timedelta
from unittest.mock import patch

import test_fb_video_page_pool as pool_tests
from test_fb_manual_blacklist import BlacklistCatalog, raw
from features.fb_video_publish.sources import SourceError


class PoolBlacklistTests(pool_tests.PoolTests):
    def test_blacklisted_ids_resolve_add_schedule_and_execute(self):
        catalog = BlacklistCatalog({'21': raw('21', language='zh', content_blocked=1),
                                    '22': raw('22', language='zh', series_blocked=1)})
        source = catalog.source()
        with self.assertRaises(SourceError):
            source.get_materials(['21', '22'])
        with patch.object(self.sources, 'get_materials', side_effect=source.get_materials) as read, \
             patch.object(self.sources, 'resolve_materials', side_effect=source.resolve_materials, create=True):
            base = self.base()
            result = self.call('/items/resolve', {**base, 'material_ids': ['21', '22']})
            self.assertEqual({'total': 2, 'valid': 2, 'skipped': 0}, result['summary'])
            self.assertEqual(0, self.listing()['counts']['pending'])
            preview = self.call('/items/preview', {**base, 'material_ids': ['21', '22']})
            self.assertEqual(2, len(preview['items']))
            self.call('/items', {**base, 'material_ids': result['material_ids'],
                                'operation_id': uuid.uuid4().hex, 'preview_token': result['preview_token'],
                                'allow_repeat': False})
            self.settings()
            self.call('/enable', self.base())
            for mid in ('21', '22'):
                self.assertEqual(1, self.due()['slots_created'])
                task = self.tasks()[-1]
                self.assertEqual((mid, 'pool'), (task['material_id'], task['source_kind']))
                frozen = task['snapshot_json']
                self.ready()
                worker = self.worker()
                self.assertEqual('submitted', worker.execute_next()['status'])
                self.assertEqual(1, len(worker.calls))
                self.now += timedelta(seconds=61)
                self.assertEqual('published', worker.reconcile_next()['status'])
                self.assertEqual(frozen, self.tasks()[-1]['snapshot_json'])
            self.assertTrue(all(c.kwargs['enforce_blacklist'] is False for c in read.call_args_list))
            self.assertTrue(all(not c.kwargs.get('manual', False) for c in read.call_args_list))

    def test_legacy_manual_pool_snapshot_waives_only_blacklist(self):
        self.submit(); self.ready()
        with self.store.connect() as conn:
            task = conn.execute('SELECT task_id,snapshot_json FROM fb_video_task').fetchone()
            snapshot = json.loads(task['snapshot_json']); snapshot['pool'] = {'legacy': True}
            conn.execute('UPDATE fb_video_task SET snapshot_json=? WHERE task_id=?',
                         (json.dumps(snapshot), task['task_id']))
        with patch.object(self.sources, 'get_materials', wraps=self.sources.get_materials) as read:
            worker = self.worker()
            self.assertEqual('submitted', worker.execute_next()['status'])
            self.assertFalse(read.call_args.kwargs['enforce_blacklist'])
            self.assertFalse(read.call_args.kwargs['manual'])


if __name__ == '__main__':
    unittest.main()
