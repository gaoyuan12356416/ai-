#!/usr/bin/env python3
"""Static validation for the V3 HTML usage guide."""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import sys
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "usage-guide.html"
STANDALONE = ROOT / "usage-guide-standalone.html"
EXPECTED_ASSETS = {
    "01-rule-group-list.jpg",
    "02-scope-and-estimate.jpg",
    "03-object-observe.jpg",
    "04-rule-conditions.jpg",
    "05-schedule-quota.jpg",
    "06-review-safety.jpg",
    "08-execution-log-list.jpg",
    "09-log-detail-zero-meta.jpg",
    "10-log-integrity-zero-meta.jpg",
}


class GuideParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: list[str] = []
        self.hrefs: list[str] = []
        self.images: list[dict[str, str]] = []
        self.scripts: list[dict[str, str]] = []
        self.links: list[dict[str, str]] = []
        self.inline_handlers: list[tuple[str, str]] = []
        self.title_depth = 0
        self.title_parts: list[str] = []
        self.script_depth = 0
        self.inline_script_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: value or "" for key, value in attrs}
        if attributes.get("id"):
            self.ids.append(attributes["id"])
        if attributes.get("href"):
            self.hrefs.append(attributes["href"])
        if tag == "img":
            self.images.append(attributes)
        if tag == "script":
            self.scripts.append(attributes)
            if not attributes.get("src"):
                self.script_depth += 1
        if tag == "link":
            self.links.append(attributes)
        for name, value in attrs:
            if name.lower().startswith("on"):
                self.inline_handlers.append((name, value or ""))
        if tag == "title":
            self.title_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "title" and self.title_depth:
            self.title_depth -= 1
        if tag == "script" and self.script_depth:
            self.script_depth -= 1

    def handle_data(self, data: str) -> None:
        if self.title_depth:
            self.title_parts.append(data)
        if self.script_depth:
            self.inline_script_parts.append(data)


def fail(errors: list[str], message: str) -> None:
    errors.append(message)


def parse(path: Path) -> tuple[str, GuideParser]:
    text = path.read_text(encoding="utf-8")
    parser = GuideParser()
    parser.feed(text)
    parser.close()
    return text, parser


def validate_common(path: Path, text: str, parser: GuideParser, errors: list[str]) -> None:
    lowered = text.lower()
    if not lowered.lstrip().startswith("<!doctype html>"):
        fail(errors, f"{path.name}: missing leading HTML5 doctype")
    if '<meta charset="utf-8">' not in lowered:
        fail(errors, f"{path.name}: missing UTF-8 charset")
    if "AI 自动规则调控 V3" not in "".join(parser.title_parts):
        fail(errors, f"{path.name}: unexpected or missing title")
    duplicates = [item for item, count in Counter(parser.ids).items() if count > 1]
    if duplicates:
        fail(errors, f"{path.name}: duplicate ids: {duplicates}")
    known_ids = set(parser.ids)
    missing_anchors = sorted({
        href[1:] for href in parser.hrefs
        if href.startswith("#") and href[1:] not in known_ids
    })
    if missing_anchors:
        fail(errors, f"{path.name}: missing anchor targets: {missing_anchors}")
    if re.search(r"javascript\s*:", text, flags=re.IGNORECASE):
        fail(errors, f"{path.name}: javascript URL found")
    if parser.inline_handlers:
        fail(errors, f"{path.name}: inline event attributes found: {parser.inline_handlers}")
    if "\ufffd" in text or re.search(r"\?{3,}", text):
        fail(errors, f"{path.name}: replacement characters or question-mark mojibake found")
    external_scripts = [item.get("src", "") for item in parser.scripts if item.get("src")]
    external_styles = [item.get("href", "") for item in parser.links if item.get("rel") == "stylesheet"]
    if external_scripts or external_styles:
        fail(errors, f"{path.name}: external script/style dependencies found")
    if "@media (max-width: 820px)" not in text or "@media print" not in text:
        fail(errors, f"{path.name}: responsive or print stylesheet missing")
    node = shutil.which("node")
    if node and parser.inline_script_parts:
        script = "\n".join(parser.inline_script_parts)
        result = subprocess.run(
            [node, "--check", "-"],
            input=script,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )
        if result.returncode:
            fail(
                errors,
                f"{path.name}: inline JavaScript syntax failed: "
                f"{(result.stderr or result.stdout).strip()}",
            )
    for index, image in enumerate(parser.images, start=1):
        if not image.get("alt", "").strip():
            fail(errors, f"{path.name}: image {index} has empty alt text")
        if not image.get("width") or not image.get("height"):
            fail(errors, f"{path.name}: image {index} is missing intrinsic dimensions")


