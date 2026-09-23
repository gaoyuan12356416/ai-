# Acceptance cases

Backend: real SQLite and immutable files; eight-field encoding including non-ASCII; duplicate/concurrent operation; explicit new UUID; failure before reservation; write failure and retry; file-created/response-lost retry; failed readback returns no URL; old links unchanged; channel identity changes; owner/tenant/admin isolation; invalid input; metadata name/case conflicts; SQL bounds and literal wildcards.

HTTP: anonymous/token-only/module/nav rejection; same-origin, JSON and size checks; exact route matching; safe errors and actor projection; search pagination.

Browser: open independent of material config; blocked channels; required selections; language/ID display; polling preserves DOM; in-flight double clicks; close/late success; copy/fallback; explicit next link; changed selection; unknown outcome/query/retry; failed search; pages; mobile; Escape; existing settings/new publish. All browser APIs are isolated mocks.

Release: live app transform retains unrelated changes, drift blocks apply/rollback, rollback preserves database and generated links. Public assets must match GitHub bytes; only API restarts; existing workers remain unchanged.
