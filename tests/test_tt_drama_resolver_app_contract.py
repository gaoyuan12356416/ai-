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

    def test_w2a_cache_is_explicitly_activated_without_leaking_internal_fields(self):
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        drop_in = (
            ROOT / "deploy" / "drama-material-api-tt-drama-resource.conf"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "Environment=TT_DRAMA_RESOURCE_SOURCE=w2a_cache",
            drop_in,
        )
        self.assertIn(
            "Environment=TT_DRAMA_RESOURCE_DB_PATH="
            "/mnt/data-disk/tt-drama-resource-cache/state/resources.sqlite3",
            drop_in,
        )
        self.assertNotIn("UMask=", drop_in)
        self.assertIn(
            "Environment=TT_DRAMA_RESOURCE_LANDING_ID=2049",
            drop_in,
        )
        self.assertIn(
            "TT_DRAMA_RESOURCE_LANDING_ID must be exactly 2049",
            source,
        )
        self.assertIn(
            'logging.error("%s; using mysql failback", exc)',
            source,
        )
        self.assertIn("TT_DRAMA_RESOLVER_PUBLIC_FIELDS", source)
        self.assertIn(
            "for key in TT_DRAMA_RESOLVER_PUBLIC_FIELDS",
            source,
        )

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
        self.assertIn(
            "location = /api/public/tt-drama/featured",
            nginx,
        )
        self.assertIn(
            "alias /mnt/data-disk/tt-drama-featured/public/current.json",
            nginx,
        )
        featured_location = nginx.split(
            "location = /api/public/tt-drama/featured", 1
        )[1].split("location =", 1)[0]
        self.assertNotIn("proxy_pass", featured_location)
        self.assertIn("public, max-age=300", featured_location)

    def test_result_link_has_no_seed_href(self):
        html = (ROOT / "static" / "tt-drama-search.html").read_text(
            encoding="utf-8"
        )
        self.assertIn('class="continue" id="continue-link" rel="noreferrer"', html)
        self.assertNotIn('id="continue-link" href="#"', html)

    def test_featured_cards_use_local_cache_and_existing_target_builder(self):
        script = (ROOT / "static" / "tt-drama-search.js").read_text(
            encoding="utf-8"
        )
        html = (ROOT / "static" / "tt-drama-search.html").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            'const FEATURED_PATH = "/api/public/tt-drama/featured"',
            script,
        )
        self.assertIn(
            "const target = createTarget(drama.content_id, search)",
            script,
        )
        self.assertIn('card.dataset.contentId = target.contentId', script)
        self.assertIn("card.dataset.targetUrl = target.url", script)
        self.assertIn('card.href = `#story-${target.contentId}`', script)
        click_handler = script.split(
            'stories.addEventListener("click"', 1
        )[1]
        self.assertLess(
            click_handler.index(
                "resolveDrama(card.dataset.contentId, controller.signal)"
            ),
            click_handler.index(
                "root.location.assign(card.dataset.targetUrl)"
            ),
        )
        self.assertIn('id="recent-note"', html)
        self.assertIn("story-link:focus-visible", html)

    def test_featured_refresh_is_offline_and_data_disk_backed(self):
        service = (
            ROOT / "deploy" / "tt-drama-featured.service"
        ).read_text(encoding="utf-8")
        timer = (
            ROOT / "deploy" / "tt-drama-featured.timer"
        ).read_text(encoding="utf-8")
        self.assertIn("Type=oneshot", service)
        self.assertIn("User=tt-drama-featured", service)
        self.assertIn(
            "EnvironmentFile=/etc/tt-drama-featured.env",
            service,
        )
        self.assertIn("ConditionPathIsMountPoint=/mnt/data-disk", service)
        self.assertIn(
            "ReadWritePaths=/mnt/data-disk/tt-drama-featured/public",
            service,
        )
        self.assertIn("15:30:00 Asia/Shanghai", timer)
        self.assertIn("18:00:00 Asia/Shanghai", timer)
        self.assertIn("Persistent=true", timer)


if __name__ == "__main__":
    unittest.main(verbosity=2)
