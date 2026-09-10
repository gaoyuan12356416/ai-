"""Read-only SQL fixtures and isolated local short-link/workflow regression tests."""
import json
from pathlib import Path
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.drama_synthesis.core import DramaSynthesisStore, DramaSynthesisError, build_long_url
from features.youtube_auto_publish.drama import DramaMetadataResolver
from features.youtube_auto_publish.templates import WorkflowError
from features.youtube_auto_publish.runtime import build_service
from test_youtube_auto_service import WorkflowCase


def encoded(*values):
    return [(json.dumps(value).encode().hex(),) for value in values]


class MetadataCase(unittest.TestCase):
    def material(self, **changes):
        return dict(dict(content_id='dramaA', language='zh-TW', macro_name='filename', macro_desc='old'), **changes)

    def resource(self, **changes):
        return dict(dict(content_id='dramaA', language='zh-tw', name='繁體劇名', desc='繁體劇情簡介'), **changes)

    def test_duplicate_episode_records_resolve_same_localized_metadata(self):
        query = Mock(return_value=encoded(self.resource(), self.resource()))
        materials = [self.material() for _ in range(100)]
        DramaMetadataResolver(query)(materials)
        self.assertTrue(all(m['drama_status'] == 'matched' and m['macro_name'] == '繁體劇名'
                            and m['macro_desc'] == '繁體劇情簡介' for m in materials))
        query.assert_called_once()
        sql = query.call_args.args[0]
        self.assertIn('SELECT DISTINCT HEX(JSON_OBJECT', sql)
        self.assertIn('FORCE INDEX (content_id)', sql)
        self.assertIn('LIMIT 1001', sql)
        self.assertIn('COLLATE utf8mb4_unicode_ci', sql)

    def test_language_or_exact_content_id_mismatch_does_not_guess(self):
        query = Mock(return_value=encoded(self.resource(language='en'), self.resource(content_id='dramaa')))
        result = DramaMetadataResolver(query)([self.material()])[0]
        self.assertEqual(result['drama_status'], 'missing')
        self.assertEqual((result['macro_name'], result['macro_desc']), ('', ''))

    def test_conflicting_metadata_is_visible_and_not_randomly_selected(self):
        query = Mock(return_value=encoded(self.resource(), self.resource(desc='Different description')))
        result = DramaMetadataResolver(query)([self.material()])[0]
        self.assertEqual(result['drama_status'], 'ambiguous')
        self.assertIn('冲突', result['drama_message'])
        self.assertEqual(result['macro_desc'], '')

    def test_empty_identity_does_not_query(self):
        query = Mock()
        DramaMetadataResolver(query)([self.material(content_id=''), self.material(language='')])
        query.assert_not_called()

    def test_untrusted_values_are_hex_literals(self):
        query = Mock(return_value=[])
        DramaMetadataResolver(query)([self.material(content_id="x' OR 1=1 --", language="zh'--")])
        sql = query.call_args.args[0]
        self.assertNotIn("x' OR", sql)
        self.assertNotIn("zh'--", sql)

    def test_query_error_or_truncated_result_fails_closed(self):
        for query in (Mock(side_effect=RuntimeError('secret')), Mock(return_value=encoded(self.resource()) * 1001)):
            with self.assertRaises(WorkflowError) as caught:
                DramaMetadataResolver(query)([self.material()])
            self.assertEqual(caught.exception.code, 'drama_query_failed')
            self.assertNotIn('secret', str(caught.exception))


