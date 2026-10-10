# FB Page daily frequency and random scheduling repair

Template 1, DramaWave group 62, was saved as v6 on 2026-10-09 with 144
Pages capped at two automatic posts per day and one held at zero. Its schedule
still contained five fixed candidate windows. The Page policy selected two of
those windows; the template list displayed all five without explaining the cap.
It also omitted random scheduling details whenever Page limits were configured.

The list now emphasizes each active Page's daily cap, then separately describes
candidate times, held Pages and aggregate capacity. The editor clears fixed-only
staggering when switching to random mode, preserving the prior local value when
switching back. The four HTML/JS files are deployed to the public Nginx directory,
main-runtime static directory and the FB runtime static directory. A narrow
backend addition gives the disable API an optional transaction-level drain
guard; ordinary enable/disable requests retain their existing behavior.

## Reviewed configuration change

- Exact source: enabled template 1 v6, config SHA-256
  `6203dea56c0f776340cf8617d1584120c6a6afa8eac004382c5dcf9f821492ca`.
- Replace schedule with `mode=random`, `daily_count=2`, `start=09:15`,
  `end=21:55`; set fixed-only `stagger_minutes=0`.
- Preserve all Page limits (144 at two, one at zero), default zero, source,
  material and drama rules, cooldowns, feedback settings and message template.
- Date/time basis: Asia/Shanghai. Existing random scheduling persists two times
  per date/version with at least 60 minutes between them. Repeated reads do not
  redraw. The nominal configured capacity remains 288 posts/day; authorization,
  unknown outcomes, materials and cooldowns can reduce actual delivery.
- This is automatic-template scheduling. An operator's separately requested
  manual publication remains governed by the existing manual workflow.

At the initial readback on 2026-10-10, 130 Pages had two executable or already
submitted tasks per date, with no Page above two. Today had 53 published tasks,
two unknown outcomes, and 205 prepared tasks remaining. These are a snapshot,
not a guarantee of eventual publication.

## Cutover timing and safety

Saving a new version immediately cancels old planned/ready tasks, while new
calendar activation begins the next full Beijing day. Therefore the reviewed
one-shot runs after today's final 21:55 possible publication plus the 30-minute
late allowance: first attempt 2026-10-10 22:30, then every five minutes. Fresh
operations may start only before 23:10; a persisted partial operation can recover
until 23:50. The timer's last retry is 23:45. These are independent code guards
on that exact date; it cannot silently execute on a later date.

`scripts/fb_auto_post_frequency_cutover.py --preflight` reads production API/DB
state, validates the exact candidate, and rehearses random scheduling in an
isolated SQLite database. It does not save production templates or schedules.
`--apply` rechecks version/hash and queue state, backs up SQLite on the mounted
data disk, and uses the supported disable/save/enable API. It refuses unfinished
today/manual tasks and preparing/running work. The disable API checks the same
boundary inside its write transaction, so concurrent enqueue/claim operations
cannot invalidate the prior read-only check. The durable operation receipt
reconciles uncertain API outcomes before advancing. Published/submitted/unknown
tasks, attempt records and ledger identities are preserved; only future unsent
old-version tasks are invalidated by the normal version change.

The one-shot script is installed from the verified GitHub commit at
`/mnt/data-disk/fb-auto-post-deploy/frequency-random-20261010/code/`, with
`PYTHONPATH=/opt/fb-auto-post/current`. Its private evidence and receipt remain
under `/mnt/data-disk/fb-auto-post-deploy/frequency-random-20261010/`.

## Verification and rollback

Run the focused Node UI test and Python cutover tests, plus the existing strategy,
validation, store and V2 tests. Check public asset hashes and cache versions,
the healthy FB sidecar after its targeted restart, all publishing timers and the
cutover timer's next trigger. An installed timer is pending execution, not proof
that v7 is active.

Before the one-shot executes, cancellation is:

```sh
systemctl disable --now fb-frequency-random-20261010.timer
```

Do not interrupt an active cutover between management API steps. First inspect
its receipt and read back the current template. Restore UI files only from the
deployment manifest's matching backups. After a successful config cutover,
rollback by saving the prior config as a new version through the management API,
after checking current work. Never restore the old SQLite over publication facts.
Keep the new frontend: it supports both old fixed and new random schedules.
