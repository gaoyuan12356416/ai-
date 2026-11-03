# Two original Auto tasks missing their first account snapshot

2026-11-03 maintenance scope: tasks **2658 / 2674** only. Both failed during the
initial account verification (`x_accounts_unavailable`), before selection. Their
account snapshot contains only `account_snapshot_failed=true` and
`language_source=account`; language, material, execution IDs, attempts and unknown
are empty/zero. Original runs are 418 / 419, template 2 versions 9 / 10, accounts
7 / 9. The old template's account scope and body remain authoritative.

## Design and review boundary

The normal account refresh endpoint returns a verified account; it does not
update an existing failed task. `create_task` also refuses to replace its frozen
snapshot on an idempotent call. This narrow utility completes the *missing first*
snapshot using the same `XAutoPostService._account_language` normalization and
`AutoPostExecutor._validate_account` identity/language check. It never replaces a
nonempty frozen language. Empty/missing verified language is explicitly rejected
instead of accepting the language helper's default.

The exact original task row SHA-256 is pinned for each ID. Plan/apply require the
original running parent, frozen template version/body/account membership, enabled
template, exactly `task_created` then `task_account_snapshot_failed`, zero Auto
ledger, no canonical hold, no native run under the original external task key,
and no prior execution evidence. The central projection may only be the original
unclaimed generation-zero `adapter_unavailable` control row. Native availability,
publish approval, identity/language, and account ledger blockers are rechecked.

The two snapshots are completed in one Auto SQLite transaction. Only missing
language, account username/display name/snapshot, status and failure/finish
metadata change. Attempts and unknown are never assigned. Original task/run IDs,
date, template/body, history and empty execution identities are retained. Native
and central databases are read-only. No material is selected or queue created by
the utility. Subsequent normal selection/preflight retains the existing material
ledger and account safeguards.

The 13 separate zero-queue `account_needs_review` failures are excluded: every
original material now has another canonical queue and confirmed Auto ledger.
Their original runs and released reservations are unchanged.

## Operator procedure

Use only the committed source. Complete production review before `apply`.
No service restart is needed for this maintenance script. Do not alter channel,
timer, runtime or capacity settings as part of it.

1. Create a private maintenance directory (0700). Run `capture-accounts` with the
   existing Auto bridge bearer in `X_POST_AUTO_INTERNAL_TOKEN`, never on the
   command line. This action calls only the existing sidecar account verification
   endpoint with the same flags as initial task creation. It may perform the
   standard account token refresh; it does not publish. It saves only the safe
   account DTO, without tokens, headers, URLs or profile images.
2. Run `plan --auto PATH --native PATH --central PATH --accounts ACCOUNTS.json
   --output PLAN.json`. Evidence must be no older than 120 seconds. Review the
   two original row hashes, first-language completion, immutable identities,
   current native account proof and exact running Auto code/PID proof.
3. Run `apply --plan PLAN.json --backup-dir PRIVATE_BACKUPS --actor ACTOR
   --output RESULT.json` within the same 120-second verification window. A changed
   task/run/template/account/runtime or new execution evidence rejects the plan.
   Regenerate verification and plan if stale; do not relax the predicates.
4. Read back two pending tasks, audit rows and original attempts/unknown. Normal
   executor admission may still wait for another task on the same account. The
   actual Auto cutover at read-only inspection was 2026-11-02T11:17:02.798907Z;
   both original task dates are later, so no backlog authorization is rewritten.

Output files and online SQLite backups are created exclusively with 0600; backup
directory creation uses 0700 and backup `quick_check` must pass. The backup is
created before mutation. Audit rows contain original task/run/template/event and
verification evidence plus actor/backup/runtime/plan hash, and reject update,
delete and `INSERT OR REPLACE`. Reapplying the identical completed plan returns
already-applied without touching tasks, even after normal selection has begun.

Rollback is not a blanket database restore: after normal selection starts it
could discard unrelated progress. Before any attempt to reverse admission,
inspect the original task's new execution/material/receipt state. Never clear a
claim, unknown, attempt or newly created queue as rollback.

## Local verification

Passed 2026-11-03:

```text
python -m unittest scripts.test_x_auto_missing_snapshots scripts.test_x_auto_post_service scripts.test_x_auto_post_publisher scripts.test_x_auto_post_x_sidecar
54 tests, OK
python -m py_compile scripts/recover_x_auto_missing_snapshots.py scripts/test_x_auto_missing_snapshots.py
git diff --check
```

The 10 new tests use real Auto/native/central SQLite databases. They cover normal
selection eligibility after completion, no network during plan/apply, unchanged
other databases/run/frozen task fields and attempts, backup integrity, idempotent
reentry, fresh/eligible/explicit-language account evidence, immutable-language and
claim/attempt/unknown drift, native execution/material ledger/hold/canceled parent,
central execution or account-approval races, exact plan scope and snapshot CAS,
runtime change, and append-only audit protection. Capture is separately mocked to
prove only standard verify calls and safe DTO serialization. No production apply
was performed during development.