class AssociationWorkflowCase(unittest.TestCase):
    payload, create, assert_error, cover = WorkflowCase.payload, WorkflowCase.create, WorkflowCase.assert_error, WorkflowCase.cover

    def setUp(self):
        WorkflowCase.setUp(self)
        self.material.update(source_job_id='', source_kind='', macro_name='video filename', macro_desc='', language='zh-TW')
        self.drama = dict(content_id=self.material['content_id'], language='zh-tw', name='繁體劇名', desc='繁體劇情簡介')
        self.drama_query = Mock(side_effect=lambda sql: encoded(self.drama))
        self.source.drama_resolver = DramaMetadataResolver(self.drama_query)

    def test_selection_resolves_and_submit_freezes_all_three_macros(self):
        before = self.service.list_materials(self.actor)['items'][0]
        self.assertTrue(before['link_ready'])
        self.assertEqual(before['source_job_id'], '')
        self.assertEqual(before['long_url'], '')
        task = self.create(title_template='{name}', description_template='{url}\n{desc}', comment_template='{name}\n{url}')
        m = task['material']
        self.assertEqual(task['title'], '繁體劇名')
        self.assertIn('繁體劇情簡介', task['description'])
        self.assertEqual(task['comment'], '繁體劇名\nhttps://example.invalid/short-for-tests')
        self.assertEqual(m['link_job_id'], task['id'])
        self.assertEqual(m['source_job_id'], '')
        self.assertEqual(m['long_url'], build_long_url(task['id'], self.material['content_id']))
        self.drama['desc'] = 'Changed after publishing preparation'
        self.assertIn('繁體劇情簡介', self.service.get_task(self.actor, task['id'])['task']['description'])
        self.assertEqual(self.drama_query.call_count, 2)  # list + fresh submission lookup

    def test_same_operation_and_retry_before_save_keep_tracking_identity(self):
        payload = self.payload()
        self.short_link.side_effect = [WorkflowError('temporary', 'Retry', 503), 'https://example.invalid/short']
        self.assert_error('temporary', lambda: self.service.create_task(self.actor, payload))
        task = self.service.create_task(self.actor, payload)['task']
        replay = self.service.create_task(self.actor, payload)['task']
        self.assertEqual(task['id'], replay['id'])
        identities = [call.args[0]['link_job_id'] for call in self.short_link.call_args_list]
        self.assertEqual(identities, [task['id'], task['id']])

    def test_owners_and_new_operations_have_different_link_identities(self):
        payload = self.payload()
        first = self.service.create_task(self.actor, payload)['task']
        other = self.service.create_task(self.other, payload)['task']
        next_task = self.create()
        self.assertEqual(len({first['id'], other['id'], next_task['id']}), 3)

    def test_missing_drama_or_synopsis_preflight_has_no_short_link_side_effect(self):
        self.drama['language'] = 'en'
        self.assert_error('source_association_missing', lambda: self.create())
        self.drama['language'] = 'zh-tw'
        self.drama['desc'] = ''
        self.assert_error('macro_source_missing', lambda: self.create())
        self.short_link.assert_not_called()

    def test_existing_synthesis_link_identity_is_preserved(self):
        self.material.update(source_job_id='a'*32, source_kind='concat_video')
        task = self.create()
        self.assertEqual(task['material']['link_job_id'], 'a'*32)
        self.assertEqual(task['material']['long_url'], build_long_url('a'*32, self.material['content_id']))

    def test_reviewed_enqueue_uses_same_identity_as_long_link(self):
        asset = self.cover()
        task = self.create(cover_source='local', cover_asset_id=asset['id'])
        self.service.run_once()
        self.assertEqual(self.store.enqueues[0]['job_id'], task['material']['link_job_id'])

    def test_real_local_store_deduplicates_concurrent_short_links(self):
        engine = DramaSynthesisStore(self.root / 'short-links.sqlite3')
        engine.ensure_storage()
        publisher = Mock()
        publisher.publish.return_value = {'reused': False}
        barrier = threading.Barrier(2)
        def short_link(material):
            barrier.wait(timeout=5)
            return engine.ensure_short_link(material['link_job_id'], 'custom_source', material['content_id'], publisher)['short_url']
        self.service.short_link = short_link
        payload = self.payload()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.service.create_task(self.actor, payload)['task'], range(2)))
        self.assertEqual(results[0]['id'], results[1]['id'])
        self.assertEqual(results[0]['description'], results[1]['description'])
        links = engine.short_links_for_job(results[0]['id'])
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]['material_kind'], 'custom_source')
        self.assertEqual(links[0]['long_url'], build_long_url(results[0]['id'], self.material['content_id']))
        with self.assertRaises(DramaSynthesisError):
            engine.ensure_short_link(results[0]['id'], 'custom_source', 'another-drama', publisher)

    def test_runtime_short_link_adapter_accepts_standalone_and_synthesis(self):
        app = Mock(PUBLIC_BASE_URL='https://example.invalid', DB_NAME='kunlunads_dev')
        app.DRAMA_SYNTHESIS_STORE.ensure_short_link.return_value = {'short_url': 'https://example.invalid/short'}
        with patch('features.youtube_auto_publish.runtime.YouTubeWorkflow') as workflow, patch.dict('os.environ', {'YOUTUBE_AUTO_STORAGE_ROOT': str(self.root)}):
            build_service(app)
        source = workflow.call_args.args[2]
        self.assertIsInstance(source.drama_resolver, DramaMetadataResolver)
        callback = workflow.call_args.args[4]
        for m, kind, identity in ((dict(self.material, link_job_id='b'*32), 'custom_source', 'b'*32),
                                  (dict(self.material, source_job_id='a'*32, source_kind='concat_video'), 'concat_video', 'a'*32)):
            callback(m)
            app.DRAMA_SYNTHESIS_STORE.ensure_short_link.assert_called_with(identity, kind, m['content_id'], app.DRAMA_SHORT_LINK_PUBLISHER)


if __name__ == '__main__':
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(cls)
                               for cls in (MetadataCase, AssociationWorkflowCase))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
