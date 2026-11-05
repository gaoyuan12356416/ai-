# X pool ordering and account verification isolation

The material candidate reader excluded active operator-manual material
reservations, but the atomic schedule FIFO replay did not. A newer reserved
row could therefore reject a valid older candidate and fail a whole batch.
Both reads now exclude exactly the same active reservations. The reservation,
automatic material deduplication, current snapshot boundary and queue identity
remain intact. An explicitly selected reserved material is still rejected.

A confirmed X `400 invalid_request` during account verification was previously
flattened into the shared `x_upstream_error` path and failed the whole schedule.
Verification now records a distinct account-local rejection, including whether
the failure was during token refresh or profile lookup. Only that account is
omitted from the current plan. Its existing authorization is not relabeled as
revoked. The existing last-error field carries the visible exclusion; the next
successful authenticated verification clears it. The verify endpoint ignores
only this derived planning exclusion, while higher-priority publication and
unknown-outcome holds still apply. Capacity calculation uses the same exclusion,
so omission cannot produce another FIFO capacity conflict.

The historical API error retained only HTTP status and `invalid_request`, not
the provider's detailed body or substage. Do not claim an expired token, revoked
grant, or application outage was proved by that historical message. New stage
diagnostics intentionally omit request bodies, tokens and raw provider details.

## Scope review

- Candidate-local hydration/media failures already skip that candidate.
- Explicit authorization/approval/identity failures already skip the account.
- Confirmed verification 400 now has the same account-local scope, with an
  observable state and a successful-verification release condition.
- Active manual occupancy must exclude only the material, at selection and
  reservation; it must not reject unrelated available material.
- Shared rate limits, unknown external writes, frozen identities, language
  rules, durable queue occupancy and storage/transaction failures retain their
  existing protection. No failed/unknown publication is replayed by this fix.

## Runtime composition

The source base is the verified deployed recovery branch `a2bf0c50`. The OAuth
module preserves the independently deployed account-list overlay `c25b060a`;
the daily runner preserves the deployed shared 200-account limit. Only four
runtime modules are installed. Main-API copies of the store/blocker are synced
with a strict two-line store patch that preserves unrelated main overlays.
The changed plan-writing and account-verification methods are owned by the X
sidecar; the main API is not restarted for these dormant shared-code changes.

## Validation

The two store regressions prove a held newest material does not block two
healthy schedule slots, repeated plan reads do not create duplicate queues,
and a reserved candidate still cannot be taken. Runner regressions cover both
material and drama schedules with one rejected account. The real HTTP handler
test proves both verification substages preserve authorization/token files on
explicit rejection, safely redact provider details, and can recover without
operator unlock. Existing schedule/store and publication-protection tests are
also run: all 241 schedule/store cases passed, as did 76 of 79 account cases.
The three account failures assert the superseded owner-transfer rule; all three
also fail against the byte-verified deployed OAuth baseline. One older priority
test likewise expects already-reserved drama episodes to be selectable; that
mismatch also reproduces against the deployed store. These four pre-existing
test expectations were not used to change the live business rules.

## Deployment and rollback

Fetch this exact GitHub commit into a clean isolated checkout on the CPU host.
Run `scripts/deploy_x_pool_local_failures.py --phase prepare`, then `--phase
drain --folder BACKUP`. Drain pauses only new X dispatch claims and X planner
triggers, lets active jobs finish, and leaves other channels and execution
nodes running. `--phase switch --folder BACKUP` returns `waiting` until those
resources finish; it never cancels them. The switch backs up both SQLite
ledgers and credentials, checks concurrent code drift, switches the scoped X
release, restarts the X sidecar and its dependent Auto service, then restores
X admission and trigger states. It never restores database/token snapshots.

Abort a pre-switch drain with `--phase abort --folder BACKUP`. To roll back a
completed deployment, run `--phase rollback --folder BACKUP`, wait for the same
X-only drain, and run `--phase switch --folder BACKUP`. This restores code only;
all current queues, receipts, reservations and credential rotations survive.
