import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from retire_verified import delivery_matches, verified_sources


class RetirementTests(unittest.TestCase):
    def test_requires_an_approved_snapshot_and_excludes_original_receipts(self):
        daily = "dramawave-review/topic190ddc-20261103/day-2026-11-01.json"
        audit = "projects/codex_test/output/fb_2446289_daily_replace_delete_20261103_100000.jsonl"
        with tempfile.TemporaryDirectory() as temp:
            job = Path(temp)
            (job / "receipts").mkdir()
            records = [
                dict(path=daily, status="verified", dataset="dw_daily"),
                dict(path=audit, status="verified", dataset="fb_receipt_index"),
                dict(path=daily.replace("01.json", "02.json"), status="verified", dataset="dw_daily"),
                dict(path=daily.replace("01.json", "03.json"), status="retained", dataset="dw_daily"),
            ]
            (job / "receipts/history-final.jsonl").write_text(
                "".join(json.dumps(v) + "\n" for v in records)
            )
            sources = verified_sources(job, {daily: {}, audit: {}, records[3]["path"]: {}})
            self.assertEqual(list(sources), [daily])

    def test_changed_linked_data_protects_the_whole_delivery(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            for name in ("index.html", "data.csv"):
                (root / name).write_bytes(name.encode())
            digest = lambda name: hashlib.sha256(name.encode()).hexdigest()
            proof = dict(
                delivery="index.html",
                delivery_sha256=digest("index.html"),
                delivery_files={"data.csv": digest("data.csv")},
            )
            self.assertTrue(delivery_matches(proof, root, {}))
            (root / "data.csv").write_bytes(b"new content")
            self.assertFalse(delivery_matches(proof, root, {}))


if __name__ == "__main__":
    unittest.main()
