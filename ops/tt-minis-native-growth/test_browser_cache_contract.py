from pathlib import Path


ROOT = Path(__file__).resolve().parent
GENERATOR = (ROOT / "tt_minis_multi_dim_dashboard.py").read_text(encoding="utf-8")
NGINX = (ROOT / "tt-minis-native-growth-auth.conf").read_text(encoding="utf-8")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


require(
    "fetch('latest.json?v='+Date.now(),{cache:'no-store'" in GENERATOR,
    "latest.json must remain a fresh, no-store manifest request",
)
require(
    "DETAIL_CACHE_TTL_MS=15*60*1000" in GENERATOR,
    "application-managed daily detail cache must expire after 15 minutes",
)
require(
    "caches.open(DETAIL_CACHE_NAME)" in GENERATOR
    and "cache.match(url)" in GENERATOR
    and "cache.put(url" in GENERATOR,
    "daily detail requests must use same-origin Cache Storage",
)
require(
    "part:await fetchDailyJson(url)" in GENERATOR,
    "daily detail loading must pass through the TTL cache helper",
)
require(
    "let version=encodeURIComponent((DATA.meta&&DATA.meta.generated_at)||'')" in GENERATOR,
    "daily detail cache keys must remain tied to generated_at",
)
require(
    "location ^~ /reports/tt-minis-native-growth/data/" in NGINX,
    "nginx must have a dedicated daily detail location",
)
require(
    'add_header Cache-Control "private, max-age=900" always;' in NGINX,
    "daily detail responses must be private and expire after 15 minutes",
)
require(
    'add_header Cache-Control "private, no-store" always;' in NGINX,
    "the report shell and manifest must remain private and no-store",
)

print("browser cache contract: ok")
