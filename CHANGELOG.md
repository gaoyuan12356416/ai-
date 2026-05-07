## 2026-05-07 1s cover intro

- Changed the default GPU cover intro duration from 3 seconds to 1 second.

## 2026-05-07 raw episode concat on GPU

- Changed GPU video render to concatenate downloaded episode files directly in episode order.
- Kept 16:9 cover intro generation and insertion at the beginning of concat videos.
- Removed per-episode background/blur/overlay normalization from the GPU render path.
- Kept rolling prefetch downloads and post-COS local cleanup.
- Backup before final intro restore: app.py.bak.restore-cover-intro.20260507143621


