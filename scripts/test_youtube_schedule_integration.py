"""Offline end-to-end contract across preparation DTO and real ledger/engine."""
import base64
import unittest
from types import SimpleNamespace
from scripts import test_youtube_schedule_engine as fixtures
from features.youtube_auto_publish.service import YouTubeWorkflow


class IntegrationTests(unittest.TestCase):
    setUp=fixtures.ScheduleTests.setUp
    due=fixtures.ScheduleTests.due
    tick=fixtures.ScheduleTests.tick
    row=fixtures.ScheduleTests.row

    def create_workflow(self):
        self.actor={'tenant_key':'test','user_id':'offline_actor','role':'user'}
        source=SimpleNamespace(get=lambda _:dict(id='90001',name='Offline',source_job_id='a'*32,
            content_id='drama1',app_id='1479',url='https://media.example.test/video.mp4',source_kind='custom_source'))
        channels=lambda _:[dict(id='12',name='Offline',channel_id=fixtures.fixtures.CHANNEL,
            youtube_account_id='11',eligible=True,comment_eligible=True,scopes=[fixtures.fixtures.COMMENT_SCOPE])]
        self.workflow=YouTubeWorkflow(self.store.db_path,self.covers,source,channels,lambda _:'',self.store)
        asset=self.workflow.upload_cover(self.actor,{'data':base64.b64encode(self.cover.read_bytes()).decode()})['asset']
        self.task=self.workflow.create_task(self.actor,dict(operation_id='integration_operation_0001',material_id='90001',
            channel_id='12',title_template='Test',description_template='Test description',comment_template='Test comment',
            cover_source='local',cover_asset_id=asset['id'],publish_at=self.target))['task']
        self.workflow.run_once()

    def dto(self):return self.workflow.get_task(self.actor,self.task['id'])['task']

    def test_preparation_native_schedule_and_actual_public_dto(self):
        self.create_workflow()
        self.assertEqual(self.dto()['publish_at'],self.target)
        self.tick();self.tick()
        self.assertEqual(self.dto()['status'],'scheduled')
        self.assertEqual(self.dto()['steps'][3]['message'],'YouTube 已确认预约，等待到点公开')
        self.assertEqual(self.client.comments,[])
        self.now='2030-01-02T11:00:01Z';self.client.visibility='public';self.client.remote_time=''
        self.tick()
        self.assertEqual(self.dto()['status'],'published')
        self.assertEqual(len(self.client.comments),1)
        self.assertEqual(len(self.media.uploaded),1)

    def test_control_dto_waits_for_cancel_readback_and_retains_video(self):
        self.create_workflow();self.tick();self.tick()
        current=self.dto()
        self.workflow.schedule(self.actor,self.task['id'],dict(action='cancel',schedule_version=current['schedule_version'],
            operation_id='integration_cancel_0001'))
        waiting=self.dto()
        self.assertEqual(waiting['status'],'schedule_pending')
        self.assertEqual(waiting['publish_at'],self.target)
        self.assertFalse(waiting['can_schedule'])
        self.tick()
        canceled=self.dto()
        self.assertEqual(canceled['status'],'cancelled')
        self.assertEqual(canceled['video_id'],fixtures.fixtures.VIDEO)
        self.assertFalse(canceled['can_retry'])
        self.assertEqual(self.client.visibility,'private')
        self.assertEqual(self.client.comments,[])


if __name__=='__main__':unittest.main()
