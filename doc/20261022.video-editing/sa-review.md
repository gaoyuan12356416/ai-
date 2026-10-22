# Design review
Basis: independent COS/library and render/runtime source reviews plus confirmed operator choices.
| Issue | Priority | Required resolution |
|---|---|---|
| Old canonical payload drops trims | P1 | New type, versioned recipe/fingerprint |
| Separate GPU process bypasses concurrency | P1 | One runtime, dispatcher and slot |
| Old binary cannot read new records | P1 | Mixed reader retained; rollback disables intake |
| Newest CPU branch lacks GPU enhancements | P1 | Pin live GPU baseline; scoped overlay |
| Existing uploader permits local fallback | P1 | Dedicated COS-only private verified upload |
| Network ambiguity duplicates writes | P1 | Durable operation IDs and reconciliation |
| Shared library expands project write scope | P1 | Independent object ownership |
| Crossfade changes timing | P2 | Explicit overlap/frame rounding/short-clip checks |

All resolutions are acceptance requirements. This design review does not imply production validation.
