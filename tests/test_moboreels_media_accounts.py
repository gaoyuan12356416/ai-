from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from features.moboreels_library.content_tools import atomic_json
from features.moboreels_library.media_accounts import MediaAccounts
from features.moboreels_library.media_sources import MediaSource, profile_link
from features.moboreels_library.promotions import PromotionError, identity

ACTOR = {"tenant_key": "tenant", "user_id": "user", "name": "创建者", "role": "user"}
BODY = {"platform_type": 3, "accounts": [{"account_id": "12345", "nickname": "测试账号", "platform_link": "https://www.facebook.com/12345"}]}


class MediaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.settings = SimpleNamespace(data_dir=Path(self.tmp.name), institution_user_id="institution")
        self.db = Mock(); self.db._read.return_value = [BODY["accounts"][0]]
        self.provider = Mock(); self.provider._post.side_effect = self.respond
        self.manager = MediaAccounts(self.settings, self.db, self.provider, autostart=False)

    def respond(self, endpoint, body, request_id):
        self.assertEqual(endpoint, "/api/story/addplatformaccount")
        self.assertEqual(body["star_user_id"], identity("institution", ACTOR)[0])
        return SimpleNamespace(status_code=200, json=lambda: {"code": 200, "data": {"id": 88, **body}})

    def test_bulk_repeated_clicks_and_concurrency_make_one_provider_call(self):
        with ThreadPoolExecutor(max_workers=6) as executor:
            results = list(executor.map(lambda _: self.manager.submit(ACTOR, BODY), range(12)))
        self.assertEqual(len({r["list"][0]["id"] for r in results}), 1)
        with ThreadPoolExecutor(max_workers=3) as executor:
            list(executor.map(lambda _: self.manager.work(), range(3)))
        self.assertEqual(self.provider._post.call_count, 1)
        row = self.manager.list(ACTOR, {})["list"][0]
        self.assertEqual(row["status"], "succeeded")
        self.assertEqual(row["provider_id"], "88")
        self.assertEqual(len(list(self.manager.root.rglob('*.json'))), 1)
        self.assertTrue(self.manager.submit(ACTOR, BODY)["list"][0]["reused"])

    def test_mysql_failure_keeps_success_and_outbox_does_not_repeat_submission(self):
        self.db.save_media_registration.side_effect = TimeoutError()
        self.manager.submit(ACTOR, BODY); self.manager.work()
        self.assertEqual(self.manager.list(ACTOR, {})["list"][0]["status"], "succeeded")
        self.manager.submit(ACTOR, BODY); self.manager.work()
        self.assertEqual(self.provider._post.call_count, 1)
        self.db.save_media_registration.side_effect = None
        self.manager.flush_outbox()
        with self.manager.connect() as c:
            self.assertEqual(c.execute('SELECT mirror_pending FROM registrations').fetchone()[0], 0)

    def test_unknown_is_held_across_restart_and_admin_can_verify_not_created(self):
        self.provider._post.side_effect = TimeoutError()
        key = self.manager.submit(ACTOR, BODY)["list"][0]["id"]
        self.manager.work()
        self.manager = MediaAccounts(self.settings, self.db, self.provider, autostart=False)
        self.assertEqual(self.manager.submit(ACTOR, BODY)["list"][0]["status"], "unknown")
        self.manager.work(); self.assertEqual(self.provider._post.call_count, 1)
        resolution = {"outcome": "not_created", "evidence": "已人工联系供应方核对未创建"}
        with self.assertRaises(PromotionError): self.manager.resolve(ACTOR, key, resolution)
        self.manager.resolve(dict(ACTOR, role="admin"), key, resolution)
        self.provider._post.side_effect = self.respond
        self.manager.submit(ACTOR, BODY); self.manager.work()
        self.assertEqual(self.provider._post.call_count, 2)

    def test_receipt_recovers_after_crash_before_state_save(self):
        self.manager.submit(ACTOR, BODY)
        with self.manager.connect() as c:
            row=json.loads(c.execute('SELECT payload FROM registrations').fetchone()[0]);row['status']='submitting';self.manager._save(c,row)
        envelope={"http_status":200,"response":{"code":200,"data":{"id":88,**row['request']}}}
        atomic_json(self.manager.root/'receipts'/(row['request_id']+'.json'),envelope)
        recovered=MediaAccounts(self.settings,self.db,self.provider,autostart=False)
        self.assertEqual(recovered.list(ACTOR,{})['list'][0]['status'],'succeeded')
        recovered.work();self.provider._post.assert_not_called()

    def test_users_and_tenants_are_isolated_and_existing_other_owner_held(self):
        self.manager.submit(ACTOR,BODY)
        other=dict(ACTOR,user_id='someone-else')
        self.assertEqual(self.manager.list(other,{})['total'],0)
        self.assertEqual(self.manager.submit(other,BODY)['list'][0]['status'],'held')
        self.assertEqual(self.manager.list(dict(other,role='admin'),{})['total'],1)
        self.assertEqual(self.manager.list(dict(ACTOR,tenant_key='other',role='admin'),{})['total'],0)

    def test_forged_identity_and_profile_substitution_rejected_before_call(self):
        for body in [{**BODY,'star_user_id':'spoofed'}, {**BODY,'accounts':[{**BODY['accounts'][0],'platform_link':'https://www.facebook.com/99999'}]}]:
            with self.assertRaises(ValueError):self.manager.submit(ACTOR,body)
        self.provider._post.assert_not_called()

    def test_conflicting_metadata_rejected_but_missing_can_be_completed(self):
        self.db._read.return_value=[BODY['accounts'][0],{**BODY['accounts'][0],'nickname':'冲突名字'}]
        self.assertEqual(self.manager.preview(ACTOR,{'platform_type':3,'account_ids':['12345']})['list'][0]['match_status'],'conflict')
        with self.assertRaises(ValueError): self.manager.submit(ACTOR,BODY)
        self.db._read.return_value=[]
        self.assertEqual(self.manager.submit(ACTOR,BODY)['list'][0]['status'],'queued')

    def test_mismatched_provider_success_stays_unknown(self):
        self.provider._post.side_effect=lambda *args:SimpleNamespace(status_code=200,json=lambda:{'code':200,'data':{'id':1,'project_type':2,'platform_type':2}})
        self.manager.submit(ACTOR,BODY);self.manager.work()
        self.assertEqual(self.manager.list(ACTOR,{})['list'][0]['status'],'unknown')

    def test_source_exact_ids_never_internal_ids_or_credentials(self):
        source=MediaSource(self.db);self.db._read.return_value=[]
        for kind in (1,2,3):
            source.lookup(kind,['12345'])
            sql=self.db._read.call_args.args[0]
            self.assertNotIn('token',sql);self.assertNotIn('source_account_id',sql)
            self.assertNotIn('SELECT *',sql)
        for bad in ["1' OR 1=1",'@handle','123/456']:
            with self.assertRaises(ValueError):source.lookup(1,[bad])
        self.db._read.reset_mock();result=source.lookup(4,['12345'])
        self.db._read.assert_not_called();self.assertEqual(result[0]['match_status'],'missing')

    def test_profile_urls_reject_cross_platform_posts_and_credentials(self):
        for kind,url in [(1,'https://www.instagram.com/example'),(1,'https://www.tiktok.com/@x/video/123'),
                         (4,'https://evil.example/test'),(3,'https://user:secret@www.facebook.com/123'),
                         (2,'https://www.youtube.com/watch?v=123')]:
            with self.assertRaises(ValueError):profile_link(kind,url)


if __name__=='__main__':unittest.main()
