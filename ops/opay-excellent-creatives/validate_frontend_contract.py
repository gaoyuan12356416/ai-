#!/usr/bin/env python3
import re
import subprocess
import tempfile
from pathlib import Path


HERE = Path(__file__).resolve().parent
html = (HERE / "report.html").read_text(encoding="utf-8")

required = [
    '<meta name="robots" content="noindex,nofollow,noarchive">',
    'fetch("latest.json",{cache:"no-store"})',
    "monthFilter",
    "channelFilter",
    "appFilter",
    "typeFilter",
    "keywordFilter",
    "makerFilter",
    "ruleFilter",
    "导出当前 CSV",
    "AF D0 首交",
    "素材制作者",
    "openPreview",
    "openDetail",
]
missing = [item for item in required if item not in html]
if missing:
    raise SystemExit("missing frontend contract tokens: %s" % missing)
if "api/auth/feishu" in html.casefold() or "auth_request" in html.casefold():
    raise SystemExit("public report must not include Feishu authentication")
if re.search(r"<script\s+[^>]*src=", html, flags=re.I):
    raise SystemExit("report must not depend on external JavaScript")

scripts = re.findall(r"<script>(.*?)</script>", html, flags=re.S | re.I)
if len(scripts) != 1:
    raise SystemExit("expected exactly one inline script")
with tempfile.TemporaryDirectory(prefix="opay-frontend-") as temporary:
    script_path = Path(temporary) / "report.js"
    script_path.write_text(scripts[0], encoding="utf-8")
    process = subprocess.run(
        ["node", "--check", str(script_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if process.returncode:
        raise SystemExit(process.stderr)

print("frontend contract: PASS")
