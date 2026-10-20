import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

spec = importlib.util.spec_from_file_location("recover", Path(__file__).with_name("recover.py"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
from features.cover_material_source import Client, Ledger, identity, make_payload, submit


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ledger = Ledger(self.temp.name)
        (self.ledger.root / "HOLD").touch()
        self.source = dict(source_kind="screenshot", source_key="screenshot:job:square_1x1",
                           app_id=3360, content_id="65044414", sha256="a" * 64,
                           url="https://ai.yingliangads.com/image.jpg", name="封面",
                           category="1:1", language="id", country="ID", system_actor=True,
                           owner_email="")
        self.key = identity(self.source)
        self.ledger.prepare(self.source, make_payload(self.source, 248))
        self.ledger.begin_attempt(self.key)
        self.ledger.change(self.key, "unknown", {"http_status": 200, "code": 403})
        self.catalog = Mock()
        self.catalog.find.return_value = []

    def tearDown(self):
        self.ledger.close()
        self.temp.cleanup()

    def test_classification_keeps_same_signature_and_no_post(self):
        row = module.recover(self.ledger, self.catalog, self.key, "classify", "user-confirmation")
        self.assertEqual((row["state"], row["attempts"], row["key"]), ("rejected", 1, self.key))

    def test_hold_reference_and_explicit_rejection_required(self):
        with self.assertRaisesRegex(ValueError, "audit_reference"):
            module.recover(self.ledger, self.catalog, self.key, "classify", "")
        with self.assertRaisesRegex(ValueError, "only_explicit_rejection"):
            module.recover(self.ledger, self.catalog, self.key, "rearm", "ticket-fixed")
        (self.ledger.root / "HOLD").unlink()
        with self.assertRaisesRegex(ValueError, "persistent_hold"):
            module.recover(self.ledger, self.catalog, self.key, "classify", "user-confirmation")

    def test_timeout_without_explicit_rejection_cannot_rearm(self):
        self.ledger.change(self.key, "unknown", {"http_status": 200, "code": None})
        with self.assertRaisesRegex(ValueError, "explicit_product_rejection"):
            module.recover(self.ledger, self.catalog, self.key, "classify", "user-confirmation")

    def test_confirmed_remote_outcome_prevents_rearm(self):
        request = json.loads(self.ledger.get(self.key)["request_json"])
        self.catalog.find.return_value = [dict(request, id=123, product="moboreels", type=1,
                                              data_source_id=request["content_id"], is_delete=0)]
        row = module.recover(self.ledger, self.catalog, self.key, "rearm", "ticket-fixed")
        self.assertEqual((row["state"], row["attempts"]), ("confirmed", 1))

    def test_later_timeout_cannot_reuse_previous_attempt_rejection(self):
        module.recover(self.ledger, self.catalog, self.key, "classify", "user-confirmation")
        module.recover(self.ledger, self.catalog, self.key, "rearm", "upstream-fixed")
        self.ledger.begin_attempt(self.key)
        self.ledger.change(self.key, "unknown", {"exception_type": "TimeoutError"})
        with self.assertRaisesRegex(ValueError, "explicit_product_rejection"):
            module.recover(self.ledger, self.catalog, self.key, "classify", "user-confirmation")

    def test_same_request_retry_keeps_cumulative_attempt_and_can_only_rearm_once(self):
        original = self.ledger.get(self.key)["request_json"]
        module.recover(self.ledger, self.catalog, self.key, "classify", "user-confirmation")
        row = module.recover(self.ledger, self.catalog, self.key, "rearm", "upstream-fix-123")
        self.assertEqual((row["state"], row["attempts"]), ("prepared", 1))
        with self.assertRaisesRegex(ValueError, "only_explicit_rejection"):
            module.recover(self.ledger, self.catalog, self.key, "rearm", "upstream-fix-123")
        (self.ledger.root / "HOLD").unlink()
        client = Mock()
        client.post.return_value = ("accepted", {"api_id": 987})
        row = submit(self.source, self.ledger, self.catalog, client, verify_image=Mock())
        self.assertEqual((row["state"], row["attempts"], row["api_id"]), ("accepted", 2, 987))
        self.assertEqual(row["request_json"], original)
        event = json.loads(self.ledger.db.execute(
            "SELECT detail_json FROM events WHERE state='outbound_started' ORDER BY id DESC LIMIT 1").fetchone()[0])
        self.assertEqual(event["attempt"], 2)
        self.assertEqual(len(event["request_sha256"]), 64)

    def test_http200_business403_is_rejected_by_confirmed_contract(self):
        response = Mock(status_code=200)
        response.json.return_value = {"code": 403, "data": {}}
        session = Mock()
        session.post.return_value = response
        state, receipt = Client({"AD_MATERIAL_SOURCE_API_TOKEN": "test"}, session=session).post({})
        self.assertEqual((state, receipt["reason"]), ("rejected", "business_product_restricted"))


if __name__ == "__main__":
    unittest.main()
