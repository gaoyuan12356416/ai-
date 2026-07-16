#!/usr/bin/env python3
import argparse
import hashlib
import html
import json
import os
import re
import sys


if sys.version_info < (3, 10):  # pragma: no cover - production publishes the tracked artifact
    raise RuntimeError("the local HTML renderer requires Python 3.10 or newer")

try:
    import markdown_it
    from markdown_it import MarkdownIt
except ImportError as exc:  # pragma: no cover - exercised only on missing dev dependency
    raise RuntimeError(
        "markdown-it-py is required to render the HTML document; "
        "install markdown-it-py==4.0.0 in the local build environment"
    ) from exc

if markdown_it.__version__ != "4.0.0":  # pragma: no cover - deterministic build guard
    raise RuntimeError(
        "playable preview docs require markdown-it-py==4.0.0; found %s"
        % markdown_it.__version__
    )


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DEFAULT_SOURCE = os.path.join(ROOT, "doc", "deployment", "playable-preview-api.md")
DEFAULT_OUTPUT = os.path.join(ROOT, "doc", "deployment", "playable-preview-api.html")
CANONICAL_ENDPOINT = "https://ai.yingliangads.com/api/fb-playable/preview"
LEGACY_ENDPOINT = "/api/ad-material/playable-preview"
MARKDOWN_PUBLIC_URL = (
    "https://advertising-1306474899.cos.ap-hongkong.myqcloud.com/"
    "ad-materials/docs/playable-preview-api.md"
)


def validate_markdown(source_text):
    if not source_text.startswith("# "):
        raise ValueError("playable preview API document must start with a Markdown title")
    if LEGACY_ENDPOINT in source_text:
        raise ValueError("legacy playable preview endpoint must not appear in the document")
    api_urls = sorted(set(re.findall(r"https://ai\.yingliangads\.com/api/[^\s'\"`]+", source_text)))
    if api_urls != [CANONICAL_ENDPOINT]:
        raise ValueError("document must contain only the canonical playable preview API URL")


def heading_slug(title, level, index, seen):
    if level == 1 and "top" not in seen:
        candidate = "top"
    else:
        numbered = re.match(r"^\s*(\d+)(?:\.(\d+))?\.?\s*", title)
        if numbered:
            parts = [part for part in numbered.groups() if part]
            candidate = "section-" + "-".join(parts)
        else:
            candidate = "section-%d" % index
    base = candidate
    suffix = 2
    while candidate in seen:
        candidate = "%s-%d" % (base, suffix)
        suffix += 1
    seen.add(candidate)
    return candidate


def render_markdown(source_text):
    markdown = MarkdownIt(
        "commonmark",
        {
            "html": False,
            "linkify": False,
            "typographer": True,
        },
    )
    markdown.enable("table")
    markdown.enable("strikethrough")

    tokens = markdown.parse(source_text)
    headings = []
    seen = set()
    heading_index = 0
    for index, token in enumerate(tokens):
        if token.type != "heading_open":
            continue
        heading_index += 1
        level = int(token.tag[1:])
        title = tokens[index + 1].content if index + 1 < len(tokens) else ""
        slug = heading_slug(title, level, heading_index, seen)
        token.attrSet("id", slug)
        headings.append({"level": level, "title": title, "slug": slug})

    body_tokens = tokens
    if (
        len(tokens) >= 3
        and tokens[0].type == "heading_open"
        and tokens[0].tag == "h1"
        and tokens[2].type == "heading_close"
    ):
        body_tokens = tokens[3:]
    body = markdown.renderer.render(body_tokens, markdown.options, {})
    return body, headings


def render_toc(headings):
    items = []
    for item in headings:
        if item["level"] not in (2, 3):
            continue
        css_class = "toc-subitem" if item["level"] == 3 else "toc-item"
        items.append(
            '<li class="%s"><a href="#%s">%s</a></li>'
            % (
                css_class,
                html.escape(item["slug"], quote=True),
                html.escape(item["title"]),
            )
        )
    return "\n".join(items)


