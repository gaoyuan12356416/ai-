"""Isolated template persistence and publishing regressions; no platform writes."""
import copy
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import Mock

import test_youtube_auto_service as fixture
from test_youtube_channel_cache import CRED
from features.youtube_auto_publish.channels import ChannelDirectory
from features.youtube_auto_publish.channel_templates import ChannelTemplateStore
from features.youtube_auto_publish.templates import WorkflowError


class Templates(unittest.TestCase):
    setUp = fixture.WorkflowCase.setUp
    tearDown = fixture.WorkflowCase.tearDown
    payload = fixture.WorkflowCase.payload

    def config(self, **changes):
        value = dict(channel_id=self.channel['channel_id'],version=0,
                     title_template='',description_template='',comment_template='')
        value.update(changes)
        return value

    def save(self, actor=None, **changes):
        return self.service.save_channel_template(actor or self.actor, self.channel['id'], self.config(**changes))['template']

    def get(self, actor=None):
        return self.service.get_channel_template(actor or self.actor, self.channel['id'])['template']

    def test_empty_partial_and_whitespace(self):
        self.assertEqual(self.get()['version'],0)
        value = self.save(title_template=' \n',description_template='  {desc}\nWatch {url}  ',comment_template='\t')
        self.assertEqual(value['title_template'],'')
        self.assertEqual(value['description_template'],'{desc}\nWatch {url}')
        self.assertEqual(value['comment_template'],'')
        self.assertEqual(self.service.channel_list(self.actor)['channels'][0]['template']['configured_fields'],['description'])
        self.save(version=1)
        self.assertEqual(self.get()['version'],2)
        self.assertEqual(self.service.channel_list(self.actor)['channels'][0]['template']['configured_fields'],[])

    def test_shared_publishers_and_tenant_isolation(self):
        self.save(description_template='Shared')
        self.assertEqual(self.get(self.other)['description_template'],'Shared')
        self.assertEqual(self.get(self.foreign)['version'],0)
        self.save(self.other,version=1,comment_template='Reply')
        self.assertEqual(self.get()['updated_by'],self.other['user_id'])

    def test_conflict_and_concurrent_first_save(self):
        def attempt(text):
            try:return self.save(description_template=text)['version']
            except WorkflowError as exc:return exc.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes=list(pool.map(attempt,['One','Two']))
        self.assertCountEqual(outcomes,[1,'template_conflict'])
        with self.assertRaises(WorkflowError) as ctx:self.save(version=0,title_template='stale')
        self.assertEqual(ctx.exception.status,409)

    def test_invalid_macro_identity_and_version(self):
        for changes in [dict(title_template='{bad}'),dict(description_template='{{name}}'),
                        dict(channel_id='UCother'),dict(version=True),dict(title_template=None),
                        dict(comment_template='x'*1001),dict(title_template='x'*101),
                        dict(description_template='汉'*1667)]:
            with self.subTest(changes=str(changes)[:80]),self.assertRaises(WorkflowError):self.save(**changes)
        self.assertEqual(self.get()['version'],0)

    def test_source_config_independent_and_safe_list(self):
        self.source.state=Mock(side_effect=AssertionError('must not read material configuration'))
        result=self.service.channel_list(self.actor)
        self.assertEqual(len(result['channels']),1)
        self.assertNotIn('scopes',result['channels'][0])
        self.assertNotIn('youtube_account_id',result['channels'][0])

    def test_blocked_channel_can_edit_but_identity_is_fresh(self):
        repo=Mock();repo._query.return_value=[CRED]
        directory=ChannelDirectory(repo,probe=Mock(side_effect=AssertionError('do not probe while editing')))
        self.service.channel_directory=directory
        payload=self.config(channel_id=CRED.channel_id)
        self.service.save_channel_template(self.actor,CRED.channel_local_id,payload)
        directory.probe.assert_not_called()
        repo._query.return_value=[]
        with self.assertRaises(WorkflowError) as ctx:self.service.get_channel_template(self.actor,CRED.channel_local_id)
        self.assertEqual(ctx.exception.status,404)

    def test_publish_uses_submitted_values_and_freezes_idempotently(self):
        self.save(title_template='Shared title',description_template='Shared description')
        request=self.payload(title_template='Manual {name}',description_template='Manual description')
        before=self.service.create_task(self.actor,request)['task']
        self.save(version=1,title_template='Later title')
        again=self.service.create_task(self.actor,copy.deepcopy(request))['task']
        self.assertEqual(before['id'],again['id'])
        self.assertEqual(again['title'],'Manual Test Drama')
        self.assertEqual(again['description'],'Manual description')
        self.assertEqual(again['comment'],'')

    def test_cover_defaults_shared_isolated_clear_and_literal(self):
        self.assertEqual(self.get()['cover_requirements_template'], '')
        value = self.save(cover_requirements_template='  16:9 横版\n保留 {name} 和 {anything} 原文  ')
        self.assertEqual(value['cover_requirements_template'], '16:9 横版\n保留 {name} 和 {anything} 原文')
        self.assertEqual(self.get(self.other)['cover_requirements_template'], value['cover_requirements_template'])
        self.assertEqual(self.get(self.foreign)['cover_requirements_template'], '')
        self.assertEqual(self.service.channel_list(self.actor)['channels'][0]['template']['configured_fields'], ['cover_requirements'])
        self.save(version=1, cover_requirements_template=' \n\t')
        self.assertEqual(self.get()['cover_requirements_template'], '')

    def test_cover_limit_and_invalid_types(self):
        self.save(cover_requirements_template='封'*2000)
        for text in ('封'*2001, None, 123, [], {}):
            with self.subTest(text=str(text)[:30]), self.assertRaises(WorkflowError):
                self.save(version=1, cover_requirements_template=text)
        self.assertEqual(self.get()['version'], 1)
        self.assertEqual(len(self.get()['cover_requirements_template']), 2000)

    def test_legacy_client_omission_retains_saved_cover(self):
        self.save(cover_requirements_template='Saved cover')
        result = self.save(version=1, title_template='Legacy title')
        self.assertEqual(result['cover_requirements_template'], 'Saved cover')
        self.assertEqual(self.get()['cover_requirements_template'], 'Saved cover')
        with self.assertRaises(WorkflowError):
            self.save(version=1, cover_requirements_template='Stale cover')
        self.assertEqual(self.get()['cover_requirements_template'], 'Saved cover')

    def test_legacy_schema_migration_is_repeatable_and_preserves_rows(self):
        with self.service.db(True) as c:
            c.execute('DROP TABLE youtube_channel_template')
            c.execute('''CREATE TABLE youtube_channel_template(
                tenant TEXT NOT NULL, channel_id TEXT NOT NULL,
                title_template TEXT NOT NULL DEFAULT '', description_template TEXT NOT NULL DEFAULT '',
                comment_template TEXT NOT NULL DEFAULT '',version INTEGER NOT NULL,
                updated_by TEXT NOT NULL,updated_by_name TEXT NOT NULL,updated_at TEXT NOT NULL,
                PRIMARY KEY(tenant,channel_id))''')
            c.execute('INSERT INTO youtube_channel_template VALUES(?,?,?,?,?,?,?,?,?)',
                      (self.actor['tenant_key'], self.channel['channel_id'], 'Old title', 'Old description',
                       'Old comment', 7, 'editor', 'Editor', '2026-10-14T00:00:00Z'))
            before = tuple(c.execute('SELECT * FROM youtube_channel_template').fetchone())
        def migrate(_):
            ChannelTemplateStore(self.service.db)
        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(migrate, range(3)))
        ChannelTemplateStore(self.service.db)
        with self.service.db() as c:
            after = tuple(c.execute('SELECT * FROM youtube_channel_template').fetchone())
        self.assertEqual(after[:-1], before)
        self.assertEqual(after[-1], '')
        self.assertEqual(self.get()['version'], 7)

    def test_cover_submission_frozen_for_retry_and_generation(self):
        self.save(cover_requirements_template='Default cover')
        request = self.payload(requirements='Final edited cover')
        first = self.service.create_task(self.actor, request)['task']
        self.save(version=1, cover_requirements_template='Later default')
        repeated = self.service.create_task(self.actor, copy.deepcopy(request))['task']
        self.assertEqual(first['id'], repeated['id'])
        self.assertEqual(repeated['requirements'], 'Final edited cover')
        self.service.run_once()
        self.assertEqual(self.generate.call_args.args[0]['requirements'], 'Final edited cover')


if __name__=='__main__':unittest.main()
