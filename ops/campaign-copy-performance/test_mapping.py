import copy
import datetime as dt
import unittest
import json
from unittest import mock

import campaign_mapping as mapping
import service


NOW = dt.datetime(2026, 10, 13, 16)
TT = mapping.SOURCE_SPECS[2][1]
FB = mapping.SOURCE_SPECS[0][1]
FALLBACK = mapping.SOURCE_SPECS[1][1]


def source_row(cid="100", ad="200", **kw):
    return dict(campaign_id=cid, ad_id=ad, campaign_name="Example", product="Source product",
                country="US", language="en", **kw)


class SourceCursor:
    def __init__(self, tables):
        self.tables = tables
        self.queries = []
        self.rows = []

    def execute(self, sql, args):
        table = sql.split(" FROM ")[1].split()[0]
        covering = sql.startswith("SELECT campaign_id, ad_id FROM")
        self.queries.append((table, covering, tuple(args), sql))
        self.rows = []
        for original in self.tables.get(table, []):
            if original["campaign_id"] not in args:
                continue
            row = {k: original[k] for k in ("campaign_id", "ad_id")} if covering else dict(original)
            if table == TT and not covering:
                row["product"] = ""
            self.rows.append(row)

    def fetchall(self):
        return self.rows


class MappingTest(unittest.TestCase):
    def query(self, tables, logs=None, previous=None, now=NOW):
        cur = SourceCursor(tables)
        logs = logs if logs is not None else [{"platform": 3, "new_id": "100"}]
        rows, metadata, bucket, stats = mapping.query_campaign_mapping(cur, logs, previous, now)
        return rows, metadata, bucket, stats, cur

    def prior(self, tables, logs=None):
        rows, metadata, bucket, _, _ = self.query(tables, logs)
        fields = list(service.CAMPAIGN_FIELDS)
        campaigns = []
        for (platform, cid), item in metadata.items():
            member = [r for r in rows if r["platform"] == platform and r["campaign_id"] == cid]
            row = dict(item, platform=platform, campaign_id=cid, product="App label",
                       metadata_product=item["product"], mapped=bool(member),
                       ad_count=len({r["ad_id"] for r in member if r["ad_id"]}))
            campaigns.append([row.get(field) for field in fields])
        occupied = {mapping.metadata_bucket(p, cid) for p, cid in metadata}
        bucket = next(i for i in range(mapping.METADATA_BUCKETS) if i not in occupied)
        return {"cf": fields, "c": campaigns,
                "m": {"generated_at": str(NOW), "mapping_metadata_next_bucket": bucket}}

    def test_unchanged_reuses_details_but_reads_current_exact_membership(self):
        tables = {TT: [source_row(), source_row(ad="201"), source_row(ad="201")]}
        prior = self.prior(tables)
        original = copy.deepcopy(prior)
        rows, metadata, _, stats, cur = self.query(tables, previous=prior)
        self.assertEqual([r["ad_id"] for r in rows], ["200", "201", "201"])
        self.assertEqual(stats["detail_queries"], 0)
        self.assertEqual(stats["reused_campaigns"], 1)
        self.assertEqual(metadata[(3, "100")]["product"], "")
        self.assertIn("FORCE INDEX (campaign_id_2)", cur.queries[0][3])
        self.assertEqual(prior, original)

    def test_same_count_child_replacement_and_new_campaign_refresh_immediately(self):
        prior = self.prior({TT: [source_row()]})
        rows, metadata, _, stats, _ = self.query(
            {TT: [source_row(ad="999"), source_row(cid="101", ad="202")]},
            [{"platform": 3, "new_id": "100"}, {"platform": 3, "new_id": "101"}], prior)
        self.assertEqual({r["ad_id"] for r in rows}, {"999", "202"})
        self.assertEqual(stats["detail_campaigns"], 2)

    def test_missing_members_clear_old_labels_and_late_child_is_found(self):
        prior = self.prior({TT: [source_row()]})
        rows, metadata, _, stats, _ = self.query({}, previous=prior)
        self.assertEqual(rows, [])
        self.assertEqual(metadata[(3, "100")]["campaign_name"], "")
        self.assertEqual(stats["detail_queries"], 0)
        empty_prior = self.prior({})
        rows, _, _, stats, _ = self.query({TT: [source_row()]}, previous=empty_prior)
        self.assertEqual(len(rows), 1)
        self.assertEqual(stats["detail_campaigns"], 1)

    def test_source_fallback_is_per_campaign_and_platforms_are_isolated(self):
        tables = {FB: [source_row(ad="1")], FALLBACK: [source_row(ad="2"), source_row(cid="101", ad="3")],
                  TT: [source_row(ad="4")]}
        logs = [{"platform": p, "new_id": cid} for p, cid in [(0, "100"), (0, "101"), (3, "100")]]
        rows, metadata, _, _, _ = self.query(tables, logs)
        self.assertEqual({(r["platform"], r["ad_id"]) for r in rows}, {(0, "1"), (0, "3"), (3, "4")})
        self.assertEqual(metadata[(0, "100")]["product"], "Source product")
        self.assertEqual(metadata[(3, "100")]["product"], "")

    def test_metadata_rotation_and_maximum_age_refresh_labels(self):
        original = {TT: [source_row()]}
        prior = self.prior(original)
        changed = copy.deepcopy(original)
        changed[TT][0]["campaign_name"] = "Renamed"
        result = self.query(changed, previous=prior)
        self.assertEqual(result[1][(3, "100")]["campaign_name"], "Example")
        prior["m"]["mapping_metadata_next_bucket"] = mapping.metadata_bucket(3, "100")
        self.assertEqual(self.query(changed, previous=prior)[1][(3, "100")]["campaign_name"], "Renamed")
        prior = self.prior(original)
        self.assertEqual(self.query(changed, previous=prior, now=NOW+dt.timedelta(days=1))[3]["detail_campaigns"], 1)

    def test_legacy_tiktok_cache_migrates_without_full_detail_rebuild(self):
        tables = {TT: [source_row()]}
        prior = self.prior(tables)
        for field in ["metadata_signature", "metadata_checked_at", "metadata_product"]:
            index = prior["cf"].index(field)
            prior["cf"].pop(index)
            for row in prior["c"]:
                row.pop(index)
        self.assertEqual(self.query(tables, previous=prior)[3]["detail_queries"], 0)
        prior["c"][0][prior["cf"].index("platform")] = 0
        self.assertEqual(self.query({FB: [source_row()]}, [{"platform": 0, "new_id": "100"}], prior)[3]["detail_queries"], 1)

    def test_meta_migration_only_fetches_raw_product_when_log_app_name_is_missing(self):
        logs = [{"platform": 0, "new_id": "100", "app_name": "App label"}]
        tables = {FB: [source_row()]}
        prior = self.prior(tables, logs)
        prior["c"][0][prior["cf"].index("metadata_product")] = None
        _, metadata, _, stats, _ = self.query(tables, logs, prior)
        self.assertEqual(stats["detail_queries"], 0)
        self.assertIsNone(metadata[(0, "100")]["product"])
        logs[0]["app_name"] = ""
        _, metadata, _, stats, _ = self.query(tables, logs, prior)
        self.assertEqual(stats["detail_queries"], 1)
        self.assertEqual(metadata[(0, "100")]["product"], "Source product")

    def test_inconsistent_source_fails_instead_of_installing_partial_metadata(self):
        cur = SourceCursor({TT: [source_row()]})
        execute = cur.execute
        def drift(sql, args):
            execute(sql, args)
            if "campaign_name" in sql:
                cur.rows[0]["ad_id"] = "changed"
        cur.execute = drift
        with self.assertRaisesRegex(RuntimeError, "membership changed"):
            mapping.query_campaign_mapping(cur, [{"platform": 3, "new_id": "100"}], None, NOW)

    def test_details_are_bounded_and_financial_payload_matches_full_mapping(self):
        rows = [source_row(ad=str(200+i)) for i in range(3)]
        logs = [{"platform": 3, "new_id": "100", "id": 1, "created_at": str(NOW), "app_name": "App"}]
        current, metadata, _, _, cur = self.query({TT: rows}, logs)
        raw = {"safety": {"is_read_only": 1, "server_time": str(NOW)}, "logs": logs,
               "copy_status_counts": [], "mapping": [dict(r, platform=3, product="") for r in rows],
               "daily": [{"platform": 3, "campaign_id": "100", "dt": str(NOW.date()),
                          "spend": 10, "af_revenue0": 4, "iaa_revenue": 2}]}
        original = service.build_payload(raw)
        optimized = service.build_payload(dict(raw, mapping=current, campaign_metadata=metadata))
        for row in optimized["campaigns"]:
            for field in ("metadata_signature", "metadata_checked_at", "metadata_product"):
                row[field] = None
        self.assertEqual(optimized, original)
        with mock.patch.object(mapping, "DETAIL_BATCH", 1):
            many = [source_row(cid=str(i), ad=str(i+1)) for i in range(4)]
            _, _, _, stats, cur = self.query({TT: many}, [{"platform": 3, "new_id": r["campaign_id"]} for r in many])
            self.assertEqual(stats["detail_queries"], 4)
            self.assertTrue(all(len(args) <= 1 for _, cover, args, _ in cur.queries if not cover))


