# TT Post clean Direct Post profile

## Outcome

Add an explicit formal-publishing media mode without weakening the existing
branded-preview protection.

## Contract

1. `branded_preview` remains the default GPU media mode. It keeps the
   DramaWave Logo/tutorial outro, uses the existing v2 profiles, and must
   always return `brand_overlay_review_required=true` and
   `direct_post_eligible=false`.
2. `direct_clean` only scales, pads, frame-rate normalizes, and encodes the
   reviewed source. It must not read or add the configured Logo, tutorial
   outro, Drama ID, link prompt, or other promotional overlay.
3. The clean HEVC profile is
   `tt-post-direct-clean-hevc-720x1280-v1`; the H.264 fallback is
   `tt-post-direct-clean-h264-720x1280-v1`. Media mode and profile must match
   exactly.
4. A clean job has a distinct CPU job identity and manifest contract. It
   cannot reuse a branded job. Legacy branded v2 manifests remain reusable in
   branded mode.
5. Only a manifest whose profile, media mode, media probe, storage identity,
   and exact eligibility flags all match the current clean configuration can
   reach formal publish. Editing only `direct_post_eligible` must fail closed.
6. The existing live, Direct Post audit, URL-property, exact-origin, account
   capability, account-setting, consent, queue idempotency, and reconciliation
   gates remain mandatory.
7. Selecting `direct_clean` does not approve a source. The operator must
   separately review the actual source for watermarks, calls to action, rights,
   music, commercial disclosure, and AI-generated-content disclosure.

