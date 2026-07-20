from pathlib import Path
import os
import unittest
from unittest import mock

import service


ROOT = Path(__file__).resolve().parent


class ContractTest(unittest.TestCase):
    def test_frontend_contract(self):
        source = (ROOT / "index.html").read_text(encoding="utf-8")
        self.assertIn("fetch('./api/data'", source)
        self.assertIn(
            "const items=[['all','全部'],['today','当天'],['yesterday','昨天'],['3','近三天'],['7','近七天']]",
            source,
        )
        self.assertNotIn("['today','最后一天']", source)
        self.assertNotIn("['yesterday','前一天']", source)
        self.assertIn("else if(kind==='7'){start=addDays(currentDate,-6);end=currentDate;}", source)
        self.assertNotIn("campaign-copy-report-data", source)

    def test_payload_contract(self):
        self.assertEqual(service.self_test(), 0)

    def test_read_only_port_guard(self):
        with mock.patch.dict(os.environ, {"ADMIN_MAPPING_MYSQL_PORT": "63353"}):
            with self.assertRaisesRegex(RuntimeError, "refusing non-read-only MySQL port"):
                service.mysql_connection()


if __name__ == "__main__":
    unittest.main()
