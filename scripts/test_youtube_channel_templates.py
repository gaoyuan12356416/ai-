"""Isolated template persistence and publishing regressions; no platform writes."""
import copy
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import Mock

import test_youtube_auto_service as fixture
from test_youtube_channel_cache import CRED
from features.youtube_auto_publish.channels import ChannelDirectory
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


if __name__=='__main__':unittest.main()
