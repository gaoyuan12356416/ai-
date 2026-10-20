import contextlib
import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("cover_history", Path(__file__).with_name("history.py"))
history = importlib.util.module_from_spec(spec)
spec.loader.exec_module(history)
from features.cover_material_source import SPECS
from PIL import Image


@contextlib.contextmanager
def writing(path):
    conn = sqlite3.connect(path)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


class FakeCatalog:
    def __init__(self):
        self.intakes = []
        self.finals = []
        self.queries = []

    def query(self, sql):
        self.queries.append(sql)
        return self.intakes if "data_source=102" in sql else self.finals


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "source.sqlite3"
        self.sync = self.root / "sync.sqlite3"
        self.cutoff = "2026-10-20T10:00:00+08:00"
        self.start = "2026-10-13 02:00:00"
        self.accepted = "2026-10-19 03:00:00"
        with writing(self.source) as conn:
            conn.executescript("""
              CREATE TABLE drama_admin_audit_log(id INTEGER,action TEXT,target_id TEXT,detail_json TEXT,created_at TEXT);
              CREATE TABLE drama_screenshot_job(job_id TEXT,app_id TEXT,content_id TEXT,language TEXT,country TEXT,
                creator_user_id TEXT,creator_name TEXT,drama_name TEXT,finished_at TEXT,assets_json TEXT,
                square_1x1_url TEXT,landscape_1_91x1_url TEXT,portrait_4x5_url TEXT);
              CREATE TABLE drama_admin_user(user_id TEXT,email TEXT);
            """)
        with writing(self.sync) as conn:
            conn.executescript("""
              CREATE TABLE events(id INTEGER,key TEXT,state TEXT,detail TEXT,created_at TEXT);
              CREATE TABLE cover_sync(key TEXT,source TEXT,request TEXT,material_id TEXT,intake_id INTEGER,final_id INTEGER,state TEXT);
            """)
        self.catalog = FakeCatalog()

    def image(self, filename, size, at="2026-10-01T00:00:00+00:00"):
        path = self.root / filename
        Image.new("RGB", size).save(path)
        stamp = history.timestamp(at).timestamp()
        os.utime(path, (stamp, stamp))
        return path

    def screenshot(self, at=None, code=0):
        assets = {}
        urls = []
        job_id = "job-12345"
        for index, (size, category) in enumerate(SPECS):
            url = "https://example.invalid/" + size + ".jpg"
            path = self.image(size + ".jpg", [(120, 120), (191, 100), (80, 100)][index])
            assets[size] = dict(url=url, public_output_path=str(path))
            urls.append(url)
            name = "剧目-中文标题-" + category + "-job-1234"
            mid = str(int(hashlib.sha1((job_id + ":" + size).encode()).hexdigest()[:15], 16))
            self.catalog.intakes.append(dict(id=index + 1, data_source_id=mid, name=name, url=url,
                                             category=category, remark=str(index + 101), created_at="2026-09-01"))
            self.catalog.finals.append(dict(id=index + 101, product="Drama-AI素材", type=1, data_source=6, data_source_id="剧目",
                                            name=name, category=category, is_delete=0, created_at="2026-10-19"))
        with writing(self.source) as conn:
            conn.execute("INSERT INTO drama_screenshot_job VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (job_id, "1479", "剧目", "en", "us", "api:screenshot-api-direct",
                          "", "中文标题", "2026-10-01 00:00:00", json.dumps(assets), *urls))
            conn.execute("INSERT INTO drama_admin_audit_log VALUES (1,?,?,?,?)",
                         ("callback_screenshot_ai_source", job_id,
                          json.dumps(dict(sent_count=3, status_code=200, response=json.dumps(dict(code=code)))),
                          at or self.accepted))
        return assets

    def youtube(self, accepted=True):
        key = "youtube-cover:task:v1:asset"
        path = self.image("youtube.jpg", (160, 90))
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        source = dict(path=str(path), sha256=sha, category="16:9", content_id="drama", language="en",
                      owner_email="example@example.invalid", owner_name="测试", asset_created_at="2026-07-01T00:00:00Z",
                      version=1, asset_id="asset", cover_source="manual")
        request = dict(payload=dict(material_id=1, content_id="drama", category="16:9",
                                    name="legacy-youtube", url="https://example.invalid/youtube.jpg"))
        with writing(self.sync) as conn:
            conn.execute("INSERT INTO cover_sync VALUES (?,?,?,?,?,?,?)",
                         (key, json.dumps(source), json.dumps(request), "1", 99, 199, "confirmed"))
            conn.execute("INSERT INTO events VALUES (1,?,'accepted',?,?)",
                         (key, json.dumps(dict(accepted=True, http_status=200, code=0)) if accepted else "{}",
                          "2026-10-19T03:00:00+00:00"))
        self.catalog.intakes.append(dict(id=99, data_source_id="1", name="legacy-youtube",
                                         url=request["payload"]["url"], category="16:9", remark="199", created_at="2026-07-01"))
        self.catalog.finals.append(dict(id=199, product="Drama-AI素材", type=1, data_source=6, data_source_id="drama", name="legacy-youtube",
                                        category="16:9", is_delete=0, created_at="2026-10-19"))
        return key

    def discover(self, **kwargs):
        return history.discover(self.cutoff, self.source, self.sync, self.catalog, **kwargs)

    def test_old_images_recent_acceptance_and_original_product(self):
        self.screenshot()
        sources, issues = self.discover()
        self.assertEqual(len(sources), 3)
        self.assertFalse(issues)
        self.assertTrue(all(x["system_actor"] and x["app_id"] == 1479 for x in sources))
        self.assertEqual(sources[0]["legacy_final_ids"], [102])

    def test_window_start_included_cutoff_excluded(self):
        self.screenshot(at=self.start)
        self.assertEqual(len(self.discover()[0]), 3)
        with writing(self.source) as conn:
            conn.execute("UPDATE drama_admin_audit_log SET created_at='2026-10-20 02:00:00'")
        self.assertEqual(len(self.discover()[0]), 0)
        self.cutoff = "2026-10-20T02:00:00.500000+00:00"
        self.assertEqual(len(self.discover()[0]), 3)

    def test_rejected_http_200_not_success(self):
        self.screenshot(code=-1)
        sources, issues = self.discover()
        self.assertFalse(sources)
        self.assertEqual(issues[0]["reason"], "legacy_callback_not_accepted")
        self.assertTrue(issues[0]["excluded"])

    def test_changed_image_not_substituted(self):
        assets = self.screenshot()
        path = assets["square_1x1"]["public_output_path"]
        stamp = history.timestamp("2026-10-19T04:00:00+00:00").timestamp()
        os.utime(path, (stamp, stamp))
        sources, issues = self.discover()
        self.assertEqual(len(sources), 2)
        self.assertEqual(issues[0]["reason"], "image_modified_after_acceptance")

    def test_pending_final_retains_candidate_for_later_reconciliation(self):
        self.screenshot()
        self.catalog.intakes[0]["remark"] = ""
        sources, issues = self.discover()
        self.assertEqual(len(sources), 2)
        self.assertEqual(issues[0]["reason"], "legacy_final_pending")
        self.assertIn("candidate", issues[0])

    def test_youtube_uses_http_receipt_not_generation_date(self):
        self.youtube()
        sources, issues = self.discover()
        self.assertEqual(len(sources), 1)
        self.assertFalse(issues)
        self.assertEqual(sources[0]["legacy_accepted_at"], "2026-10-19T03:00:00+00:00")
        self.assertEqual(sources[0]["cover_source"], "manual")

    def test_empty_accepted_event_requires_durable_adoption_evidence(self):
        key = self.youtube(accepted=False)
        sources, issues = self.discover()
        self.assertFalse(sources)
        self.assertEqual(issues[0]["reason"], "accepted_time_evidence_missing")
        sources, issues = self.discover(adoption_receipts={key: dict(
            accepted_at="2026-10-19T03:00:00+00:00", intake_id=99, final_id=199,
            evidence_ref="trial-result.json", evidence_sha256="a" * 64)})
        self.assertEqual(len(sources), 1)
        self.assertFalse(issues)

    def test_cutoff_timezone_required(self):
        with self.assertRaisesRegex(ValueError, "explicit_timezone"):
            history.discover("2026-10-20 10:00:00", self.source, self.sync, self.catalog)


if __name__ == "__main__":
    unittest.main()
