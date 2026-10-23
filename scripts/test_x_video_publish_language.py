"""Real admin language transactions against temporary account/Post databases.

No OAuth, token files, media access or platform writes are used. Only profile
decoding is replaced so the real setter and SQLite attachment paths execute.
"""
from __future__ import annotations

from contextlib import ExitStack, closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from features.x_accounts import oauth_service as oauth
from features.x_posts.service import XPostStore
from features.x_video_publish.core import PersonalStore
from features.x_video_publish.pool import PoolService, install_schema


OWNER={"tenant_key":"fixture-tenant","user_id":"fixture-owner","name":"Fixture owner","role":"user"}
ADMIN={"tenant_key":"fixture-tenant","user_id":"fixture-admin","name":"Fixture admin","role":"admin"}
OLD="2029-01-01T00:00:00Z"
NEW="2030-01-01T00:00:00Z"


def connect(path):
    conn=sqlite3.connect(str(path))
    conn.row_factory=sqlite3.Row
    return conn


def rows(path,table):
    with closing(connect(path)) as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM "+table+" ORDER BY 1")]


@contextmanager
def fixture(*,split=False,pools=True):
    with tempfile.TemporaryDirectory() as directory,ExitStack() as stack:
        root=Path(directory)
        accounts=root/"accounts.sqlite"
        posts=root/"posts.sqlite" if split else accounts
        for key,value in {"DATA_DIR":root,"DB_PATH":accounts,"POST_DB_PATH":posts,"TOKENS_DIR":root/"tokens"}.items():
            stack.enter_context(patch.object(oauth,key,value))
        stack.enter_context(patch.object(oauth,"row_to_item",lambda row:{**dict(row),"publish_approved":bool(row["publish_approved"]),"publish_eligible":True}))
        stack.enter_context(patch.object(oauth,"iso_utc",return_value=NEW))
        stack.enter_context(patch.object(oauth,"read_token_file",side_effect=AssertionError("No token reads in this test")))
        oauth.ensure_storage()
        with closing(connect(accounts)) as conn,conn:
            for aid in (1,2):
                conn.execute("""INSERT INTO x_authorized_account(id,x_user_id,username,display_name,status,
                  publish_approved,drama_language,owner_tenant_key,owner_user_id,first_authorized_at,last_authorized_at,
                  created_at,updated_at) VALUES(?,?,?,?,'active',1,'en',?,?,?,?,?,?)""",
                  (aid,str(9000000000000000000+aid),"fixture"+str(aid),"Fixture "+str(aid),OWNER["tenant_key"],OWNER["user_id"],OLD,OLD,OLD,OLD))
        personal=PersonalStore(posts)
        if pools:
            with closing(connect(posts)) as conn,conn:
                install_schema(conn)
                for aid in (1,2):
                    conn.execute("""INSERT INTO x_video_publish_pool(tenant_key,user_id,account_id,real_account_id,
                      version,status,times_json,processing_mode,language,effective_at_utc,created_at_utc,updated_at_utc)
                      VALUES(?,?,?,?,7,'enabled','["09:00"]','original','en',?,?,?)""",
                      (OWNER["tenant_key"],OWNER["user_id"],aid,str(9000000000000000000+aid),OLD,OLD,OLD))
                conn.execute("""INSERT INTO x_video_publish_pool_item(tenant_key,user_id,account_id,material_id,
                  position,material_name,language,material_json,created_at_utc,updated_at_utc)
                  VALUES(?,?,1,'102',1,'Pending fixture','en','{}',?,?)""",(OWNER["tenant_key"],OWNER["user_id"],OLD,OLD))
        yield SimpleNamespace(root=root,accounts=accounts,posts=posts,store=personal)


