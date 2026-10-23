import csv
import io
import json
import unittest
from copy import deepcopy
from unittest.mock import patch
from urllib.parse import urlsplit

from features.social_analytics import routes
from features.social_analytics.backend_scope import SCOPE
from test_social_backend_scope import DAY, link, metric


class Handler:
    command = "GET"

    def __init__(self, actor):
        self.actor = actor
        self.wfile = io.BytesIO()
        self.headers = {}
        self.status = None

    def _social_analytics_actor(self):
        return self.actor

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.headers[key] = value

    def end_headers(self):
        pass


class WebBackendScopeTests(unittest.TestCase):
    def setUp(self):
        self.catalog = [link(cid="6", owner="alice", date=DAY),
                        link(cid="failed", published=False),
                        link(cid="bob", owner="bob"), link(cid="other", owner="eve")]
        self.catalog[-1]["tenant"] = "other-tenant"
        self.source = [metric(cid="6", cents=123), metric(cid="6", cents=95097, adset_id="other-page"),
                       metric(cid="failed", cents=380670), metric(cid="bob", cents=200),
                       metric(cid="other", cents=98765)]
        self.actor = dict(role="admin", tenant_key="tenant", user_id="alice", _system_report=True)
        self.query = "?start=" + DAY + "&end=" + DAY + "&platform=facebook"

    def invoke(self, route="report", actor=None, extra=""):
        handler = Handler(actor or self.actor)
        output = []
        def source(app, start, end, site, catalog=None):
            rows = self.source if site == "2049" else [metric(cid="outside", cents=0, site_id="2310")]
            return deepcopy(rows), "snapshot"
        with patch.object(routes, "read_catalogs", return_value=(deepcopy(self.catalog), {})), \
                patch.object(routes, "scoped_facebook_source", side_effect=source), \
                patch.object(routes, "enrich"):
            routes.dispatch(handler, urlsplit("/api/social-analytics/" + route + self.query + extra),
                            {"json_response": lambda h, status, value, **kw: output.append((status, value))})
        return handler, output

    def test_web_excludes_collisions_failed_posts_and_other_tenants(self):
        _, output = self.invoke()
        status, report = output[0]
        self.assertEqual(status, 200)
        self.assertEqual(report["totals"]["revenue_cents"], 323)
        self.assertEqual(report["totals"]["published_count"], 1)
        self.assertEqual(report["meta"]["scope"], "tenant")
        self.assertEqual(report["meta"]["content_scope"], SCOPE)
        self.assertTrue(report["quality"]["complete"])
        self.assertNotIn("scope_audit", report)
        payload = json.dumps(report)
        for forbidden in ("other-tenant", "98765", "380670", "95097"):
            self.assertNotIn(forbidden, payload)

    def test_regular_user_and_owner_filter_cannot_expand_visibility(self):
        actor = dict(self.actor, role="user")
        _, output = self.invoke(actor=actor)
        self.assertEqual(output[0][1]["totals"]["revenue_cents"], 123)
        _, output = self.invoke(actor=actor, extra="&owner=bob")
        self.assertEqual(output[0][1]["totals"]["revenue_cents"], 0)

    def test_csv_uses_same_restricted_amounts_and_counts(self):
        handler, output = self.invoke("export.csv")
        self.assertEqual(output, [])
        self.assertEqual(handler.status, 200)
        rows = list(csv.DictReader(io.StringIO(handler.wfile.getvalue().decode("utf-8-sig"))))
        self.assertEqual(sum(round(float(row["收入（USD）"]) * 100) for row in rows), 323)
        self.assertEqual(sum(int(row["成功发布数"]) for row in rows), 1)
        self.assertNotIn(b"scope_audit", handler.wfile.getvalue())

    def test_request_cannot_disable_backend_scope(self):
        _, output = self.invoke(extra="&ai_backend_only=false")
        self.assertEqual(output[0][0], 400)

    def test_source_failure_remains_unknown(self):
        with patch.object(routes, "read_catalogs", return_value=(self.catalog, {})), \
                patch.object(routes, "scoped_facebook_source", side_effect=routes.ReportError("unavailable", 503)):
            report = routes.build_report({}, self.actor, {"start": [DAY], "end": [DAY], "platform": ["facebook"]},
                                         ai_backend_only=True)
        self.assertIsNone(report["totals"]["revenue_cents"])
        self.assertEqual(report["totals"]["published_count"], 1)

    def test_provenance_cache_is_separate_and_keeps_site_dimension(self):
        routes._cache.clear()
        try:
            with patch.object(routes, "query_source_rows", return_value=[metric()]) as query:
                routes.source({}, DAY, DAY, "facebook", "2049")
                routes.scoped_facebook_source({}, DAY, DAY, "2049")
                routes.scoped_facebook_source({}, DAY, DAY, "2049")
                query.return_value = [metric(site_id="2310")]
                routes.scoped_facebook_source({}, DAY, DAY, "2310")
            self.assertEqual(query.call_count, 3)
            self.assertIn("BINARY adset_id,BINARY ad_id", query.call_args_list[1].args[1])
        finally:
            routes._cache.clear()

    def test_web_source_reduction_keeps_coverage_and_new_candidates_invalidate_cache(self):
        routes._cache.clear()
        coverage = metric(cid="", cents=0, adset_id="", ad_id="", _coverage_only=1)
        try:
            with patch.object(routes, "query_source_rows", return_value=[coverage]) as query:
                rows, _ = routes.scoped_facebook_source({}, DAY, DAY, "2049", self.catalog)
                routes.scoped_facebook_source({}, DAY, DAY, "2049", self.catalog)
                routes.scoped_facebook_source({}, DAY, DAY, "2049", self.catalog + [link(cid="new")])
            self.assertEqual(query.call_count, 2)
            sql = query.call_args_list[0].args[1]
            self.assertIn("UNION ALL", sql)
            self.assertIn("(BINARY campaign_id,BINARY adset_id,BINARY ad_id) IN", sql)
            self.assertIn("0x" + "other".encode().hex(), sql)  # all tenants before ACL
            self.assertNotIn("0x" + "failed".encode().hex(), sql)
            self.assertEqual(rows[0]["date"], DAY)
        finally:
            routes._cache.clear()


if __name__ == "__main__":
    unittest.main()
