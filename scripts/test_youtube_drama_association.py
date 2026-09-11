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


DRAMA_COVER = 'https://cdn.usrgrow.com/storage/icons/drama-cover.jpg'


class MetadataCase(unittest.TestCase):
    def material(self, **changes):
        return dict(dict(content_id='dramaA', language='zh-TW', macro_name='filename', macro_desc='old'), **changes)

    def resource(self, **changes):
        return dict(dict(content_id='dramaA', language='zh-tw', name='繁體劇名', desc='繁體劇情簡介', cover=DRAMA_COVER), **changes)

    def test_duplicate_episode_records_resolve_same_localized_metadata(self):
        query = Mock(return_value=encoded(self.resource(), self.resource()))
        materials = [self.material() for _ in range(100)]
        DramaMetadataResolver(query)(materials)
        self.assertTrue(all(m['drama_status'] == 'matched' and m['macro_name'] == '繁體劇名'
                            and m['macro_desc'] == '繁體劇情簡介' for m in materials))
        self.assertTrue(all(m['drama_cover_status'] == 'available' and m['drama_cover_url'] == DRAMA_COVER
                            for m in materials))
        query.assert_called_once()
        sql = query.call_args.args[0]
        self.assertIn('SELECT DISTINCT HEX(JSON_OBJECT', sql)
        self.assertIn('FORCE INDEX (content_id)', sql)
        self.assertIn('LIMIT 1001', sql)
        self.assertIn('COLLATE utf8mb4_unicode_ci', sql)
        self.assertIn("'cover',TRIM(COALESCE(r.cover,''))", sql)

    def test_language_or_exact_content_id_mismatch_does_not_guess(self):
        query = Mock(return_value=encoded(self.resource(language='en'), self.resource(content_id='dramaa')))
        result = DramaMetadataResolver(query)([self.material()])[0]
        self.assertEqual(result['drama_status'], 'missing')
        self.assertEqual((result['macro_name'], result['macro_desc']), ('', ''))
        self.assertEqual((result['drama_cover_status'], result['drama_cover_url']), ('missing', ''))

    def test_conflicting_metadata_is_visible_and_not_randomly_selected(self):
        query = Mock(return_value=encoded(self.resource(), self.resource(desc='Different description')))
        result = DramaMetadataResolver(query)([self.material()])[0]
        self.assertEqual(result['drama_status'], 'ambiguous')
        self.assertIn('冲突', result['drama_message'])
        self.assertEqual(result['macro_desc'], '')
        self.assertEqual((result['drama_cover_status'], result['drama_cover_url']), ('available', DRAMA_COVER))

    def test_each_language_gets_only_its_exact_drama_cover(self):
        english_cover = 'https://cdn.usrgrow.com/storage/icons/english.jpg'
        query = Mock(return_value=encoded(self.resource(), self.resource(language='en', name='English', cover=english_cover)))
        result = DramaMetadataResolver(query)([self.material(), self.material(language=' EN ')])
        self.assertEqual([m['drama_cover_url'] for m in result], [DRAMA_COVER, english_cover])
        self.assertEqual([m['macro_name'] for m in result], ['繁體劇名', 'English'])
        query.assert_called_once()

    def test_equivalent_cover_hosts_and_blank_episodes_do_not_conflict(self):
        canonical = 'https://static-v1.mydramawave.com/covers/drama.jpg'
        query = Mock(return_value=encoded(
            self.resource(cover=' https://static.mydramawave.com/covers/drama.jpg '),
            self.resource(cover=canonical), self.resource(cover='http://static.mydramawave.com/covers/drama.jpg'),
            self.resource(cover=''), self.resource(cover=None)))
        result = DramaMetadataResolver(query)([self.material()])[0]
        self.assertEqual((result['drama_cover_status'], result['drama_cover_url']), ('available', canonical))
        self.assertEqual(result['drama_cover_message'], '')
        self.assertEqual(result['drama_status'], 'matched')

    def test_different_cover_versions_do_not_break_text_macros(self):
        query = Mock(return_value=encoded(self.resource(), self.resource(cover=DRAMA_COVER + '?version=2')))
        result = DramaMetadataResolver(query)([self.material()])[0]
        self.assertEqual((result['drama_cover_status'], result['drama_cover_url']), ('ambiguous', ''))
        self.assertTrue(result['drama_cover_message'])
        self.assertEqual((result['drama_status'], result['macro_name'], result['macro_desc']),
                         ('matched', '繁體劇名', '繁體劇情簡介'))

    def test_empty_episode_covers_are_missing_and_clear_stale_cover_fields(self):
        query = Mock(return_value=encoded(self.resource(cover=''), self.resource(cover=None), self.resource(cover='  ')))
        material = self.material(drama_cover_status='available', drama_cover_url=DRAMA_COVER, drama_cover_message='old')
        result = DramaMetadataResolver(query)([material])[0]
        self.assertEqual((result['drama_cover_status'], result['drama_cover_url']), ('missing', ''))
        self.assertNotEqual(result['drama_cover_message'], 'old')
        self.assertEqual(result['drama_status'], 'matched')

    def test_invalid_cover_urls_are_reported_without_changing_text_match(self):
        for cover in ('https://untrusted.invalid/cover.jpg', 'http://untrusted.invalid/cover.jpg',
                      'file:///tmp/cover.jpg', 'https://user@cdn.usrgrow.com/cover.jpg',
                      'https://cdn.usrgrow.com:8080/cover.jpg'):
            with self.subTest(cover=cover):
                query = Mock(return_value=encoded(self.resource(cover=cover)))
                result = DramaMetadataResolver(query)([self.material()])[0]
                self.assertEqual((result['drama_cover_status'], result['drama_cover_url']), ('invalid', ''))
                self.assertTrue(result['drama_cover_message'])
                self.assertEqual(result['drama_status'], 'matched')

    def test_valid_and_invalid_covers_never_silently_select_the_valid_one(self):
        for reverse in (False, True):
            resources = [self.resource(), self.resource(cover='https://untrusted.invalid/cover.jpg')]
            query = Mock(return_value=encoded(*(list(reversed(resources)) if reverse else resources)))
            result = DramaMetadataResolver(query)([self.material()])[0]
            self.assertEqual((result['drama_cover_status'], result['drama_cover_url']), ('invalid', ''))
            self.assertEqual(result['drama_status'], 'matched')

    def test_invalid_cover_for_different_identity_does_not_taint_exact_match(self):
        query = Mock(return_value=encoded(self.resource(), self.resource(language='en', cover='bad'),
                                         self.resource(content_id='dramaa', cover='bad')))
        result = DramaMetadataResolver(query)([self.material()])[0]
        self.assertEqual((result['drama_cover_status'], result['drama_cover_url']), ('available', DRAMA_COVER))

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
        self.drama = dict(content_id=self.material['content_id'], language='zh-tw', name='繁體劇名', desc='繁體劇情簡介', cover=DRAMA_COVER)
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
        app = Mock(PUBLIC_BASE_URL='https://example.invalid', DB_NAME='kunlunads_dev', JOB_DB_PATH=str(self.root / 'jobs.sqlite3'))
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
