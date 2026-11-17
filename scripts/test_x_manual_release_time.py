"""Offline manual scheduling acceptance; never calls X or production databases."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from features.x_posts.service import XPostError, XPostStore
from features.x_video_publish.core import PersonalStore
from features.x_video_publish.service import PublishingService
from features.x_video_publish.sources import SourceRepository, SourceClient

NOW = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)
RELEASE = datetime(2030, 1, 1, 16, tzinfo=timezone.utc)  # Beijing Jan 2 midnight.
ACTOR = {'tenant_key': 'fixture', 'user_id': 'operator', 'name': 'Operator'}


class ManualReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'posts.sqlite3'
        self.store = PersonalStore(self.path)
        self.accounts = {'1': dict(id=1, owner_tenant_key='fixture', owner_user_id='operator',
            status='active', publish_approved=True, publish_eligible=True, drama_language='en')}
        self.materials = {'501': dict(material_id='501', drama_name='Drama', language='en',
            duration_seconds=45, drama_deploy_time=int(RELEASE.timestamp()))}

    def tearDown(self):
        self.tmp.cleanup()

    def preview(self, requested='', source='manual'):
        return self.store.preview(ACTOR, {'source': source, 'rows': [dict(account_id='1',
            material_id='501', caption='Fixture', scheduled_at=requested)]},
            self.accounts, self.materials, now=NOW)

    def commit(self, preview):
        return self.store.commit(ACTOR, preview['preview_token'], 'fixture-operation',
            self.accounts, self.materials, now=NOW)

    def test_immediate_before_release_is_scheduled_and_cannot_be_claimed_early(self):
        preview = self.preview()
        self.assertTrue(preview['rows'][0]['valid'])
        self.assertTrue(preview['rows'][0]['schedule_adjusted'])
        dto = PublishingService.preview_dto(preview)
        self.assertEqual('2030-01-01T16:00:00Z', dto['items'][0]['planned_at_utc'])
        batch = self.commit(preview)
        task = batch['tasks'][0]
        legacy = XPostStore(self.path)
        run = legacy.get_manual_run(task['manual_run_id'])
        self.assertEqual('scheduled', run['publish_mode'])
        self.assertEqual('2030-01-01T16:00:00Z', run['scheduled_at'])
        self.assertEqual('2030-01-02', run['run_date'])
        with patch('features.x_posts.service.utc_now', return_value='2030-01-01T15:59:59Z'):
            self.assertFalse(legacy.claim_manual_run()['found'])
        with patch('features.x_posts.service.utc_now', return_value='2030-01-01T16:00:00Z'):
            self.assertEqual(run['id'], legacy.claim_manual_run()['run']['id'])

    def test_earlier_and_even_past_requested_time_are_clamped(self):
        for requested in ('2030-01-01 22:00', '2030-01-01 18:00'):
            with self.subTest(requested=requested):
                row = self.preview(requested)['rows'][0]
                self.assertTrue(row['valid'], row)
                self.assertEqual('2030-01-01T16:00:00Z', row['scheduled_at'])
                self.assertTrue(row['schedule_adjusted'])

    def test_equal_and_later_requested_times_are_preserved(self):
        for requested, expected in [('2030-01-02 00:00', '2030-01-01T16:00:00Z'),
                                    ('2030-01-02 00:01', '2030-01-01T16:01:00Z'),
                                    ('2030-01-01T16:02:00.000Z', '2030-01-01T16:02:00Z')]:
            with self.subTest(requested=requested):
                row = self.preview(requested)['rows'][0]
                self.assertTrue(row['valid'], row)
                self.assertEqual(expected, row['scheduled_at'])
                self.assertFalse(row['schedule_adjusted'])

    def test_released_drama_keeps_immediate_and_rejects_past_schedule(self):
        self.materials['501']['drama_deploy_time'] = int(NOW.timestamp())
        self.assertEqual('', self.preview()['rows'][0]['scheduled_at'])
        self.assertEqual('x_video_schedule_past', self.preview('2030-01-01 19:00')['rows'][0]['error_code'])

    def test_material_without_release_metadata_keeps_existing_policy(self):
        self.materials['501'].pop('drama_deploy_time')
        self.assertEqual('', self.preview()['rows'][0]['scheduled_at'])

    def test_invalid_requested_time_is_not_hidden_by_clamping(self):
        self.assertEqual('x_video_schedule_invalid', self.preview('invalid')['rows'][0]['error_code'])

    def test_release_delayed_after_preview_requires_new_confirmation(self):
        preview = self.preview()
        self.materials['501']['drama_deploy_time'] += 3600
        with self.assertRaises(XPostError) as caught:
            self.commit(preview)
        self.assertEqual('x_video_release_time_changed', caught.exception.code)
        with closing(self.store.connect()) as db:
            self.assertEqual(0, db.execute('SELECT count(*) FROM x_video_publish_task').fetchone()[0])

    def test_confirmed_idempotent_replay_keeps_original_schedule(self):
        preview = self.preview()
        first = self.commit(preview)
        self.materials['501']['drama_deploy_time'] += 3600
        repeated = self.commit(preview)
        self.assertEqual(first['id'], repeated['id'])
        self.assertEqual(first['tasks'][0]['scheduled_at'], repeated['tasks'][0]['scheduled_at'])
        self.assertFalse(repeated['created'])

    def test_service_preview_and_commit_request_scheduling_source(self):
        source = Mock()
        source.resolve.return_value = {'items': self.materials, 'errors': {}}
        oauth = SimpleNamespace(POST_DB_PATH=self.path, INTERNAL_TOKEN='fixture-token',
            require_actor_subject=lambda actor: actor)
        service = PublishingService(oauth, source=source, enabled=True)
        service.accounts = lambda actor: self.accounts
        with patch('features.x_video_publish.core._now', return_value=NOW):
            preview = service.preview(ACTOR, dict(material_ids=['501'], account_ids=['1'],
                pairs=[dict(material_id='501', account_id='1', caption_template='Fixture')]))
            service.dispatch('POST', '/batches', ACTOR,
                dict(preview_id=preview['preview_id'], idempotency_key='fixture-service'))
        self.assertEqual(2, source.resolve.call_count)
        for call in source.resolve.call_args_list:
            self.assertEqual({'allow_future_deploy': True}, call.kwargs)


class SourceReleaseTests(unittest.TestCase):
    def setUp(self):
        self.raw = [dict(material_id='501', product='Fixture', source_app_id=1479,
            material_type=2, material_is_deleted=0, material_url='https://media.example.invalid/1.mp4',
            material_name='Video', material_language='en', content_id='C1', source_tag_name='', video_duration=45)]
        self.dramas = [dict(content_id='C1', series_code='S1', language='en',
            drama_name='Drama', drama_labels='Romance', drama_description='Story')]
        self.deploy = [dict(content_id='C1', app_id=1479, app='a', language='en',
            deploy_time=int(RELEASE.timestamp()))]
        self.repo = SourceRepository(SimpleNamespace(schema='fixture'))
        self.repo._read_exact_bundle = lambda *args: (self.raw, (self.dramas, self.deploy))

    def test_manual_source_accepts_future_release_but_default_stays_strict(self):
        strict = self.repo.resolve(['501'])
        self.assertEqual('drama_not_yet_deliverable', strict['errors']['501']['code'])
        manual = self.repo.resolve(['501'], allow_future_deploy=True)
        self.assertEqual(int(RELEASE.timestamp()), manual['items']['501']['drama_deploy_time'])

    def test_latest_platform_release_wins(self):
        self.deploy.append({**self.deploy[0], 'app': 'b', 'deploy_time': int(RELEASE.timestamp())+120})
        manual = self.repo.resolve(['501'], allow_future_deploy=True)
        self.assertEqual(int(RELEASE.timestamp())+120, manual['items']['501']['drama_deploy_time'])

    def test_missing_and_invalid_release_are_still_rejected(self):
        for rows, code in [([], 'drama_deploy_time_missing'),
                           ([{**self.deploy[0], 'deploy_time': -1}], 'drama_deploy_time_invalid')]:
            with self.subTest(code=code):
                self.deploy = rows
                result = self.repo.resolve(['501'], allow_future_deploy=True)
                self.assertEqual(code, result['errors']['501']['code'])

    def test_internal_source_action_is_explicit(self):
        with patch('features.x_video_publish.sources.broker', return_value={}) as broker:
            client = SourceClient('fixture')
            client.resolve(['501'])
            self.assertEqual('resolve', broker.call_args.args[1]['action'])
            client.resolve(['501'], allow_future_deploy=True)
            self.assertEqual('resolve_manual_schedule', broker.call_args.args[1]['action'])


if __name__ == '__main__':
    unittest.main()
