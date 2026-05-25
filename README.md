# AI Drama Material Service

Drama material composition and GPU video worker service snapshot.

## Current GPU video behavior

- Prefetches episode video files with a rolling download pool.
- Concatenates original episode files in episode order without resizing, blurring, background filling, or overlaying the episode video.
- Keeps the 16:9 cover image as a short intro at the beginning of the output video. Default intro duration is `1` second via `DRAMA_INTRO_SECONDS`.
- Produces the normal concat video and the no-BGM video.
- Uploads generated outputs to COS and clears local GPU temporary files after successful upload.

## Deployment

1. Copy `.env.example` to `.env` on the server and fill secrets.
2. Install dependencies used by `app.py` in the Python environment.
3. Run the shared live-feature guard before upload:
   `python scripts/verify_live_feature_guard.py --root .`
4. Install `deploy/drama-material-api.service` as the systemd unit, adjusting paths if needed.
5. Start or restart the service with `systemctl restart drama-material-api.service`.
6. After deployment, verify the server copy with:
   `python3 scripts/verify_live_feature_guard.py --root /root/drama_material_service --public-root /usr/share/nginx/html`

Secrets are intentionally not committed.

## Module Isolation

Business projects must be kept in separate feature directories and standalone frontend pages. Do not add new project logic directly to the shared `app.py` or the shared `static/index.html`.

See `features/README.md` and `doc/deployment/live-feature-guard.md`.
