import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from features.pipeline_dispatch import native_media
from features.pipeline_dispatch.media_decision import build_context
from scripts import test_pipeline_x_native


class XMediaFallbackTests(unittest.TestCase):
    def test_x_repairable_original_returns_to_native_preparation(self):
        f = test_pipeline_x_native.NativeTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        for reason in ("invalid_media_codec", "invalid_media_dimensions"):
            with self.subTest(reason=reason):
                row = f.bridge.store.get_queue(f.queue(random=True)["id"])
                job = f.job(row, "prepare", generation=0)
                due = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
                job.update(publish_at=due, observed_publish_at=due)
                payload = {"job": job, "decision_id": "repair-test", "decision": {"kind": "deadline_original", "source_identity": build_context(row["material_url"], mode="")["source_identity"]}}
                with patch.object(native_media, "_artifact", side_effect=native_media.InvalidMedia(reason)):
                    result = f.bridge.dispatch("media-decision", payload)
                self.assertEqual(result, {"already_started": True, "reason": "native_media_repair_required"})
                self.assertEqual(f.bridge.store.get_queue(row["id"])["status"], "queued")
                with f.bridge.connect() as conn:
                    self.assertEqual(native_media.existing(conn, job["source_key"]), {})
                    self.assertEqual(conn.execute("SELECT count(*) FROM x_post_publish_log WHERE queue_id=?", (row["id"],)).fetchone()[0], 0)

