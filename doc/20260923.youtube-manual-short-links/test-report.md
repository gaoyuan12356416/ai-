# Test report

Local Python: 531 tests run, 529 passed, 2 pre-existing platform/fixture skips. Includes 17 manual-link cases, 24 HTTP/auth cases and 2 deployment/rollback cases. JS syntax and git diff --check passed.

Browser mocks: manual short links 22, channel templates 20, schedule 38, loading race 10 checks passed (90 total). Desktop and mobile screenshots inspected under output/playwright. The first browser run had stale CLI callback output; a fresh session rerun completed cleanly. No real platform or production data writes.

Live 63350 replica: exact content ID 1.98s, English title prefix 5.41s (23 dramas), Traditional Chinese title prefix 1.90s (1 drama), exact content/language resolve succeeded. Queries use existing indexed columns and bounded timeouts. The initial Chinese command input used the wrong console encoding; rerun with explicit UTF-8/unicode escapes confirmed the actual behavior.

Three identified issues were fixed and verified; see bugs/. Release is deployed and verified. Linux: 530 passed, one historical fixture skip. Real ordinary-user API search and completed channel readback passed; public assets match. Exact release, preserved data and rollback evidence are in deployment-evidence.md.
