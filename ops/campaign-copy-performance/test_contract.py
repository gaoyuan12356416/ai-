from pathlib import Path
import json
import datetime as dt
import time
import os
import tempfile
import threading
import socket
import types
import unittest
from urllib import error, request
from unittest import mock

import service


ROOT = Path(__file__).resolve().parent


class ContractTest(unittest.TestCase):
    def test_sql_gate_loss_closes_the_connection_and_fails_refresh(self):
        permit, peer = socket.socketpair()
        gate = types.SimpleNamespace(DEFAULT_POOL="test", DEFAULT_SOCKET_PATH="test",
                                     acquire_permit=lambda *args: (permit, {"status": "granted"}))
        conn = mock.MagicMock()
        stopped = threading.Event()
        conn._sock.shutdown.side_effect = lambda *args: stopped.set()
        try:
            with mock.patch.dict(service.sys.modules, {"sql_connection_gate": gate}), mock.patch.object(service, "mysql_connection", return_value=conn):
                with self.assertRaisesRegex(RuntimeError, "lease"):
                    with service.report_connection():
                        peer.close()
                        self.assertTrue(stopped.wait(2))
            conn.close.assert_called_once()
        finally:
            peer.close()
            permit.close()

    def test_sql_gate_denial_never_opens_database_connection(self):
        gate = types.SimpleNamespace(DEFAULT_POOL="test", DEFAULT_SOCKET_PATH="test",
                                     acquire_permit=lambda *args: (None, {"status": "error"}))
        with mock.patch.dict(service.sys.modules, {"sql_connection_gate": gate}), mock.patch.object(service, "mysql_connection") as connect:
            with self.assertRaisesRegex(RuntimeError, "gate unavailable"):
                with service.report_connection():
                    self.fail("denied permit yielded a database connection")
            connect.assert_not_called()

    def previous(self):
        return {"v": 2, "m": {"read_only_verified": True, "stat_end": "2026-10-08"},
                "cf": ["platform", "campaign_id"], "c": [[0, "100"]],
                "xf": ["platform", "level", "entity_id"], "x": [[3, 2, "200"]],
                "df": ["platform", "campaign_id", "dt", "spend"],
                "d": [[0, "100", "2026-07-17", 3], [0, "100", "2026-08-01", 5], [0, "100", "2026-10-08", 7]],
                "xdf": ["entity_key", "dt", "spend"], "xd": [["3:2:200", "2026-08-01", 4]]}

    def test_incremental_replaces_dates_without_double_counting(self):
        previous = self.previous()
        payload = {"meta": {}, "campaigns": [{"platform": 0, "campaign_id": "100"}],
                   "extra_entities": [{"entity_key": "3:2:200"}], "extra_daily": [],
                   "daily": [{"platform": 0, "campaign_id": "100", "dt": "2026-10-08", "spend": 11}]}
        merged = service.merge_history(payload, previous, ["2026-07-17", "2026-10-08"], "2026-07-18")
        self.assertEqual(sum(row["spend"] for row in merged["daily"]), 16)
        self.assertEqual(merged["meta"]["stat_start"], "2026-08-01")
        self.assertEqual(merged["extra_daily"][0]["spend"], 4)
        # An empty successful re-query replaces old rows too (including deleted corrections).
        self.assertNotIn("2026-07-17", [row["dt"] for row in merged["daily"]])

    def test_refresh_window_covers_outage_and_rotates_history(self):
        previous = self.previous()
        logs = [{"platform": 0, "level": 0, "new_id": "100", "created_at": "2026-07-17 10:00:00"}]
        dates, cursor = service.refresh_dates(previous, logs, dt.date(2026, 10, 10))
        self.assertEqual(len(dates), 8)
        self.assertIn(dt.date(2026, 10, 9), dates)
        self.assertIn(dt.date(2026, 10, 10), dates)
        self.assertEqual(cursor, "2026-07-18")
        previous["m"]["stat_end"] = "2026-09-20"
        dates, _ = service.refresh_dates(previous, logs, dt.date(2026, 10, 10))
        self.assertIn(dt.date(2026, 9, 21), dates)
        logs.append({"platform": 3, "level": 0, "new_id": "100", "created_at": "2026-08-10"})
        dates, _ = service.refresh_dates(previous, logs, dt.date(2026, 10, 10))
        self.assertIn(dt.date(2026, 8, 10), dates)

    def test_failed_refresh_keeps_last_good_cache(self):
        cache = service.ReportCache(None)
        body = json.dumps(self.previous()).encode()
        cache._install_body(body)
        with mock.patch.object(service, "query_raw", side_effect=service.pymysql.err.OperationalError(3024, "timeout")):
            self.assertFalse(cache.refresh())
        self.assertEqual(cache.snapshot()["body"], body)
        self.assertFalse(cache.snapshot()["refresh_in_progress"])

    def test_stale_disk_cache_remains_available_as_historical_seed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"report.json"
            path.write_text(json.dumps(self.previous()))
            past = time.time()-3*86400
            os.utime(path, (past, past))
            cache = service.ReportCache(path)
            self.assertTrue(cache.load_disk())
            self.assertGreater(cache.snapshot()["age"], 2*86400)

    def test_frontend_contract(self):
        source = (ROOT / "index.html").read_text(encoding="utf-8")
        self.assertIn("fetch('./api/data'", source)
        self.assertIn("cache:'default'", source)
        self.assertIn("RAW.v===2", source)
        self.assertIn('data-level="0">Campaign', source)
        self.assertIn('data-level="1">Ad Set', source)
        self.assertIn('data-level="2">Ad', source)
        self.assertIn('id="platformFilter"', source)
        self.assertIn("platform_label", source)
        self.assertIn("['iaa_revenue','IAA收入','money']", source)
        self.assertIn("['iaa_roas','IAA ROAS','ratio']", source)
        self.assertIn("agg.iaa_roas=agg.spend?agg.iaa_revenue/agg.spend:null", source)
        self.assertIn("new_id = adset_id", source)
        self.assertIn("new_id = ad_id", source)
        self.assertIn('id="loadingPanel"', source)
        self.assertIn("每15分钟后台自动刷新", source)
        self.assertNotIn("cache:'no-store'", source)
        self.assertIn(
            "const items=[['all','全部'],['today','当天'],['yesterday','昨天'],['3','近三天'],['7','近七天']]",
            source,
        )
        self.assertNotIn("['today','最后一天']", source)
        self.assertNotIn("['yesterday','前一天']", source)
        self.assertIn("else if(kind==='7'){start=addDays(currentDate,-6);end=currentDate;}", source)
        self.assertNotIn("campaign-copy-report-data", source)

    def test_level_query_contract(self):
        source = (ROOT / "service.py").read_text(encoding="utf-8")
        self.assertIn("l.level IN (0,1,2)", source)
        self.assertIn("l.platform IN (0,3)", source)
        self.assertIn("ads_tiktok_auto_created_data", source)
        self.assertIn("WHERE dt=%s AND platform=%s", source)
        self.assertIn('for level, id_field in ((1, "adset_id"), (2, "ad_id"))', source)
        self.assertIn('"copy_pipelines": copy_pipelines', source)
        self.assertIn('"extra_entities": extra_entities', source)

    def test_payload_contract(self):
        self.assertEqual(service.self_test(), 0)

    def test_platform_ids_isolate_same_campaign_id(self):
        raw = {
            "safety": {
                "is_read_only": 1,
                "server_time": "2026-07-28 12:00:00",
                "session_time_zone": "+08:00",
            },
            "copy_status_counts": [{"level": 0, "status": 1, "n": 2}],
            "insight_max_dt": "2026-07-28",
            "logs": [
                {
                    "id": 1,
                    "platform": 0,
                    "new_id": "1001",
                    "app_name": "Meta App",
                    "created_at": "2026-07-28 08:00:00",
                },
                {
                    "id": 2,
                    "platform": 3,
                    "new_id": "1001",
                    "app_name": "dramawaveminis",
                    "created_at": "2026-07-28 09:00:00",
                },
            ],
            "mapping": [
                {"platform": 0, "campaign_id": "1001", "ad_id": "2001"},
                {"platform": 3, "campaign_id": "1001", "ad_id": "3001"},
            ],
            "daily": [
                {"platform": 0, "campaign_id": "1001", "dt": "2026-07-28", "spend": 10},
                {"platform": 3, "campaign_id": "1001", "dt": "2026-07-28", "spend": 20},
            ],
            "extra_logs": [],
            "extra_mapping": [],
            "extra_daily": [],
        }
        payload = service.build_payload(raw)
        campaigns = {row["platform"]: row for row in payload["campaigns"]}
        daily = {row["platform"]: row for row in payload["daily"]}
        self.assertEqual(set(campaigns), {0, 3})
        self.assertEqual(campaigns[0]["platform_label"], "Meta")
        self.assertEqual(campaigns[3]["platform_label"], "TikTok")
        self.assertEqual(daily[0]["spend"], 10)
        self.assertEqual(daily[3]["spend"], 20)

    def test_read_only_port_guard(self):
        with mock.patch.dict(os.environ, {"ADMIN_MAPPING_MYSQL_PORT": "63353"}):
            with self.assertRaisesRegex(RuntimeError, "refusing non-read-only MySQL port"):
                service.mysql_connection()

    def test_etag_accepts_nginx_weak_variant(self):
        self.assertTrue(service.etag_matches('W/"abc"', '"abc"'))
        self.assertTrue(service.etag_matches('"other", W/"abc"', '"abc"'))
        self.assertFalse(service.etag_matches('"other"', '"abc"'))

    def test_persistent_cache_round_trip(self):
        body = json.dumps(
            {"v": 2, "m": {"read_only_verified": True}, "p": {}, "cf": [], "c": [], "df": [], "d": []},
            separators=(",", ":"),
        ).encode()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache" / "report.json"
            writer = service.ReportCache(path)
            writer._persist(body)
            reader = service.ReportCache(path)
            self.assertTrue(reader.load_disk())
            snapshot = reader.snapshot()
            self.assertEqual(snapshot["body"], body)
            self.assertLess(len(snapshot["gzip_body"]), len(body) + 30)

    def test_runtime_and_proxy_cache_contract(self):
        unit = (ROOT / "campaign-copy-performance.service").read_text(encoding="utf-8")
        nginx = (ROOT / "campaign-copy-performance.nginx.conf").read_text(encoding="utf-8")
        self.assertIn("CAMPAIGN_COPY_REPORT_CACHE_PATH=/mnt/data-disk/", unit)
        self.assertIn("ReadWritePaths=/mnt/data-disk/campaign-copy-performance/cache", unit)
        self.assertNotIn('Cache-Control "private, no-store"', nginx)

    def test_http_gzip_and_conditional_cache(self):
        body = b'{"v":2,"m":{"read_only_verified":true},"p":{},"cf":[],"c":[],"df":[],"d":[]}'
        cache = service.ReportCache(None)
        cache._install_body(body)
        server = service.ThreadingHTTPServer(("127.0.0.1", 0), service.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            with mock.patch.object(service, "CACHE", cache):
                response = request.urlopen(request.Request(base + "/api/data", headers={"Accept-Encoding": "gzip"}))
                self.assertEqual(response.headers["Content-Encoding"], "gzip")
                self.assertIn("max-age=60", response.headers["Cache-Control"])
                etag = response.headers["ETag"]
                self.assertTrue(etag.startswith('W/"'))
                self.assertEqual(service.gzip.decompress(response.read()), body)
                with self.assertRaises(error.HTTPError) as caught:
                    request.urlopen(request.Request(base + "/api/data", headers={"If-None-Match": etag}))
                self.assertEqual(caught.exception.code, 304)
                caught.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
