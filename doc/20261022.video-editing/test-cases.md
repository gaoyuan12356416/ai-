# Acceptance cases
| Area | Scenarios |
|---|---|
| Auth | Missing Cookie/API token/module rejected; cross-owner writes denied; admins inspect |
| Navigation | Stale config gains group once; disabled choice preserved; user permission assignable |
| Upload | Multi-file/cancel/retry one part/expiry/wrong size or digest/forged ID/missing parts/repeat completion |
| Ambiguity | Init/complete/submit disconnect reconciles durable identity; no false ready/duplicates |
| Privacy | Signed reads work; anonymous denied; CPU JSON only; no local fallback |
| Library | Probe pending/failure, search/rename/soft-delete, frozen references survive |
| Timeline | Delete two intervals, reorder/undo/repeated asset/remove episode/per-drama isolation/autosave conflict |
| Shared settings | Insert/fade application preserves episode trims |
| Render | Two dramas two outputs, contain/cover/cut/fade/short-clip error/mute/no-audio/mixed fps and size |
| Output | Download real MP4, verify profile/duration, inspect boundaries and A/V alignment |
| Recovery | Same operation ID, retry only failed child, CPU loss/GPU restart/stale generation/unknown process |
| Compatibility | Old hash/DTO/cache, shared slot, old prefetch skips new type, mixed-reader rollback |
| Cleanup | COS verified before removal, stopped failed task TTL, receipts retained |
| External | No processing/ingestion request; pending integration shown |

Mocks, real network checks and actual-media validation are reported separately.
