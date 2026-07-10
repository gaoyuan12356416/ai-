# Meta playable preview packaging

Endpoints:

- `POST /api/ad-material/playable-preview` (legacy-compatible)
- `POST /api/fb-playable/preview`

Authentication uses `PLAYABLE_PREVIEW_API_TOKEN`, with `FB_PLAYABLE_API_TOKEN` as an alias. Send it as a bearer token or `X-API-Token`.

Accepted sources are an HTML file or ZIP uploaded as multipart field `static_page` (legacy field names remain supported), or JSON fields `static_html`, `static_html_base64`, or `static_zip_base64`.

The service converts the source into a Meta-oriented single-file package:

- the ZIP contains only top-level `index.html`;
- local scripts, styles, images, WASM, JSON, and archive files are embedded;
- source `XMLHttpRequest` and `fetch` loaders are redirected to the embedded resource map;
- direct JavaScript redirects are rejected, while Defold `sys.open_url` is bridged to the parent CTA;
- the install button only calls `FbPlayableAd.onCTAClick()` and never opens the store URL directly;
- large runtime resources are compacted as LZMA plus script-safe Base91 and decoded before original game scripts execute;
- both final UTF-8 `index.html` and the ZIP must be at or below `PLAYABLE_PREVIEW_MAX_ASSET_BYTES` (default `4,800,000` decimal bytes);
- `PLAYABLE_PREVIEW_MAX_ZIP_BYTES` remains as a backwards-compatible stricter ZIP cap, but can never raise the overall asset limit.

Validation:

```powershell
python -m py_compile app.py fb_playable_generator.py scripts\test_fb_playable_generator.py
python scripts\test_fb_playable_generator.py
python scripts\test_fb_playable_generator.py --source-zip <game.zip> --output-html <preview.html>
```

The response includes `meta_compatible`, `compatibility`, `html_size`, `zip_size`, `meta_size_limit_bytes`, `size_headroom_bytes`, `manifest_url`, the original `source_entry`, and final `entry=index.html`.
