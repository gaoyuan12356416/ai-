# TT Post clean Direct Post test report

Date: 2026-07-31

## Automated checks

- Full TT regression: 251/251 passed (core 56, service 87, prepare
  runner 14, pool UI 25, app contract 12, GPU worker 57).
- Clean prepare performs one source-only encode, has no Logo/outro input, and
  records the independent clean profile and media mode.
- Changing branded assets does not invalidate or alter a clean job.
- Legacy branded v2 prepare and reuse remain unchanged.
- A branded manifest stays blocked, and changing only its eligibility flag is
  rejected as an invalid prepared-media contract.
- Formal publish, unknown-init, reconciliation, credential-expiry, storage
  origin binding, and terminal-media cleanup tests run with the clean profile.
- One test executes a real clean prepare manifest directly through the formal
  Fake TikTok publish path, proving the two contracts join without a seeded
  eligibility bypass.

Production evidence is recorded separately during deployment; this document
does not by itself claim that TikTok accepted or made a post public.
