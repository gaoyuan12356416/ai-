"""Reference cover freezing and workflow regression; no network or platform writes."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from test_youtube_auto_service import WorkflowCase, png
from features.youtube_auto_publish.templates import WorkflowError


class ReferenceWorkflowCase(WorkflowCase):
    def test_missing_reference_blocks_ai_before_link_but_local_still_works(self):
        self.material.update(drama_cover_status='missing',drama_cover_url='')
        self.assert_error('reference_cover_missing',lambda:self.create())
        self.short_link.assert_not_called()
        self.fetch_reference.assert_not_called()
        asset=self.cover()
        self.create(cover_source='local',cover_asset_id=asset['id'])
        self.service.run_once()
        self.generate.assert_not_called()
        self.fetch_reference.assert_not_called()

    def test_client_reference_fields_never_override_server_material(self):
        self.material.update(drama_cover_status='ambiguous',drama_cover_url='')
        payload=self.payload(reference_cover={'asset_id':'f'*32},drama_cover_url='https://static-v1.mydramawave.com/fake.jpg')
        self.assert_error('reference_cover_ambiguous',lambda:self.service.create_task(self.actor,payload))
        self.short_link.assert_not_called()

    def test_reference_is_downloaded_by_worker_and_privately_frozen(self):
        created=self.create()
        self.fetch_reference.assert_not_called()
        self.assertIsNone(created['reference_cover'])
        self.service.run_once()
        used=self.generate.call_args.args[0]['reference_cover']
        self.fetch_reference.assert_called_once_with(self.material['drama_cover_url'])
        asset=self.service.asset(self.actor,used['asset_id'])
        self.assertEqual(hashlib.sha256(Path(asset['path']).read_bytes()).hexdigest(),used['sha256'])
        self.assert_error('not_found',lambda:self.service.asset(self.other,used['asset_id']),404)
        self.assert_error('not_found',lambda:self.service.asset(self.foreign,used['asset_id']),404)
        dto=self.service.get_task(self.actor,created['id'])['task']['reference_cover']
        self.assertEqual(dto,{'url':self.service.asset_url(used['asset_id']),'drama_name':'Test Drama','frozen':True})

    def test_reject_reuses_reference_when_remote_cover_changes(self):
        task=self.ready()
        first=copy.deepcopy(self.generate.call_args.args[0]['reference_cover'])
        self.material['drama_cover_url']='https://static-v1.mydramawave.com/covers/different.jpg'
        self.fetch_reference.side_effect=AssertionError('must reuse original snapshot')
        self.service.review(self.actor,task['id'],{'action':'reject','version':1,'feedback':'Keep the same actors, increase contrast'})
        self.service.run_once()
        self.service.run_once()  # an existing outbox may be serviced first
        self.assertEqual(self.generate.call_count,2)
        self.assertEqual(self.generate.call_args.args[0]['reference_cover'],first)
        self.assertEqual(self.generate.call_args.args[1]['feedback'],'Keep the same actors, increase contrast')

    def test_failed_generation_retry_reuses_original(self):
        self.generate.side_effect=RuntimeError('test failure')
        task=self.create();self.service.run_once()
        original=self.generate.call_args.args[0]['reference_cover']['asset_id']
        self.generate.side_effect=None
        self.service.retry(self.actor,task['id'])
        self.service.run_once()
        self.assertEqual(self.generate.call_args.args[0]['reference_cover']['asset_id'],original)
        self.fetch_reference.assert_called_once()

    def test_download_error_is_visible_without_text_only_generation(self):
        self.fetch_reference.side_effect=WorkflowError('reference_cover_download_failed','原剧封面下载失败',503)
        task=self.create();self.service.run_once()
        dto=self.service.get_task(self.actor,task['id'])['task']
        self.assertEqual(dto['status'],'generation_failed')
        self.assertEqual(dto['error']['code'],'reference_cover_download_failed')
        self.generate.assert_not_called();self.notify.assert_not_called()
        self.assertEqual(self.store.tasks,{})

    def test_tampered_reference_stops_retry(self):
        self.generate.side_effect=RuntimeError('test failure')
        task=self.create();self.service.run_once()
        reference=self.generate.call_args.args[0]['reference_cover']
        asset=self.service.asset(self.actor,reference['asset_id'])
        Path(asset['path']).write_bytes(b'changed')
        self.service.retry(self.actor,task['id']);self.service.run_once()
        self.assertEqual(self.generate.call_count,1)
        self.assertEqual(self.service.get_task(self.actor,task['id'])['task']['status'],'generation_failed')

    def test_old_pending_task_resolves_matching_drama_before_generation(self):
        task=self.create()
        with self.service.db(True) as c:
            row,body=self.service._row(c,task['id'])
            for field in ('drama_cover_url','drama_cover_status','drama_cover_message'):
                body['material'].pop(field,None)
            body.pop('reference_cover',None);self.service._save(c,body)
        self.service.run_once()
        self.assertEqual(self.generate.call_count,1)
        self.assertTrue(self.service.get_task(self.actor,task['id'])['task']['reference_cover']['frozen'])

    def test_old_task_refuses_changed_content_id(self):
        task=self.create()
        with self.service.db(True) as c:
            row,body=self.service._row(c,task['id'])
            body['material'].pop('drama_cover_status',None);self.service._save(c,body)
        self.material['content_id']='different-drama'
        self.service.run_once()
        self.assertEqual(self.service.get_task(self.actor,task['id'])['task']['error']['code'],'reference_drama_changed')
        self.generate.assert_not_called();self.fetch_reference.assert_not_called()


if __name__=='__main__':
    # Reuse fixtures without redundantly repeating the parent regression suite.
    names=[name for name in ReferenceWorkflowCase.__dict__ if name.startswith('test_')]
    result=unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(ReferenceWorkflowCase(name) for name in names))
    sys.exit(not result.wasSuccessful())
