# Intro cover compatibility repair

The cover producer can return a valid JPEG without APP0/JFIF (including FFmpeg
JPEGs). The intro renderer previously rejected it before FFmpeg started. Retries
reused that same cover and failed again, while RuntimeError became gpu_render_failed.

Every intro now decodes its private frozen cover and converts it to RGB sRGB JFIF
before the existing strict color contract and BT.709 video conversion. This boundary
covers new and historical covers, regardless of filename. RGB/gray web images
without profiles use sRGB; embedded ICC profiles are converted with LittleCMS.
EXIF orientation is applied and transparency uses a white background. Original
cover bytes, public objects, episode checkpoints, recipe and completed videos stay
unchanged. Corrupt, truncated, animated, oversized, unprofiled CMYK and invalid ICC
inputs fail with explicit safe error codes propagated through CPU remote-client DTOs.

Validation: scripts/test_drama_intro_cover.py; scripts/test_drama_synthesis_media_pipeline.py;
scripts/test_drama_synthesis_remote_client.py. New fixtures exercise missing JFIF,
JPEG/PNG/WebP, grayscale, ICC, alpha, EXIF, corruption, limits, source changes,
private-file cleanup and repeat normalization. Existing intro command tests still
verify pixel conversion and BT.709 output tags.

Deployment: baseline f6bef3fb8e68a51663d2b2ba24394fc0640df28c. Push first, fetch exact
commit on both hosts. Verify no active drama execution, back up CPU SQLite and
changed files, and record HK current symlink. Copy only app.py,
features/drama_synthesis/async_runtime.py and features/drama_synthesis/intro_cover.py
to CPU after baseline hash checks. Switch HK current to exact release. Restart only
drama GPU worker, CPU API and drama job worker. Run the same tests on Linux and
normalize the failed job's cover into a scratch directory before switching.

Recovery: invoke the existing retry API for the single failed job, preserving the
job ID, frozen recipe and verified downloaded episodes. Verify it passes intro,
finishes random-template render and publishes the selected artifacts to COS.

Rollback: drain active execution first; restore CPU app.py/async_runtime.py from
the deployment backup, remove the new module only if absent in baseline, restore
HK current to the baseline release and restart the same units. Keep the current
SQLite, outputs and execution generations; never restore old task state.
