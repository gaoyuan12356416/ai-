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
- output above `PLAYABLE_PREVIEW_MAX_ZIP_BYTES` (default 5 MB) is rejected.

Validation:

```powershell
python -m py_compile app.py fb_playable_generator.py scripts\test_fb_playable_generator.py
python scripts\test_fb_playable_generator.py
python scripts\test_fb_playable_generator.py --source-zip <game.zip> --output-html <preview.html>
```

The response includes `meta_compatible`, `compatibility`, `zip_size`, `manifest_url`, the original `source_entry`, and final `entry=index.html`.
