"""Offline client/sidecar integration for durable material snapshot boundaries."""
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from features.x_accounts import oauth_service
from features.x_posts import service
from scripts.x_post_schedule_runner import ScheduleSidecarClient


class SnapshotTransportTests(unittest.TestCase):
    def test_identity_reaches_store_and_boundary_is_visible_in_safe_dto(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "post.sqlite3"
            store = service.XPostStore(db)
            config = store.save_schedule_config("material", {
                "enabled": True, "timezone": "Asia/Shanghai", "account_ids": [2],
                "publish_times": ["09:00"], "version": 1,
            }, actor={"user_id": "admin", "name": "Admin"}, eligible_account_ids=[2],
                now=datetime(2026, 7, 27, 8, tzinfo=service.BEIJING_TZ))
            store.add_pool_materials(["201"], actor={"user_id": "admin", "name": "Admin"},
                                     validation_checks=[{"material_id": "201", "error_code": ""}])
            due = store.due_schedule_slots(datetime(2026, 7, 27, 9, 0, 10,
                                                  tzinfo=service.BEIJING_TZ))
            identity = {key: due["items"][0][key] for key in (
                "source_type", "run_date", "publish_time", "version", "account_ids")}
            client = ScheduleSidecarClient("http://127.0.0.1:8810", "offline-test-only")
            with mock.patch.object(oauth_service, "POST_DB_PATH", db), mock.patch.object(
                client, "post", side_effect=lambda _path, body:
                    oauth_service.available_post_material_pool_request(body)
            ):
                first = client.available_pool_items("/internal/posts/material-pool/available", 1000,
                                                    schedule_identity=identity)
                store.add_pool_materials(["202"], actor={"user_id": "admin", "name": "Admin"},
                                         validation_checks=[{"material_id": "202", "error_code": ""}])
                second = client.available_pool_items("/internal/posts/material-pool/available", 1000,
                                                     schedule_identity=identity)
                self.assertEqual(first, second)
                dto = oauth_service.query_post_schedule_plan_request(identity)
                self.assertEqual(dto["run"]["material_pool_snapshot_max_id"], first[0]["id"])
                # The ordinary read-only pool endpoint keeps its existing behavior.
                self.assertEqual(len(client.available_pool_items("/internal/posts/material-pool/available", 1000)), 2)
            self.assertEqual(store.query_schedule_plan("material", "2026-07-27", "09:00")["queues"], [])


if __name__ == "__main__":
    unittest.main()