class LanguageTransactionTests(unittest.TestCase):
    def freeze_history(self,f):
        account=oauth.find_account(1)
        material={"material_id":"101","material_name":"Frozen video","drama_name":"Frozen drama",
                  "drama_description":"Frozen description","language":"en"}
        proof=f.store.preview(OWNER,{"source":"manual","rows":[{"account_id":"1","material_id":"101","caption":"Frozen {name}"}]},
                              {"1":account},{"101":material},now=datetime(2029,1,1,tzinfo=timezone.utc))
        batch=f.store.commit(OWNER,proof["preview_token"],"fixture-frozen-history",{"1":account},{"101":material},
                             now=datetime(2029,1,1,tzinfo=timezone.utc))
        queue=XPostStore(f.posts).enqueue({"source_type":"material","account_id":1,"account_username":"fixture1",
            "account_drama_language":"en","source_date":"2028-12-31","material_id":"999","content_id":"fixture-content",
            "material_url":"https://media.example.invalid/frozen.mp4","material_name":"Frozen queue","material_language":"en",
            "drama_name":"Frozen drama","tag":"Romance","description":"Frozen queue description",
            "page_name":"Fixture 1","page_id":"9000000000000000001"})
        return batch,queue

    def check_pause(self,split):
        with fixture(split=split) as f:
            self.freeze_history(f)
            before_tasks=rows(f.posts,"x_video_publish_task")
            before_queue=rows(f.posts,"x_post_queue")
            before_items=rows(f.posts,"x_video_publish_pool_item")
            before_other=rows(f.posts,"x_video_publish_pool")[1]
            result=oauth.set_account_drama_language(1,"JP",ADMIN)
            self.assertEqual(result["drama_language"],"ja")
            self.assertTrue(result["personal_pool_paused"])
            pools=rows(f.posts,"x_video_publish_pool")
            self.assertEqual((pools[0]["status"],pools[0]["language"],pools[0]["version"]),("paused","ja",8))
            self.assertEqual(pools[0]["pause_reason"],"account_language_changed")
            self.assertEqual(pools[0]["effective_at_utc"],NEW)
            self.assertEqual(pools[1],before_other)
            self.assertEqual(rows(f.posts,"x_video_publish_task"),before_tasks)
            self.assertEqual(rows(f.posts,"x_post_queue"),before_queue)
            self.assertEqual(rows(f.posts,"x_video_publish_pool_item"),before_items)
            view=PoolService(f.store,lambda actor,aid:oauth.find_account(int(aid)),lambda actor,mids:{},lambda t,u:OWNER,enabled=True)
            self.assertTrue(view.listing(OWNER,"1")["items"][0]["language_mismatch"])
            self.assertEqual(rows(f.accounts,"x_authorized_account")[0]["drama_language"],"ja")

    def test_same_database_pauses_pool_without_rewriting_frozen_task_queue_or_items(self):
        self.check_pause(False)

    def test_attached_post_database_pauses_pool_without_rewriting_frozen_task_queue_or_items(self):
        self.check_pause(True)

    def add_bound_drama(self,f):
        store=XPostStore(f.posts)
        with closing(connect(f.posts)) as conn,conn:
            pool_id=conn.execute("""INSERT INTO x_post_drama_pool(content_id,drama_name,description,language,
              labels,name_tag,status,free_episode_count,next_sub_number,created_at,updated_at)
              VALUES('bound-fixture','Bound fixture','Description','en','Romance','Fixture','active',3,1,?,?)""",(OLD,OLD)).lastrowid
        queue=store.enqueue({"source_type":"drama","account_id":1,"account_username":"fixture1",
            "account_drama_language":"en","source_date":"2028-12-31","material_id":"bound-fixture-1",
            "content_id":"bound-fixture","material_url":"https://media.example.invalid/bound.mp4",
            "material_name":"Bound episode","material_language":"en","drama_name":"Bound fixture",
            "tag":"Romance","description":"Description","page_name":"Fixture 1","page_id":"9000000000000000001",
            "drama_pool_item_id":pool_id,"drama_pool_created_at":OLD,"episode_number":1,"drama_replay_generation":1,"name_tag":"Fixture"})
        with closing(connect(f.posts)) as conn,conn:
            conn.execute("UPDATE x_post_drama_pool SET assigned_account_id=1,assigned_at=?,assigned_source_queue_id=? WHERE id=?",(OLD,queue["id"],pool_id))

    def check_conflict_rollback(self,split):
        with fixture(split=split) as f:
            self.add_bound_drama(f)
            before_accounts=rows(f.accounts,"x_authorized_account")
            before_pools=rows(f.posts,"x_video_publish_pool")
            before_items=rows(f.posts,"x_video_publish_pool_item")
            with self.assertRaises(oauth.ServiceError) as conflict:
                oauth.set_account_drama_language(1,"ja",ADMIN)
            self.assertEqual(conflict.exception.code,"x_account_drama_language_conflict")
            self.assertEqual(rows(f.accounts,"x_authorized_account"),before_accounts)
            self.assertEqual(rows(f.posts,"x_video_publish_pool"),before_pools)
            self.assertEqual(rows(f.posts,"x_video_publish_pool_item"),before_items)
            self.assertFalse(any(row["event_type"]=="drama_language_changed" for row in rows(f.accounts,"x_oauth_event")))

    def test_bound_drama_conflict_rolls_back_same_database(self):
        self.check_conflict_rollback(False)

    def test_bound_drama_conflict_rolls_back_attached_database(self):
        self.check_conflict_rollback(True)

    def test_without_personal_pool_table_legacy_language_update_still_works(self):
        for split in (False,True):
            with self.subTest(split=split),fixture(split=split,pools=False) as f:
                result=oauth.set_account_drama_language(1,"ja",ADMIN)
                self.assertEqual(result["drama_language"],"ja")
                self.assertNotIn("personal_pool_paused",result)
                with closing(connect(f.posts)) as conn:
                    self.assertIsNone(conn.execute("SELECT 1 FROM sqlite_master WHERE name='x_video_publish_pool'").fetchone())

    def test_same_canonical_language_does_not_pause_or_bump_version(self):
        with fixture() as f:
            before=rows(f.posts,"x_video_publish_pool")
            result=oauth.set_account_drama_language(1,"EN",ADMIN)
            self.assertNotIn("personal_pool_paused",result)
            self.assertEqual(rows(f.posts,"x_video_publish_pool"),before)

    def test_pool_write_failure_rolls_back_account_change_in_both_layouts(self):
        for split in (False,True):
            with self.subTest(split=split),fixture(split=split) as f:
                with closing(connect(f.posts)) as conn,conn:
                    conn.execute("CREATE TRIGGER fixture_pool_failure BEFORE UPDATE ON x_video_publish_pool BEGIN SELECT RAISE(ABORT,'fixture pool write failure'); END")
                before_account=rows(f.accounts,"x_authorized_account")
                before_pool=rows(f.posts,"x_video_publish_pool")
                with self.assertRaises(sqlite3.IntegrityError):
                    oauth.set_account_drama_language(1,"ja",ADMIN)
                self.assertEqual(rows(f.accounts,"x_authorized_account"),before_account)
                self.assertEqual(rows(f.posts,"x_video_publish_pool"),before_pool)


if __name__=="__main__":
    unittest.main(verbosity=2)
