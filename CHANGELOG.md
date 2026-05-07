## 2026-05-07 screenshot retry missing sizes only

- Changed screenshot retry confirmation copy to say only failed or missing sizes will be regenerated.
- Changed screenshot retry processing to reuse the existing cover source URL so partial retries do not fail during external material revalidation.

## 2026-05-07 screenshot item retry

- Added per-ratio screenshot generation retries so a failed image size is retried up to three times before the task fails.
- Kept successful screenshot sizes reusable during retry instead of regenerating every output.

## 2026-05-07 COS upload hang guard

- Added COS existing-object reuse for same-size outputs to avoid repeated uploads.
- Added COS SDK upload timeout and disabled keepalive so worker request threads cannot hang indefinitely on stale upload connections.

## 2026-05-07 actual production duration display

- Changed total duration rendering to ignore failed, queued, validation, download, and other waiting states.
- Added support for future precise active duration fields while preserving current static-page deployment.

## 2026-05-07 screenshot Beijing time display

- Changed screenshot job created and updated timestamps to render in Beijing time.
- Kept total duration calculation on the same parsed timestamp basis and prevented duration/time columns from wrapping vertically.

## 2026-05-07 screenshot duration mojibake fix

- Changed screenshot duration unit rendering to use escaped Unicode sequences so the static page does not show garbled text.

## 2026-05-07 screenshot retry preserves finished outputs

- Changed screenshot job retry to preserve already generated ratios and only regenerate failed or missing ratios.
- Added total duration display to the screenshot job list and detail drawer.

## 2026-05-07 screenshot job actions

- Added screenshot job detail drawer with source/result previews and error log.
- Added screenshot job retry action for completed or failed cover-generation tasks.
- Kept delete action in the same operation cell and disabled retry while a job is still processing.

## 2026-05-07 task list pagination controls

- Added total page display for the task list footer.
- Disabled previous/next pagination buttons when no target page exists.
- Added click guards so disabled pagination controls do not issue no-op requests.

## 2026-05-07 1s cover intro

- Changed the default GPU cover intro duration from 3 seconds to 1 second.

## 2026-05-07 raw episode concat on GPU

- Changed GPU video render to concatenate downloaded episode files directly in episode order.
- Kept 16:9 cover intro generation and insertion at the beginning of concat videos.
- Removed per-episode background/blur/overlay normalization from the GPU render path.
- Kept rolling prefetch downloads and post-COS local cleanup.
- Backup before final intro restore: app.py.bak.restore-cover-intro.20260507143621