def render_document(source_text, source_sha256):
    validate_markdown(source_text)
    body, headings = render_markdown(source_text)
    toc = render_toc(headings)
    title = headings[0]["title"] if headings else "Meta/Facebook 试玩广告生成接口"
    return """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; object-src 'none'; form-action 'none'">
  <meta name="playable-doc-source-sha256" content="%(source_sha256)s">
  <title>%(title)s</title>
  <style>
    :root {
      --page: #f4f7fb;
      --surface: #ffffff;
      --surface-soft: #f8fafc;
      --ink: #172033;
      --muted: #64748b;
      --line: #dbe3ee;
      --brand: #2563eb;
      --brand-dark: #1d4ed8;
      --brand-soft: #eaf1ff;
      --success: #047857;
      --code: #0f172a;
      --code-ink: #e2e8f0;
      --shadow: 0 16px 45px rgba(15, 23, 42, 0.08);
      --radius: 16px;
    }

    * { box-sizing: border-box; }
    html { scroll-behavior: smooth; scroll-padding-top: 24px; }
    body {
      margin: 0;
      color: var(--ink);
      background: var(--page);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
        "Hiragino Sans GB", "Microsoft YaHei", Arial, sans-serif;
      font-size: 16px;
      line-height: 1.72;
      text-rendering: optimizeLegibility;
    }

    a { color: var(--brand); text-decoration: none; }
    a:hover { color: var(--brand-dark); text-decoration: underline; }
    .skip-link {
      position: fixed;
      left: 16px;
      top: -60px;
      z-index: 100;
      padding: 10px 14px;
      color: #fff;
      background: var(--brand-dark);
      border-radius: 8px;
      transition: top 0.2s ease;
    }
    .skip-link:focus { top: 16px; }

    .hero {
      color: #fff;
      background:
        radial-gradient(circle at 88%% 15%%, rgba(96, 165, 250, 0.55), transparent 32%%),
        linear-gradient(135deg, #0f2d69 0%%, #1d4ed8 55%%, #2563eb 100%%);
      border-bottom: 1px solid rgba(255,255,255,0.18);
    }
    .hero-inner {
      width: min(1180px, calc(100%% - 40px));
      margin: 0 auto;
      padding: 48px 0 42px;
    }
    .eyebrow {
      margin: 0 0 10px;
      color: #bfdbfe;
      font-size: 13px;
      font-weight: 750;
      letter-spacing: 0.12em;
      text-transform: uppercase;
    }
    .hero h1 {
      margin: 0 0 20px;
      max-width: 850px;
      font-size: clamp(30px, 4.4vw, 48px);
      line-height: 1.18;
      letter-spacing: -0.02em;
    }
    .endpoint {
      display: inline-flex;
      max-width: 100%%;
      align-items: center;
      gap: 11px;
      padding: 10px 14px;
      background: rgba(15, 23, 42, 0.42);
      border: 1px solid rgba(255,255,255,0.2);
      border-radius: 12px;
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.08);
    }
    .method {
      flex: 0 0 auto;
      padding: 3px 8px;
      color: #d1fae5;
      background: rgba(5, 150, 105, 0.42);
      border: 1px solid rgba(167, 243, 208, 0.35);
      border-radius: 6px;
      font-size: 12px;
      font-weight: 800;
      letter-spacing: 0.05em;
    }
    .endpoint code {
      min-width: 0;
      overflow-wrap: anywhere;
      color: #fff;
      background: transparent;
      font-size: 14px;
    }

    .layout {
      display: grid;
      grid-template-columns: 250px minmax(0, 1fr);
      gap: 26px;
      width: min(1180px, calc(100%% - 40px));
      margin: 28px auto 60px;
      align-items: start;
    }
    .toc {
      position: sticky;
      top: 20px;
      max-height: calc(100vh - 40px);
      overflow: auto;
      padding: 18px 16px;
      background: rgba(255,255,255,0.94);
      border: 1px solid var(--line);
      border-radius: 14px;
      box-shadow: 0 8px 24px rgba(15, 23, 42, 0.05);
    }
    .toc-title {
      margin: 0 0 10px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 800;
      letter-spacing: 0.1em;
      text-transform: uppercase;
    }
    .toc ul { margin: 0; padding: 0; list-style: none; }
    .toc li { margin: 2px 0; }
    .toc a {
      display: block;
      padding: 7px 9px;
      color: #334155;
      border-radius: 7px;
      font-size: 13px;
      line-height: 1.42;
    }
    .toc a:hover { color: var(--brand-dark); background: var(--brand-soft); text-decoration: none; }
    .toc-subitem a { padding-left: 22px; color: var(--muted); }

    article {
      min-width: 0;
      padding: clamp(24px, 4vw, 50px);
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
      overflow-wrap: anywhere;
    }
    article > h1:first-child { margin-top: 0; }
    article h1 {
      margin: 0 0 24px;
      padding-bottom: 18px;
      border-bottom: 1px solid var(--line);
      font-size: clamp(27px, 3.6vw, 38px);
      line-height: 1.25;
      letter-spacing: -0.02em;
    }
    article h2 {
      margin: 48px 0 18px;
      padding-top: 6px;
      font-size: clamp(23px, 2.6vw, 30px);
      line-height: 1.3;
      letter-spacing: -0.01em;
    }
    article h3 {
      margin: 32px 0 13px;
      font-size: 20px;
      line-height: 1.4;
    }
    article p { margin: 12px 0 18px; }
    article ul, article ol { margin: 12px 0 20px; padding-left: 1.55em; }
    article li { margin: 6px 0; }
    article li::marker { color: var(--brand); font-weight: 700; }
    article strong { color: #0f172a; }

    code, pre { font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace; }
    :not(pre) > code {
      padding: 0.16em 0.38em;
      color: #1e40af;
      background: #eff6ff;
      border: 1px solid #dbeafe;
      border-radius: 5px;
      font-size: 0.9em;
    }
    pre {
      position: relative;
      max-width: 100%%;
      margin: 16px 0 24px;
      padding: 18px 20px;
      overflow: auto;
      color: var(--code-ink);
      background: var(--code);
      border: 1px solid #1e293b;
      border-radius: 12px;
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.04);
      line-height: 1.6;
      tab-size: 2;
    }
    pre code { color: inherit; background: transparent; border: 0; font-size: 13px; }

    table {
      display: block;
      width: 100%%;
      max-width: 100%%;
      margin: 16px 0 26px;
      overflow-x: auto;
      border-collapse: collapse;
      border-spacing: 0;
      font-size: 14px;
    }
    thead { background: #eef4ff; }
    th, td {
      min-width: 110px;
      padding: 11px 13px;
      text-align: left;
      vertical-align: top;
      border: 1px solid var(--line);
    }
    th { color: #1e3a8a; font-weight: 750; white-space: nowrap; }
    tbody tr:nth-child(even) { background: var(--surface-soft); }

    .doc-footer {
      margin-top: 52px;
      padding-top: 20px;
      color: var(--muted);
      border-top: 1px solid var(--line);
      font-size: 13px;
    }
    .doc-footer code { font-size: 11px; }

    @media (max-width: 900px) {
      .layout { grid-template-columns: 1fr; width: min(100%% - 24px, 820px); }
      .toc { position: static; max-height: none; }
      .toc ul { columns: 2; column-gap: 18px; }
      .toc li { break-inside: avoid; }
      .hero-inner { width: min(100%% - 28px, 820px); padding: 36px 0 32px; }
    }
    @media (max-width: 580px) {
      body { font-size: 15px; }
      .endpoint { align-items: flex-start; }
      .layout { margin-top: 14px; }
      .toc ul { columns: 1; }
      article { padding: 22px 18px 32px; border-radius: 12px; }
      article h2 { margin-top: 38px; }
      pre { padding: 15px; }
      th, td { padding: 9px 10px; }
    }
    @media print {
      body { background: #fff; }
      .hero { color: #111827; background: #fff; border-bottom: 1px solid #cbd5e1; }
      .eyebrow { color: #475569; }
      .endpoint { color: #111827; background: #f8fafc; border-color: #cbd5e1; }
      .endpoint code { color: #111827; }
      .method { color: #065f46; background: #d1fae5; }
      .layout { display: block; width: 100%%; margin: 0; }
      .toc { display: none; }
      article { padding: 24px 0; border: 0; box-shadow: none; }
      pre { white-space: pre-wrap; }
    }
  </style>
</head>
<body>
  <a class="skip-link" href="#document">跳转到接口文档正文</a>
  <header class="hero">
    <div class="hero-inner">
      <p class="eyebrow">API Reference · Meta Playable Preview</p>
      <h1>%(title)s</h1>
      <div class="endpoint" aria-label="唯一接口地址">
        <span class="method">POST</span>
        <code>%(endpoint)s</code>
      </div>
    </div>
  </header>
  <main class="layout" id="document">
    <nav class="toc" aria-label="文档目录">
      <p class="toc-title">文档目录</p>
      <ul>
%(toc)s
      </ul>
    </nav>
    <article>
%(body)s
      <footer class="doc-footer">
        <p>该页面由 Git 跟踪的 Markdown 自动生成。<a href="%(markdown_url)s">查看 Markdown 源文档</a></p>
        <p>源文档 SHA-256：<code>%(source_sha256)s</code></p>
      </footer>
    </article>
  </main>
</body>
</html>
""" % {
        "source_sha256": html.escape(source_sha256, quote=True),
        "title": html.escape(title),
        "endpoint": html.escape(CANONICAL_ENDPOINT),
        "toc": toc,
        "body": body,
        "markdown_url": html.escape(MARKDOWN_PUBLIC_URL, quote=True),
    }


