# Meta playable preview packaging

Public API document source: `doc/deployment/playable-preview-api.md`

Generated standalone HTML: `doc/deployment/playable-preview-api.html`

Renderer: `scripts/render_playable_preview_docs.py`. Install the pinned local build dependency with `python -m pip install -r requirements-playable-docs.txt`. It requires Python 3.10 or newer. Production Python 3.9 publishes and audits the tracked HTML without importing the renderer.

Published documents:

`https://advertising-1306474899.cos.ap-hongkong.myqcloud.com/ad-materials/docs/playable-preview-api.md`

`https://advertising-1306474899.cos.ap-hongkong.myqcloud.com/ad-materials/docs/playable-preview-api.html`

Canonical endpoint:

- `POST https://ai.yingliangads.com/api/fb-playable/preview`

The canonical `/api/fb-playable/preview` route also requires the tracked nginx include `deploy/nginx/fb-playable-preview.conf`, installed as `/etc/nginx/default.d/fb-playable-preview.conf`. Validate with `nginx -t` before reloading nginx.

Production authentication uses `PLAYABLE_PREVIEW_AUTH_MODE=enforce` with a strong server-only `PLAYABLE_PREVIEW_API_TOKEN`; `FB_PLAYABLE_API_TOKEN` is a migration alias. Send the token as a bearer token or `X-API-Token`. Missing or invalid credentials return `403`; enforce mode without a configured server token fails closed with `503`.

Accepted sources are a public remote entry URL passed as multipart/JSON `static_page` (legacy `static_page_url` and `game_url` remain supported), an HTML file or ZIP uploaded as multipart `static_page` (legacy upload field names remain supported), or JSON fields `static_html`, `static_html_base64`, or `static_zip_base64` (legacy `html`, `html_base64`, and `zip_base64` aliases remain accepted). Each request must provide exactly one source.

Only process controlled, trusted source bundles. The scanner, runtime guards, opaque-origin sandbox, and CSP are layered defenses; this endpoint is not a general-purpose isolation service for arbitrary hostile JavaScript.

The service converts the source into a Meta-oriented single-file package:

- the ZIP contains only top-level `index.html`;
- local scripts, styles, images, WASM, JSON, and archive files are embedded;
- remote URL mode recursively fetches statically discoverable relative HTML/CSS/JS/JSON dependencies, requires public HTTP(S), and keeps redirects/resources on the same origin and under the entry directory;
- missing runtime resources fail generation instead of returning `meta_compatible: true` for a page that cannot start; dynamically constructed dependencies must be supplied in a complete ZIP;
- inner `window.fetch` and `window.XMLHttpRequest` are rebound to the embedded resource map before game scripts run, so source loaders are handled without rewriting unrelated object methods or keys;
- executable `location` references are rewritten to a frozen facade, `window.open` is locked, and dynamic anchor navigation is captured, while Defold `sys.open_url` is bridged to the parent CTA;
- timer globals are locked to function-only wrappers, while eval/Function aliases and constructor recovery are rejected;
- the install button only calls `FbPlayableAd.onCTAClick()` and never opens the store URL directly;
- large runtime resources are compacted as LZMA plus script-safe Base94 and decoded before original game scripts execute;
- original scripts are resumed with CSP-safe script-node injection; generated playables never require `unsafe-eval`;
- the game iframe has an opaque-origin sandbox, while both outer and inner documents embed CSP that blocks connect, worker, object, and form-action capabilities;
- ZIP extraction rejects traversal, absolute paths, duplicate destinations, encrypted entries, symbolic/special entries, file/directory ancestor conflicts, more than `PLAYABLE_PREVIEW_MAX_EXTRACTED_FILES`, or more than `PLAYABLE_PREVIEW_MAX_EXTRACTED_BYTES`;
- multipart requests reject duplicate scalar fields and multiple upload files, and JSON Base64 uses strict validation;
- production limits playable generation with `PLAYABLE_PREVIEW_MAX_CONCURRENCY=1` by default and returns `429` when all slots are busy;
- both final UTF-8 `index.html` and the ZIP must be at or below `PLAYABLE_PREVIEW_MAX_ASSET_BYTES` (default `4,800,000` decimal bytes);
- `PLAYABLE_PREVIEW_MAX_ZIP_BYTES` remains as a backwards-compatible stricter ZIP cap, but can never raise the overall asset limit.

The service also publishes a separate `preview.html` browser shell. It loads the strict `index.html` from the same directory, injects a preview-only `FbPlayableAd.onCTAClick` host bridge, and navigates the top-level browser to `store_url`. The Meta ZIP still contains only the strict `index.html`; never upload `preview.html` to Meta.

Local build validation (Python 3.10+):

```powershell
python -m py_compile app.py fb_playable_generator.py scripts\test_fb_playable_generator.py scripts\render_playable_preview_docs.py scripts\test_playable_preview_docs.py scripts\publish_playable_preview_docs.py
python scripts\test_fb_playable_generator.py
python scripts\test_fb_playable_generator.py --source-zip <game.zip> --output-html <meta-index.html>
python scripts\render_playable_preview_docs.py
python scripts\render_playable_preview_docs.py --check
python scripts\test_playable_preview_docs.py
python scripts\publish_playable_preview_docs.py --check-only
```

Production validation (Python 3.9 compatible) deliberately does not import the local renderer:

```bash
python3 -m py_compile scripts/publish_playable_preview_docs.py
python3 scripts/publish_playable_preview_docs.py --check-only
```

For browser acceptance, serve both generated HTML files over HTTP. The strict `index.html` must pass with a CSP that allows inline scripts and WebAssembly but omits `unsafe-eval`, reach its interactive scene, and emit no CSP console errors. The `preview.html` shell must reach the same scene and its CTA must navigate to the configured `store_url`.

After deploying the exact Git commit to the CPU server, publish the tracked API document with the server-only COS environment:

```bash
python scripts/publish_playable_preview_docs.py
```

The publisher validates that the tracked HTML carries the normalized Markdown SHA-256 and passes a standard-library HTML safety audit. It pre-stages and reads back both COS payloads, updates Markdown first and HTML as the final commit object, rolls back fixed keys on failure, then atomically refreshes both nginx copies. Content types are `text/markdown; charset=utf-8` and `text/html; charset=utf-8`; both use `Cache-Control: no-cache`. Verify each public body, SHA-256, `Content-Type`, `Last-Modified`, and `ETag` after publishing. The Markdown remains the source of truth; the HTML is its deterministic reading view.

The response returns `preview_html_url` for the browser shell and `meta_html_url` for the strict Meta HTML. It also includes `preview_entry=preview.html`, `entry=index.html`, `preview_html_size`, `meta_compatible`, `compatibility`, `html_size`, `zip_size`, `meta_size_limit_bytes`, `size_headroom_bytes`, `manifest_url`, `documentation_url`, and the original `source_entry`.

When COS publishing is enabled, a successful request removes its local output directory only after all four remote objects are uploaded, preventing repeated previews from filling the server disk. Failed requests best-effort delete partial COS objects and always remove local partial output.
