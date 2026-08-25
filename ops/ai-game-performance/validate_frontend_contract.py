#!/usr/bin/env python3
"""Validate the static report shell without making a network request."""

import re
import shutil
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent
HTML = (ROOT / "report.html").read_text(encoding="utf-8")


def require(value, message):
    if not value:
        raise AssertionError(message)


def main():
    for element_id in (
        "startDate",
        "endDate",
        "gameFilter",
        "channelFilter",
        "countryFilter",
        "viewTabs",
        "dimensionList",
        "cards",
        "trend",
        "tableHead",
        "tableBody",
        "exportBtn",
    ):
        require('id="%s"' % element_id in HTML, "missing element %s" % element_id)
    for contract in (
        'fetch("latest.json?v="+Date.now()',
        'cache:"no-store"',
        "caches.open(CACHE_NAME)",
        "x-ai-game-cache-saved-at",
        "manifest.meta.data_version",
        "mapping_status",
        "Unity兜底",
        'requestedView==="delivery"?["delivery","conversion"]',
        'out.source_country="仅转化侧"',
        "渠道事实 ${INT.format(sourceRows)} 行 + 转化事实",
        'delivery:["effective_spend","source_spend","manual_cost"',
        "URL.createObjectURL",
        'new Blob(["\\ufeff"+lines.join("\\r\\n")]',
        "document.body.appendChild(link)",
        "window.setTimeout(()=>{URL.revokeObjectURL(link.href);link.remove()},1000)",
        "replaceChildren",
    ):
        require(contract in HTML, "missing frontend contract: %s" % contract)
    require("innerHTML" not in HTML, "data must not be rendered with innerHTML")
    require("<script src=" not in HTML and "<link rel=\"stylesheet\"" not in HTML, "report shell must not load third-party assets")
    scripts = re.findall(r"<script>(.*?)</script>", HTML, flags=re.DOTALL | re.IGNORECASE)
    require(len(scripts) == 1, "expected one inline application script")
    node = shutil.which("node")
    if node:
        with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as handle:
            handle.write(scripts[0])
            script_path = Path(handle.name)
        try:
            result = subprocess.run([node, "--check", str(script_path)], capture_output=True, text=True)
            require(result.returncode == 0, "node --check failed: %s" % result.stderr.strip())
        finally:
            script_path.unlink(missing_ok=True)
    print("frontend_contract=PASS")


if __name__ == "__main__":
    main()
