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
    "file+'?v='+version,{cache:'default',credentials:'same-origin'}" in GENERATOR,
    "versioned daily detail requests must allow the private browser cache",
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
