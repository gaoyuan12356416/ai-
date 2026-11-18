import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import x_tag_auto_runner as tag


class DrainTests(unittest.TestCase):
    def test_drain_defers_new_claims_and_resumes_when_released(self):
        client = object.__new__(tag.DrainAwareClient)
        with patch.object(tag.Path, "exists", return_value=True), patch.object(
            tag.runner.SidecarClient, "post", return_value={"claimed": True}
        ) as post:
            self.assertFalse(client.post("/internal/x-auto-post/execute-next", {}, 30)["claimed"])
            post.assert_not_called()
        with patch.object(tag.Path, "exists", return_value=False), patch.object(
            tag.runner.SidecarClient, "post", return_value={"claimed": True}
        ) as post:
            self.assertTrue(client.post("/internal/x-auto-post/execute-next", {}, 30)["claimed"])
            post.assert_called_once()

    def test_drain_keeps_existing_receipt_requests_available(self):
        client = object.__new__(tag.DrainAwareClient)
        with patch.object(tag.Path, "exists", return_value=True), patch.object(
            tag.runner.SidecarClient, "post", return_value={"ok": True}
        ) as post:
            client.post("/internal/x-auto-post/tick", {}, 30)
            post.assert_called_once()


if __name__ == "__main__":
    unittest.main()
