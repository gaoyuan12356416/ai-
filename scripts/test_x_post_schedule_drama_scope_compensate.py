import contextlib
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from features.x_posts.service import BEIJING_TZ, XPostError, XPostStore, ensure_storage
from scripts.x_post_schedule_drama_scope_compensate import execute_compensation


class DramaScopeCompensationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = Path(self.temp.name) / "x.sqlite3"
        self.store = XPostStore(self.db)
        with sqlite3.connect(str(self.db)) as conn:
            conn.execute(
                "CREATE TABLE x_authorized_account("
                "id INTEGER PRIMARY KEY,x_user_id TEXT NOT NULL UNIQUE,"
                "username TEXT NOT NULL,display_name TEXT NOT NULL,"
                "token_store_key TEXT NOT NULL,status TEXT NOT NULL,"
                "publish_approved INTEGER NOT NULL,"
                "drama_language TEXT NOT NULL,created_at TEXT NOT NULL,"
                "updated_at TEXT NOT NULL)"
            )
            conn.commit()
        ensure_storage(self.db)
        self.now = datetime(2026, 8, 19, 14, 30, tzinfo=BEIJING_TZ)
        with sqlite3.connect(str(self.db)) as conn:
            timestamp = "2026-08-19T06:00:00Z"
            for account_id, language in [(1, "en"), (2, "en"), (3, "ja")]:
                conn.execute(
                    "INSERT INTO x_authorized_account("
                    "id,x_user_id,username,display_name,token_store_key,status,"
                    "publish_approved,drama_language,created_at,updated_at"
                    ") VALUES(?,?,?,?,?,'active',1,?,?,?)",
                    (
                        account_id,
                        "x%s" % account_id,
                        "u%s" % account_id,
                        "U%s" % account_id,
                        "token-%s" % account_id,
                        language,
                        timestamp,
                        timestamp,
                    ),
                )
            conn.execute(
                "INSERT INTO x_post_drama_pool("
                "content_id,app_id,drama_name,description,language,labels,"
                "name_tag,status,free_episode_count,next_sub_number,"
                "published_episode_count,last_checked_at,last_error_code,"
                "last_error_message,created_by_user_id,created_by_name,"
                "completed_at,created_at,updated_at"
                ") VALUES('content-en',1479,'Drama','Desc','en','[]','Drama',"
                "'active',20,1,0,'','','','u','U','','2026-08-18T00:00:00Z',?)",
                (timestamp,),
            )
            template = "Watch {{url}} {{drama_name}} {{episode_number}} {{desc}}"
            conn.execute(
                "INSERT OR REPLACE INTO x_post_schedule_config("
                "source_type,enabled,timezone,account_ids_json,publish_times_json,"
                "version,updated_by_user_id,updated_by_name,created_at,updated_at,"
                "body_template,schedule_mode,random_daily_count,random_effective_date"
                ") VALUES('drama',1,'Asia/Shanghai','[1,2]','[]',18,'u','U',?,?,?,"
                "'random',3,'2026-08-20')",
                (timestamp, timestamp, template),
            )
            conn.execute(
                "INSERT INTO x_post_schedule_random_plan("
                "source_type,run_date,config_version,account_ids_json,body_template,"
                "publish_times_json,created_at) VALUES('drama','2026-08-19',17,"
                "'[1,2,3]',?,'[\"01:18\",\"18:12\",\"22:49\"]',?)",
                (template, timestamp),
            )
            conn.execute(
                "INSERT INTO x_post_schedule_run("
                "slot_key,source_type,run_date,publish_time,timezone,config_version,"
                "account_ids_json,status,expected_count,queued_count,published_count,"
                "failed_count,unknown_count,error_code,error_message,created_at,"
                "updated_at,body_template,schedule_mode) VALUES("
                "'original','drama','2026-08-19','01:18','Asia/Shanghai',17,"
                "'[1,2,3]','failed_preflight',3,0,0,0,0,"
                "'x_post_schedule_drama_shortage',"
                "'short-drama pool has fewer free episodes than this schedule requires',"
                "?,?,?,'random')",
                (timestamp, timestamp, template),
            )
            self.run_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            conn.commit()

    def tearDown(self):
        self.temp.cleanup()

    @contextlib.contextmanager
    def lock(self, _path):
        yield object()

    def call(self, validate_only=False):
        return execute_compensation(
            self.run_id,
            "x_post_schedule_drama_shortage",
            actor="codex_user_authorized_20260819",
            deployed_commit="d" * 40,
            compensation_publish_time="14:29",
            validate_only=validate_only,
            db_path=self.db,
            lock_factory=self.lock,
            now=self.now,
        )

    def test_validate_only_is_zero_write(self):
        result = self.call(validate_only=True)
        self.assertEqual(result["status"], "validated")
        with sqlite3.connect(str(self.db)) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT account_ids_json,config_version FROM "
                    "x_post_schedule_random_plan"
                ).fetchone(),
                ("[1,2,3]", 17),
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM "
                    "x_post_schedule_drama_scope_compensation_audit"
                ).fetchone()[0],
                0,
            )

    def test_apply_creates_child_and_contracts_only_future_plan(self):
        result = self.call()
        self.assertEqual(result["status"], "created")
        self.assertEqual(result["removed_account_ids"], [3])
        with sqlite3.connect(str(self.db)) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT account_ids_json,config_version,publish_times_json "
                    "FROM x_post_schedule_random_plan"
                ).fetchone(),
                ("[1,2]", 18, '["01:18","18:12","22:49"]'),
            )
            child = conn.execute(
                "SELECT status,account_ids_json,expected_count,publish_time "
                "FROM x_post_schedule_run WHERE id=?",
                (result["compensation_run_id"],),
            ).fetchone()
            self.assertEqual(child, ("claimed", "[1,2]", 2, "14:29"))
            original = conn.execute(
                "SELECT status,error_code FROM x_post_schedule_run WHERE id=?",
                (self.run_id,),
            ).fetchone()
            self.assertEqual(
                original,
                ("failed_preflight", "x_post_schedule_drama_shortage"),
            )

    def test_second_apply_fails_closed(self):
        self.call()
        with self.assertRaises(XPostError) as raised:
            self.call()
        self.assertEqual(
            raised.exception.code,
            "x_post_drama_scope_compensation_conflict",
        )

    def test_reordered_replacement_scope_fails_closed(self):
        with sqlite3.connect(str(self.db)) as conn:
            conn.execute(
                "UPDATE x_post_schedule_config SET account_ids_json='[2,1]' "
                "WHERE source_type='drama'"
            )
            conn.commit()
        with self.assertRaises(XPostError) as raised:
            self.call(validate_only=True)
        self.assertEqual(
            raised.exception.code,
            "x_post_drama_scope_compensation_conflict",
        )

    def test_existing_future_run_fails_closed(self):
        with sqlite3.connect(str(self.db)) as conn:
            conn.execute(
                "INSERT INTO x_post_schedule_run("
                "slot_key,source_type,run_date,publish_time,timezone,"
                "config_version,account_ids_json,status,expected_count,"
                "queued_count,published_count,failed_count,unknown_count,"
                "created_at,updated_at,body_template,schedule_mode) "
                "SELECT 'future','drama','2026-08-19','18:12',timezone,"
                "config_version,account_ids_json,'claimed',expected_count,"
                "0,0,0,0,created_at,updated_at,body_template,'random' "
                "FROM x_post_schedule_run WHERE id=?",
                (self.run_id,),
            )
            conn.commit()
        with self.assertRaises(XPostError) as raised:
            self.call(validate_only=True)
        self.assertEqual(
            raised.exception.code,
            "x_post_drama_scope_compensation_conflict",
        )


if __name__ == "__main__":
    unittest.main()
