# X platform shared account access — 2026-10-23

An active user with the existing `x_accounts` (X 平台推广) permission can publish
to every configured X account and manage its default copy, material pool,
language, publish approval and profile verification. The separate
`x_video_publish` permission no longer grants or restricts this workflow.

The main API rereads the current user from the user database and issues a narrow
`x_platform_access` capability. The sidecar's explicit `platform` account scope
requires that capability. A normal user stays `role=user`; no administrative
identity is fabricated. Browser actor/capability fields are ignored. Private
OAuth authorization/logout and unrelated administrative routes retain their
existing boundaries. Active account, approval, language, media, occupancy,
idempotency and unknown-result checks are unchanged.

Default copy and material pools are shared by account. Existing configuration
and pool rows keep their storage identity, IDs, order, versions and history.
Updates use version checks; ambiguous multiple historical pools/configurations
fail for reconciliation instead of silently picking one. The pool adds three
operator columns; new edits record the actual operator whose live permission is
checked at the scheduled time. Historical pools fall back to their original
initiator. Frozen tasks retain the creator and account identities. Reading task
results does not transfer the pool operator. Drafts, preview tokens and submission
idempotency keys remain operator scoped. Authorized operators can open a shared
pool's linked batch detail; the recent-batch list remains the current user's list.

Publishing mutations also use the existing X audit-event ledger with the real
operator. No tokens, credentials or authorization ownership are changed.

## Validation and deployment

Offline checks cover regular non-admin cross-owner manual/Excel submission,
shared defaults, existing pool preservation and reordering, correct scheduled
operator attribution, fresh permission revocation, private OAuth scope,
same-origin checks, replay protection, and the existing manual runner contracts.
Frontend fixtures exercise the ordinary operator's eight-column account table
with enabled actions for another person's account. No real X API is called.

Deployment uses a fresh hash-frozen live composite overlay. The deployment tool
can stop and restore the existing pool scheduler for this reviewed update. It
preserves the current settings/items, checks all original ledger columns and
token fingerprints, and restores the original publisher trigger states.

Rollback restores the previous release and backed-up external files, never the
database or tokens. The upgrade snapshots the existing personal task count. Rollback accepts those
pre-upgrade tasks only; if new tasks or shared pool operator edits exist, retain
the compatible executor and reconcile instead of restoring an owner-only executor.

Production verification must use reads only. Do not create a real X post,
change actual account settings, enable plans, add materials or create test tasks.

Local result: 319 Python tests and 13 frontend integration cases passed.
Python compilation, shipped JavaScript syntax and overlay compilation passed.

A concurrent live source-pool fix was detected before activation and retained: indexed Dramawave resource/deployment reads and clearing stale UI pool errors after successful reads. Cache tags were advanced to the combined release.
