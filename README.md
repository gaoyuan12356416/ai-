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
3. Install `deploy/drama-material-api.service` as the systemd unit, adjusting paths if needed.
4. Start or restart the service with `systemctl restart drama-material-api.service`.

Secrets are intentionally not committed.
