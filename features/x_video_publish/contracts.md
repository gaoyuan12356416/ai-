# X personal video publishing integration contract

`PersonalStore(db_path)` owns durable state in the existing X Post SQLite database. It calls the existing store migration; no network or token access occurs in this module. All actor arguments are trusted session/Sidecar values `{tenant_key,user_id,name}`. Browser-provided actor/owner data must be ignored by the gateway.

## Core public methods

- `settings(actor)` returns configured accounts for that exact owner.
- `save_settings(actor, account_id, payload, account)` saves default caption (`default_copy`, default `{name}\n{desc}\n{url}`) with optimistic `version` and a trusted current account snapshot. It does not modify authorization, language or publish approval. `delete_settings(actor, account_id)` removes only the personal configuration.
- `get_draft(actor)` / `save_draft(actor, payload)` / `delete_draft(actor)` persist owner-isolated JSON, capped at 512 KiB. Draft input is never trusted as a publish request.
- `preview(actor, payload, accounts, materials, now=None)` validates up to 200 explicit rows and stores an opaque, owner-bound 10-minute confirmation token. `accounts` and `materials` are trusted maps by canonical string ID supplied by the gateway/source service. Returns `{preview_token,expires_at,rows,valid_count,invalid_count}`. Rows contain `account_id`, `material_id`, `caption` (optional; blank uses saved default), `scheduled_at` (blank or second-precision Beijing local or canonical UTC; browser .000Z is normalized to Z), `processing` (`original`/`random`), optional `row_number`, `allow_repeat_success`. Payload `{source:'manual'|'excel'|'pool',rows:[...]}`. Manual rows sharing a material must share time. Preview is metadata validation; worker still performs full media/token/storage checks.
- `commit(actor, preview_token, idempotency_key, accounts=None, materials=None, now=None)` consumes the frozen preview into one batch; when supplied, trusted accounts/materials are revalidated. Only valid rows are accepted. Time and database pair occupancy are always rechecked in the same transaction. Exact repeated token/key returns the original batch even after token expiry; mismatched token/key is a conflict. Returns the batch DTO plus `created`.
- `list_batches(actor, limit=50, offset=0)` / `get_batch(actor,batch_id)` return only this owner's batches and derived task/log status. `operations(actor,batch_id)` aliases the batch's read-only tasks.
- `get_task_for_run(run_id)` is an **internal-only** lookup returning the immutable owner/account/material/language/caption/process contract for the legacy child, or `None` for legacy runs.
- `assert_run_publishable(run_id, account=None)` is internal-only and rejects a held parent or a mismatching current owner/account/language/status/approval when a trusted account is provided.
- Runtime hooks use module helpers `read_personal_task(db_path, run_id)` and `assert_personal_run_publishable(db_path, run_id, account=None)`: both open SQLite in `mode=ro` without constructing a store or running migrations. An unupgraded legacy schema returns no personal task; unreadable or incomplete personal ledgers fail closed. Per-upload credential checks therefore add no schema writes to legacy publishing even while the personal feature is disabled.

IDs are strings in user DTOs; internal authorized account primary IDs are positive integers. Do not pass an X platform snowflake as an internal account ID without resolving the exact owner account first.

## Tables and reservation bridge

Tables are `x_video_publish_account_config`, `x_video_publish_draft`, `x_video_publish_preview`, `x_video_publish_batch`, `x_video_publish_task`. `x_video_publish_task.manual_run_id` is unique and immutable. Batch creation atomically creates one single-pair `x_post_manual_run` per valid row and one existing `x_post_manual_material_reservation` per distinct material, attached to the first (anchor) child for that material. Existing automation therefore immediately respects the new reservation.

Only immutable same-batch/same-material siblings may share that anchor. Reservation settlement waits until all siblings either have frozen queues or are terminal with zero queues. Any queue means `consumed`; all-zero failures mean `released`. Unknown outcomes hold the entire parent and leave unprepared children reserved. No history is deleted or replayed.

## Required Sidecar / runner hooks (root-owned files)

1. Both before `create_manual_plan` and before the `publish_queue_request` write, lookup personal task by run ID, re-read the exact account via `find_scoped_account_row(id, trusted_owner, 'mine')`, convert with `row_to_item`, and call `assert_run_publishable`. Do this again inside the credential lock before writing if account ownership could have changed.
2. In personal `create_manual_plan` pass the trusted frozen `account_drama_language` explicitly, which activates the existing queue language marker. Never let payload account/language override task identity.
3. `scripts/x_post_manual_runner.py` preserves `copy_contract_version` from the run, passes it into each candidate and `_preflight_candidates`. `scripts/x_post_daily_runner.py` passes `copy_contract_version=item.get('copy_contract_version',1)` to `build_post_text` at copy preflight. Default 1 preserves legacy behavior.
4. Service's shared queue publisher renders version 2 personal captions, honors final log short URLs, and keeps all existing account/unknown/media/random guards.
5. Pool scheduler must create via preview/commit, using `source='pool'`; it never writes legacy children itself. A submitted task remains independent of subsequent pause/config edits. Unknown task outcomes must pause the pool.

## Copy contract

Version 1 is unchanged. Version 2 allows free text and optional `{name}`, `{desc}`, `{url}`; unknown or malformed brace macros are rejected. No mandatory macros and no implicit truncation. Final rendered copy must be nonempty and within 280 weighted characters, using the existing character weighting and 23-character HTTP(S) URL weights. `{url}` always expands to the immutable publish-log short wrapper, not a browser URL.

Personal schedules preserve whole seconds. UTC inputs with one to three zero fractional digits (including JavaScript `.000Z`) are accepted and normalized; nonzero milliseconds are rejected rather than rounded earlier. Legacy version-1 manual runs remain minute precision.
