"""Shared Page selection preserves creator history and publisher identity."""
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
import uuid

from features.fb_auto_posts.core import FBAutoPostStore
from features.fb_video_publish.core import Store, DomainError
from features.fb_video_publish.service import Service, PREFIX
from features.fb_video_publish.sources import SourceRepository, SourceError
from features.fb_video_publish.worker import Worker


def actor(user, tenant="tenant"):
    return dict(tenant_key=tenant, user_id=user, user_name=user,
                is_admin=False, business_owner_id="41", sub_user_id="41")


class Catalog:
    schema = "catalog"
    blacklist_schema = "catalog"

    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("ATTACH DATABASE ':memory:' AS catalog")
        self.conn.execute("CREATE TABLE catalog.ads_facebook_page_post "
                          "(page_id TEXT,page_name TEXT,language TEXT,status INT,page_access_token TEXT)")
        self.conn.executemany("INSERT INTO catalog.ads_facebook_page_post VALUES(?,?,?,?,?)",
                              [("1001", "Shared Page", "en", 0, "fixture"),
                               ("1001", "Shared Page", "en", 1, "invalid-fixture"),
                               ("1002", "Unavailable Page", "en", 1, "fixture")])
        self.conn.create_function("CONCAT", -1, lambda *v: "".join(map(str, v)))
        self.conn.create_function("LOCATE", 2, lambda needle, haystack: haystack.find(needle) + 1)

    def select(self, sql, params):
        return [dict(r) for r in self.conn.execute(sql.replace("%s", "?"), params)]


class SharedPageTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "ledger.sqlite3"
        FBAutoPostStore(self.path)
        self.store = Store(self.path)
        self.catalog = Catalog()
        self.addCleanup(self.catalog.conn.close)
        self.sources = SourceRepository(None, SimpleNamespace(mysql=self.catalog))
        self.service = Service(self.store, self.sources, "fixture-signing-key" * 3, live_enabled=True)
        self.creator, self.publisher = actor("creator"), actor("publisher")

    def test_unrelated_users_can_select_but_unavailable_authorization_stays_blocked(self):
        for user in [self.creator, self.publisher, dict(actor("admin"), is_admin=True),
                     dict(actor("unassigned"), business_owner_id="")]:
            with self.subTest(user=user["user_id"]):
                pages = self.sources.list_pages(user, q="1001")
                self.assertEqual([p["page_id"] for p in pages], ["1001"])
                self.assertEqual(pages[0]["eligible_token_count"], 1)
                self.assertEqual(self.sources.list_pages(user, q="shared")[0]["page_id"], "1001")
                self.assertEqual(self.sources.get_pages(user, ["1001"], manual=True)["1001"]["blocked_reason"], "")
                with self.assertRaises(SourceError) as error:
                    self.sources.get_pages(user, ["1002"], manual=True)
                self.assertEqual(error.exception.code, "page_unavailable")

    def test_config_shared_in_tenant_edit_preserves_creator_and_recreation_changes_it(self):
        first = self.service.handle("POST", PREFIX + "/pages", self.creator,
                                    dict(page_ids=["1001"], language="en", caption_template="hello"))["items"][0]
        self.assertEqual(self.service.configured_pages(self.publisher)["items"][0]["created_by"], "creator")
        self.assertEqual(self.service.configured_pages(actor("outside", "another-tenant"))["items"], [])
        payload = dict(config_instance_id=first["config_instance_id"], expected_version=first["version"],
                       language="en", caption_template="updated")
        edited = self.service.handle("POST", PREFIX + "/pages/1001", self.publisher, payload)["items"][0]
        self.assertEqual((edited["created_by"], edited["updated_by"]), ("creator", "publisher"))
        with self.assertRaises(DomainError) as error:
            self.service.handle("POST", PREFIX + "/pages", self.publisher,
                                dict(page_ids=["1001"], language="en", caption_template=""))
        self.assertEqual(error.exception.code, "page_already_configured")
        self.store.delete_config(self.publisher, "1001", edited["config_instance_id"], edited["version"])
        recreated = self.store.save_configs(self.publisher, ["1001"], "en", "")[0]
        self.assertEqual(recreated["created_by"], "publisher")
        self.assertNotEqual(recreated["config_instance_id"], first["config_instance_id"])

    def test_shared_config_manual_submission_freezes_actual_publisher(self):
        cfg = self.store.save_configs(self.creator, ["1001"], "en", "hello")[0]
        material = dict(material_id="501", material_name="Fixture", content_id="9001", language="en",
                        drama_name="Fixture drama", drama_description="", media_url="https://media.invalid/source.mp4")
        self.sources.get_materials = lambda ids, **kwargs: {"501": material}
        request = dict(operation_id=uuid.uuid4().hex, material_ids=["501"], page_ids=["1001"],
                       pairs=[dict(page_id="1001", material_id="501", config_instance_id=cfg["config_instance_id"],
                                   config_version=cfg["version"], caption_template="hello")],
                       processing_mode="original", schedule_mode="now", scheduled_at_utc="", interval_minutes=0)
        preview = self.service.handle("POST", PREFIX + "/preflight", self.publisher, request)
        result = self.service.handle("POST", PREFIX + "/batches", self.publisher,
                                     dict(request, preview_token=preview["preview_token"], allow_repeat=False))
        with self.store.connect() as conn:
            task = dict(conn.execute("SELECT * FROM fb_video_task WHERE batch_id=?", (result["batch_id"],)).fetchone())
        snapshot = json.loads(task["snapshot_json"])
        self.assertEqual(task["owner_user_id"], "publisher")
        self.assertEqual(snapshot["actor"]["user_id"], "publisher")
        self.assertEqual(snapshot["attribution"]["sub_user_id"], "41")
        self.assertEqual(self.store.configs(self.publisher)["1001"]["created_by"], "creator")
        # The worker's final source check accepts this unrelated publisher too.
        self.assertIn("1001", self.sources.get_pages(snapshot["actor"], ["1001"], manual=True))
        calls = []
        worker = Worker.__new__(Worker)
        worker.pages = SimpleNamespace(eligible_credentials=lambda pid, **kw: calls.append((pid, kw)) or [])
        worker._task_credentials(task, dict(snapshot, credential_mode="operator", credential_owner_id="41"))
        self.assertEqual(calls, [("1001", {"owner_user_id": "41"})])


if __name__ == "__main__":
    unittest.main()
