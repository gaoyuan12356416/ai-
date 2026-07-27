from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ResolverAppContractTests(unittest.TestCase):
    def test_public_route_precedes_authenticated_routes(self):
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        resolver_route = 'parsed.path == "/api/public/tt-drama/resolve"'
        self.assertIn(resolver_route, source)
        self.assertLess(
            source.index(resolver_route),
            source.index('parsed.path == "/api/auth/status"'),
        )
        self.assertIn("self._dispatch_tt_drama_resolver(parsed)", source)

    def test_json_response_supports_observability_headers(self):
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn(
            "def json_response(handler, status_code, payload, no_store=False, extra_headers=None):",
            source,
        )
        self.assertIn('"X-TT-Drama-Cache": cache_state', source)
        self.assertIn('"Server-Timing": "tt-drama-resolver;dur=%.2f"', source)

    def test_bridge_fetches_before_building_destination(self):
        source = (ROOT / "static" / "tt-drama-search.js").read_text(
            encoding="utf-8"
        )
        resolver_call = "resolveDrama(contentId, controller.signal)"
        self.assertIn(resolver_call, source)
        self.assertIn("continueLink.removeAttribute(\"href\")", source)
        resolved_at = source.index(resolver_call)
        self.assertLess(
            resolved_at,
            source.index("showDrama(contentId, resolved)", resolved_at),
        )
        self.assertNotIn(
            "contentIdInput.value = normalizeContentId(contentIdInput.value)",
            source,
        )
        self.assertIn("await Promise.race([", source)

    def test_static_and_nginx_csp_allow_only_same_origin_api(self):
        html = (ROOT / "static" / "tt-drama-search.html").read_text(
            encoding="utf-8"
        )
        nginx = (
            ROOT / "deploy" / "nginx" / "tt-drama-search.conf"
        ).read_text(encoding="utf-8")
        for source in (html, nginx):
            self.assertIn("connect-src 'self'", source)
            self.assertIn("https://static-v1.mydramawave.com", source)
            self.assertNotIn("connect-src *", source)
        self.assertIn(
            "location = /api/public/tt-drama/resolve",
            nginx,
        )
        self.assertIn("proxy_set_header X-Real-IP $remote_addr", nginx)

    def test_result_link_has_no_seed_href(self):
        html = (ROOT / "static" / "tt-drama-search.html").read_text(
            encoding="utf-8"
        )
        self.assertIn('class="continue" id="continue-link" rel="noreferrer"', html)
        self.assertNotIn('id="continue-link" href="#"', html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
