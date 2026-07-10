#!/usr/bin/env python3
import argparse
import base64
import html as html_lib
import json
import os
import re
import shutil
import sys
import tempfile
import zipfile


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from fb_playable_generator import build_meta_playable_html, validate_meta_playable_html


TRANSLATIONS = {
    "en": {
        "headline": "Install to Play More",
        "subtitle": "Your playable preview has ended.",
        "cta": "Install to Play More",
        "plays": "Plays",
    }
}


def write_fixture(root):
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, "index.html"), "w", encoding="utf-8") as handle:
        handle.write(
            """<!doctype html><html><head>
<link rel="icon" href="icon.png"><style>.hero{background:url('icon.png')}</style>
</head><body><canvas id="game"></canvas><script src="loader.js"></script>
<script>fetch('config.json').then(function(r){return r.json();}).then(function(v){window.fixture=v;});</script>
</body></html>"""
        )
    with open(os.path.join(root, "loader.js"), "w", encoding="utf-8") as handle:
        handle.write(
            """function load(){var req=new XMLHttpRequest();req.open('GET','game.wasm',true);req.send();}
function _dmSysOpenURL(e,r){var t='https://store.example/app';window.location=t;return true}
function _emscripten_fixture(){return fetch('game.wasm')}load();"""
        )
    with open(os.path.join(root, "config.json"), "w", encoding="utf-8") as handle:
        json.dump({"ok": True}, handle)
    with open(os.path.join(root, "game.wasm"), "wb") as handle:
        handle.write(b"\x00asm\x01\x00\x00\x00")
    with open(os.path.join(root, "icon.png"), "wb") as handle:
        handle.write(b"\x89PNG\r\n\x1a\nfixture")


def decode_inner(document):
    match = re.search(r'<template id="game-source">(.*?)</template>', document, re.S)
    if not match:
        raise AssertionError("embedded game template not found")
    return html_lib.unescape(match.group(1))


def decode_embedded_scripts(inner):
    match = re.search(
        r"var __playableEncodedFiles=(\{.*?\});\s*var __playableByteCache",
        inner,
        re.S,
    )
    if not match:
        raise AssertionError("embedded resource map not found")
    resources = json.loads(match.group(1))
    scripts = []
    for key, (_, encoded) in resources.items():
        if key.lower().endswith((".js", ".mjs")):
            scripts.append(base64.b64decode(encoded).decode("utf-8"))
    return "\n".join(scripts)


def run_fixture(game_dir, entry="index.html", output_html=""):
    document, compatibility = build_meta_playable_html(
        game_dir,
        entry,
        "Fixture Playable",
        1,
        20,
        TRANSLATIONS,
    )
    validate_meta_playable_html(document)
    inner = decode_inner(document)
    embedded_scripts = decode_embedded_scripts(inner)
    runtime_source = inner + "\n" + embedded_scripts
    assertions = {
        "single_file": compatibility.get("single_file") is True,
        "meta_cta": "FbPlayableAd.onCTAClick" in document,
        "no_native_xhr": "XMLHttpRequest" not in runtime_source and "XMLHttpRequest" not in document,
        "no_native_fetch": re.search(r"\bfetch\s*\(", runtime_source) is None,
        "embedded_loader": "__PlayableXHR" in inner and "__playableRead" in inner,
        "defold_cta_bridge": 'meta-playable-cta' in runtime_source,
        "no_direct_redirect": "window.location=" not in runtime_source and "window.open(" not in runtime_source,
        "no_store_href": re.search(r"href=[\"']https?://", document, re.I) is None,
    }
    failed = [name for name, passed in assertions.items() if not passed]
    if failed:
        raise AssertionError("compatibility assertions failed: %s" % ", ".join(failed))

    with tempfile.TemporaryDirectory() as package_dir:
        index_path = os.path.join(package_dir, "index.html")
        zip_path = os.path.join(package_dir, "playable.zip")
        with open(index_path, "w", encoding="utf-8") as handle:
            handle.write(document)
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.write(index_path, "index.html")
        with zipfile.ZipFile(zip_path) as archive:
            if archive.namelist() != ["index.html"]:
                raise AssertionError("Meta package must contain only top-level index.html")
        zip_size = os.path.getsize(zip_path)

    if output_html:
        os.makedirs(os.path.dirname(os.path.abspath(output_html)), exist_ok=True)
        with open(output_html, "w", encoding="utf-8") as handle:
            handle.write(document)
    return {
        "ok": True,
        "html_size": len(document.encode("utf-8")),
        "zip_size": zip_size,
        "compatibility": compatibility,
        "assertions": assertions,
        "output_html": os.path.abspath(output_html) if output_html else "",
    }


def safe_extract(zip_path, target):
    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            destination = os.path.abspath(os.path.join(target, info.filename))
            if not destination.startswith(os.path.abspath(target) + os.sep):
                raise ValueError("unsafe zip entry: %s" % info.filename)
        archive.extractall(target)


def find_entry(root):
    candidates = []
    for current_root, _, filenames in os.walk(root):
        for filename in filenames:
            if filename.lower() == "index.html":
                candidates.append(os.path.relpath(os.path.join(current_root, filename), root).replace(os.sep, "/"))
    if not candidates:
        raise ValueError("fixture zip has no index.html")
    candidates.sort(key=lambda item: (item.count("/"), len(item), item))
    return candidates[0]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-zip", default="")
    parser.add_argument("--output-html", default="")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as root:
        if args.source_zip:
            safe_extract(os.path.abspath(args.source_zip), root)
            entry = find_entry(root)
            entry_dir = os.path.dirname(os.path.join(root, entry))
            entry_name = os.path.basename(entry)
            result = run_fixture(entry_dir, entry_name, args.output_html)
        else:
            write_fixture(root)
            result = run_fixture(root, "index.html", args.output_html)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
