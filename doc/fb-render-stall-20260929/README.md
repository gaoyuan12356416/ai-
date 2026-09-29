# FB random-overlay render progress recovery

A stalled multi-input render previously occupied a preparation slot until the
9,000-second timeout, delaying unrelated scheduled tasks beyond their grace
period. The GPU worker now kills and reaps an ffmpeg child after 180 seconds
without output growth. Healthy growing renders retain the existing total limit.

After a proven stall, normalize the original video to a constant frame rate and
audio timeline, preserving duration, initial geometry and sample aspect ratio.
Retry composition once with the original frozen random-overlay recipe. Verify
normalized duration before composition. A second stall fails safely; it cannot
upload a partial result. Original source identity and recipes remain durable,
and temporary normalized media is removed on success or failure.

Validation: progress watchdog process tests and preparation/source-overlay/
capacity regression tests. Production acceptance must additionally render a
previously stalled full video and check its resulting duration and media profile.

Deploy only `features/fb_gpu/prepare_worker.py` and `render_process.py` over the
verified current GPU release. Pause CPU preparation dispatch, drain GPU jobs,
back up current code, fetch this GitHub commit and switch the GPU code release.
Restart only the FB prepare-only GPU service; resume CPU dispatch and verify
health, prepared media, and natural publishing ledgers. Rollback switches the
GPU code symlink to the recorded old release and restarts that service. Preserve
current jobs, frozen recipes, databases, tokens and publish ledgers.