def validate_source(text: str, parser: GuideParser, errors: list[str]) -> None:
    source_assets: set[str] = set()
    for image in parser.images:
        src = unquote(image.get("src", ""))
        if src.startswith("data:"):
            fail(errors, "usage-guide.html: source guide unexpectedly embeds a data URI")
            continue
        image_path = (ROOT / src).resolve()
        try:
            image_path.relative_to(ROOT.resolve())
        except ValueError:
            fail(errors, f"usage-guide.html: image escapes guide directory: {src}")
            continue
        if not image_path.is_file():
            fail(errors, f"usage-guide.html: missing image: {src}")
            continue
        if image_path.stat().st_size == 0:
            fail(errors, f"usage-guide.html: empty image: {src}")
        if image_path.suffix.lower() in {".jpg", ".jpeg"}:
            payload = image_path.read_bytes()
            if not (payload.startswith(b"\xff\xd8") and payload.endswith(b"\xff\xd9")):
                fail(errors, f"usage-guide.html: invalid JPEG markers: {src}")
            actual_width, actual_height = jpeg_dimensions(payload)
            expected_width = int(image.get("width") or 0)
            expected_height = int(image.get("height") or 0)
            if (actual_width, actual_height) != (expected_width, expected_height):
                fail(
                    errors,
                    "usage-guide.html: intrinsic dimensions mismatch for "
                    f"{src}; HTML={expected_width}x{expected_height}, "
                    f"JPEG={actual_width}x{actual_height}",
                )
        source_assets.add(Path(src).name)
    if source_assets != EXPECTED_ASSETS:
        missing = sorted(EXPECTED_ASSETS - source_assets)
        extra = sorted(source_assets - EXPECTED_ASSETS)
        fail(errors, f"usage-guide.html: asset set mismatch; missing={missing}, extra={extra}")
    if "Meta 写入 0 次" not in text:
        fail(errors, "usage-guide.html: required zero-Meta-write safety statement missing")
    required_current_statements = {
        "当前生产已接通": "current live capability statement",
        "[*copybyAI*MMDDHHmm]": "copy-name suffix contract",
        "UTC+8": "fixed business display timezone",
        "ads_ai.ads_facebook_auto_created_data": "FB created_data ledger",
        "ads_ai.ad_control_v3_copy_intent": "copy intent ledger",
        "ads_ai.ad_control_copy_lineage": "copy lineage ledger",
        "1031273318485141": "Dramawave App ID catalog rule",
        "成功后返回规则组列表": "save-and-preview navigation behavior",
        "TikTok": "future-channel boundary",
    }
    for marker, label in required_current_statements.items():
        if marker not in text:
            fail(errors, f"usage-guide.html: missing {label}: {marker}")
    stale_statements = (
        "当前 R1",
        "当前 R2",
        "copy_live_ready=false",
        "copy_persistence_not_configured",
        "R1 仅保存",
        "R1 不判断",
        "等 Runner 发布",
    )
    for marker in stale_statements:
        if marker in text:
            fail(errors, f"usage-guide.html: stale release statement remains: {marker}")


def jpeg_dimensions(payload: bytes) -> tuple[int, int]:
    """Return JPEG width/height without requiring Pillow."""
    sof_markers = {
        0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
        0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
    }
    offset = 2
    while offset + 9 < len(payload):
        if payload[offset] != 0xFF:
            offset += 1
            continue
        while offset < len(payload) and payload[offset] == 0xFF:
            offset += 1
        if offset >= len(payload):
            break
        marker = payload[offset]
        offset += 1
        if marker in {0xD8, 0xD9}:
            continue
        if offset + 2 > len(payload):
            break
        segment_length = int.from_bytes(payload[offset:offset + 2], "big")
        if segment_length < 2 or offset + segment_length > len(payload):
            break
        if marker in sof_markers:
            height = int.from_bytes(payload[offset + 3:offset + 5], "big")
            width = int.from_bytes(payload[offset + 5:offset + 7], "big")
            if width <= 0 or height <= 0:
                break
            return width, height
        offset += segment_length
    raise ValueError("JPEG SOF dimensions not found")


def validate_standalone(
    text: str,
    parser: GuideParser,
    source_text: str,
    errors: list[str],
) -> None:
    expected_hash = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
    hash_match = re.search(r"<!-- Source-SHA256: ([0-9a-f]{64}) -->", text)
    if not hash_match:
        fail(errors, "usage-guide-standalone.html: source hash marker missing")
    elif hash_match.group(1) != expected_hash:
        fail(
            errors,
            "usage-guide-standalone.html: stale build; "
            f"embedded source hash {hash_match.group(1)} != {expected_hash}",
        )
    if "usage-guide-assets/" in text:
        fail(errors, "usage-guide-standalone.html: relative screenshot references remain")
    data_images = [image.get("src", "") for image in parser.images if image.get("src", "").startswith("data:image/")]
    if len(data_images) != len(EXPECTED_ASSETS):
        fail(
            errors,
            "usage-guide-standalone.html: expected "
            f"{len(EXPECTED_ASSETS)} embedded images, got {len(data_images)}",
        )
    if any(len(uri) < 1000 for uri in data_images):
        fail(errors, "usage-guide-standalone.html: one or more embedded images are unexpectedly small")


def main() -> int:
    errors: list[str] = []
    for required in (SOURCE, STANDALONE):
        if not required.is_file():
            fail(errors, f"missing required file: {required}")
    if errors:
        print("\n".join(f"ERROR: {item}" for item in errors))
        return 1

    source_text, source_parser = parse(SOURCE)
    standalone_text, standalone_parser = parse(STANDALONE)
    validate_common(SOURCE, source_text, source_parser, errors)
    validate_common(STANDALONE, standalone_text, standalone_parser, errors)
    validate_source(source_text, source_parser, errors)
    validate_standalone(standalone_text, standalone_parser, source_text, errors)

    if errors:
        print("\n".join(f"ERROR: {item}" for item in errors))
        return 1

    print(
        "PASS: source and standalone guides validated; "
        f"{len(source_parser.ids)} ids, {len(source_parser.images)} screenshots, "
        f"{len(source_parser.hrefs)} links."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
