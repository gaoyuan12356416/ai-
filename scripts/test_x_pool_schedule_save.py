"""Exercise settings saves without credentials, remote calls or live Posts."""
import contextlib
import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from features.x_accounts import oauth_service as oauth
from features.x_posts import service
from scripts import x_post_schedule_runner as runner


class PoolScheduleSaveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_path = Path(self.temp.name) / "posts.sqlite3"
        self.store = service.XPostStore(self.db_path)
        self.accounts = [dict(id=i, publish_eligible=True) for i in range(1, 96)]
        self.accounts.extend(dict(id=i, publish_eligible=False) for i in range(96, 100))
        connection = mock.MagicMock()
        connection.execute.return_value.fetchall.return_value = self.accounts
        patches = [
            mock.patch.object(oauth, "POST_DB_PATH", self.db_path),
            mock.patch.object(oauth, "db_connect", return_value=connection),
            mock.patch.object(oauth, "row_to_item", side_effect=lambda row: dict(row)),
            mock.patch.object(oauth, "verify_account", side_effect=AssertionError("no verification on save")),
            mock.patch.object(oauth.urllib.request, "urlopen", side_effect=AssertionError("no network on save")),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.settings = dict(enabled=True, timezone="Asia/Shanghai",
                             account_ids=list(range(1, 100)), publish_times=[],
                             schedule_mode="random", random_daily_count=1, version=1)
        self.actor = dict(tenant_key="test", user_id="admin", name="Admin", role="admin")

    def save(self, settings=None):
        return oauth.save_post_schedule_request(
            dict(actor=self.actor, settings=settings or self.settings),
            "material", oauth.POST_MATERIAL_POOL_NAVIGATION_ITEM,
        )["item"]

    def test_95_healthy_accounts_save_and_freeze_while_unavailable_are_excluded(self):
        original = copy.deepcopy(self.settings)
        item = self.save()
        self.assertTrue(item["enabled"])
        self.assertEqual(item["account_ids"], list(range(1, 96)))
        self.assertEqual(item["skipped_account_ids"], [96, 97, 98, 99])
        self.assertEqual(self.settings, original)
        self.assertEqual(self.store.get_schedule_config("material")["account_ids"], list(range(1, 96)))
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            plan = conn.execute("SELECT account_ids_json FROM x_post_schedule_random_plan").fetchone()
            self.assertEqual(json.loads(plan[0]), list(range(1, 96)))
            for table in ("x_post_queue", "x_post_publish_log"):
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM " + table).fetchone()[0], 0)
        # The same scope must survive the runner's frozen-slot parser.
        identity = runner._schedule_identity(dict(source_type="material", run_date="2026-11-04",
                                                 publish_time="12:37", version=item["version"],
                                                 account_ids=item["account_ids"]))
        self.assertEqual(len(identity["account_ids"]), 95)

    def test_ledger_hold_is_skipped_even_when_account_projection_is_active(self):
        with mock.patch.object(service.XPostStore, "schedule_account_blockers",
                               return_value={3: {"code": "x_post_account_locked"}}):
            item = self.save()
        self.assertNotIn(3, item["account_ids"])
        self.assertEqual(item["skipped_account_ids"], [3, 96, 97, 98, 99])

    def test_all_unavailable_saves_disabled(self):
        item = self.save(dict(self.settings, account_ids=[96, 97]))
        self.assertFalse(item["enabled"])
        self.assertEqual(item["account_ids"], [])
        self.assertEqual(item["skipped_account_ids"], [96, 97])

    def test_invalid_or_unknown_accounts_still_fail_without_saving(self):
        for ids in ([1, 999], [96, 96], [True, 96], ["invalid", 96]):
            with self.subTest(ids=ids), self.assertRaises(oauth.ServiceError):
                self.save(dict(self.settings, account_ids=ids))
        self.assertEqual(self.store.get_schedule_config("material")["version"], 1)

    def test_stale_version_and_page_permissions_are_preserved(self):
        self.save()
        with self.assertRaises(oauth.ServiceError) as caught:
            self.save()
        self.assertEqual(caught.exception.code, "x_post_schedule_version_conflict")
        self.actor["role"] = "user"
        with self.assertRaises(oauth.ServiceError) as denied:
            self.save()
        self.assertEqual(denied.exception.code, "x_admin_required")

    def test_small_selection_works_with_95_total_eligible_accounts(self):
        item = self.save(dict(self.settings, account_ids=[2]))
        self.assertEqual(item["account_ids"], [2])


if __name__ == "__main__":
    unittest.main()