def render_file(source, output):
    with open(source, "rb") as handle:
        source_payload = handle.read()
    source_text = source_payload.decode("utf-8")
    normalized_text = source_text.replace("\r\n", "\n").replace("\r", "\n")
    normalized_payload = normalized_text.encode("utf-8")
    source_sha256 = hashlib.sha256(normalized_payload).hexdigest()
    rendered = render_document(normalized_text, source_sha256).encode("utf-8")
    return rendered, {
        "source": os.path.abspath(source),
        "source_sha256": source_sha256,
        "output": os.path.abspath(output),
        "html_size": len(rendered),
        "html_sha256": hashlib.sha256(rendered).hexdigest(),
    }


def main():
    parser = argparse.ArgumentParser(description="Render the playable preview API Markdown as standalone HTML")
    parser.add_argument("--source", default=DEFAULT_SOURCE)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    source = os.path.abspath(args.source)
    output = os.path.abspath(args.output)
    rendered, result = render_file(source, output)
    if args.check:
        if not os.path.isfile(output):
            raise ValueError("rendered HTML document is missing: %s" % output)
        with open(output, "rb") as handle:
            current = handle.read()
        if current != rendered:
            raise ValueError("rendered HTML document is stale; regenerate it from the Markdown source")
        result["current"] = True
    else:
        os.makedirs(os.path.dirname(output), exist_ok=True)
        temporary = output + ".tmp"
        with open(temporary, "wb") as handle:
            handle.write(rendered)
        os.replace(temporary, output)
        result["current"] = True
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