class RefreshSchedulingTest(unittest.TestCase):
    def test_private_metadata_is_retained_for_refresh_but_never_sent_to_browser(self):
        seed = {"v": 2, "m": {"read_only_verified": True, "mapping_metadata_next_bucket": 5},
                "cf": ["campaign_id", "metadata_signature", "metadata_checked_at", "metadata_product"],
                "c": [["100", "private-digest", str(NOW), "raw-product"]],
                "df": ["spend"], "d": [[12.5]]}
        body = json.dumps(seed).encode()
        cache = service.ReportCache(None)
        cache._install_body(body)
        snapshot = cache.snapshot()
        self.assertEqual(snapshot["seed_body"], body)
        public = json.loads(snapshot["body"])
        self.assertEqual(public["cf"], ["campaign_id"])
        self.assertEqual(public["c"], [["100"]])
        self.assertEqual(public["d"], [[12.5]])
        self.assertNotIn("mapping_metadata_next_bucket", public["m"])
        self.assertEqual(service.gzip.decompress(snapshot["gzip_body"]), snapshot["body"])

    def test_page_reads_never_start_sql_even_for_stale_or_failed_cache(self):
        cache = service.ReportCache(None)
        cache._install_body(b"{}", age_seconds=9000)
        with mock.patch.object(cache, "refresh") as refresh, mock.patch.object(service.threading, "Thread") as thread:
            for _ in range(100):
                self.assertEqual(cache.get()[3], "stale")
            refresh.assert_not_called()
            thread.assert_not_called()

    def test_failure_backoff_is_bounded_and_success_restores_cadence(self):
        self.assertEqual([service.refresh_delay(False, 10, i) for i in range(1, 7)],
                         [120, 240, 480, 900, 900, 900])
        self.assertEqual(service.refresh_delay(True, 400, 0), 500)

    def test_one_log_read_produces_counts_and_success_population(self):
        rows = [dict(id=i, platform=3, level=0, status=status, new_id=str(i), created_at=str(NOW))
                for i, status in [(1, 0), (2, 1), (3, 2), (4, 1)]]
        rows[-1]["new_id"] = "invalid"
        cursor = mock.MagicMock()
        cursor.execute.return_value = 1
        cursor.fetchone.side_effect = [{"is_read_only": 1, "server_time": NOW}, {"dt": NOW.date()}]
        cursor.fetchall.return_value = rows
        conn = mock.MagicMock()
        conn.cursor.return_value.__enter__.return_value = cursor
        with mock.patch.object(service, "query_campaign_mapping", return_value=([], {}, 0, {})) as mapped:
            raw = service.query_raw_connection(conn)
        self.assertEqual(raw["copy_status_counts"], [{"level": 0, "status": 0, "n": 1},
                         {"level": 0, "status": 1, "n": 2}, {"level": 0, "status": 2, "n": 1}])
        self.assertEqual([r["new_id"] for r in mapped.call_args.args[1]], ["2"])
        log_queries = [call.args[0] for call in cursor.execute.call_args_list if "ads_campaign_rule_logs" in call.args[0]]
        self.assertEqual(len(log_queries), 1)


if __name__ == "__main__":
    unittest.main()
