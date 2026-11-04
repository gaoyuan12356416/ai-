# Test report

2026-11-04 Windows Python: python -m unittest tests.test_fb_skipped_recovery tests.test_fb_page_task_isolation -q: 29 passed. python -m unittest scripts.test_pipeline_fb_task_isolation -q: 20 passed. Changed modules/CLIs compile; git diff --check passed. Direct execution of the pipeline test script initially hit its existing import-path issue; module invocation passed.

Covers idempotent same-task recovery, original paused row byte-for-byte preservation, no attempt/ledger replay, all-time published exclusion, unknown exact-material fence, finite drama cooldown, recovery late-window lifecycle, concurrent planner reservations and narrow central observation rearm. Production preview, Linux tests, deployment and readback pending.
