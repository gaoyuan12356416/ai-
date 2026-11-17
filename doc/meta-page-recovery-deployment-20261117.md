# Meta Page recovery and portrait-memory acceptance — 2026-11-17

CPU: `43.166.187.96`. Repository: `gaoyuan12356416/ai-`, branch
`codex/meta-page-recovery-20261117`. Contract: [meta-page-recovery.md](meta-page-recovery.md).

The runtime and hourly/batch-broadcast configuration below are historical. The later user
change to four-hour execution and daily 10:00 reporting is recorded in
[meta-page-recovery-schedule-20261117.md](meta-page-recovery-schedule-20261117.md).

## Current release and memory result

- Installed pointer `/opt/meta-page-recovery/current`:
  `c752f6414e5c7ec420b024ab639857666f1743ad`.
- The recovery worker remains at `377d8e18b88d1a6d225aad083b18d855726843d0`;
  every recovery-module source hash matches the installed release. Portrait-only
  changes did not restart the unchanged worker.
- **88 tests passed** on CPU; the same related cases were validated locally.
  Receipt: `/mnt/data-disk/meta-page-recovery/verification/tests-c752f641.log`.
- The original 3-GiB failure was a cgroup OOM at 14:19:15 CST: kernel anonymous
  RSS reached 3,138,060 KiB. Retaining every Page's encoded JSON simultaneously
  caused the excess. The prior published snapshot stayed intact.
- Profile JSON now encodes one Page at a time; only small hashes remain for the
  full scope. A record iterator feeds the deployed PyMySQL driver's bounded SQL
  batches in one transaction. Actual detail hashes, row count and all cents are
  still verified. Hash reads use 100-Page slices, subdividing on source timeout.
- Full-size isolated check under **3 GiB** completed 3995 Pages with the actual
  SQL encoder and SQLite projector, without external SQL writes. Peak RSS:
  **1,231,320 KiB**, 148.744 seconds. Receipt:
  `verification/memory-canary-3g-061854/acceptance.json` under the recovery root.
- The **regular production refresh** also completed under **3 GiB**, at
  **15:49:57 CST**, peak RSS **1,421,536 KiB (1.356 GiB)**. All 3995 actual SQL
  detail hashes and income totals matched before the immutable serving switch.
- Current verified profile: **`20261117T072633307841Z`**, 3995 Pages,
  UTC window **2026-11-10 through 2026-11-16**; own paid products Dramawave 1479,
  FreeReels 979 and HotDrama 3302. Projection size: 1,820,389,376 bytes.
- Standard refresh budget is **3 GiB / 2 hours**. The expanded cold read plus
  cross-region write exceeded the previous combined time budget. Source SQL Gate
  and per-query execution limits were unchanged. Temporary runtime overlays were
  removed after completion; the normal unit succeeded without a memory increase.
- Production proof: `/mnt/data-disk/fb-page-portraits/20261117T072633307841Z.receipt.json`;
  aggregate acceptance: `/mnt/data-disk/meta-page-recovery/verification/final-acceptance.json`.

## Committed-data protection and upstream limit

The interrupted version `20261117T061854638718Z` had all 3995 rows committed.
Rebuilding later produced different hashes, so the existing-version guard refused
an overwrite. Its rows remain preserved. The regular owner subsequently published
its newer, fully verified version. No old SQLite was restored over current data.
`features.fb_page_portrait.recover_committed` is a read-only SQL recovery option
that projects exact committed payloads, checks original-input cents and refuses
to replace a newer published version. Its attempted older recovery respected the
active owner's lock; no process was killed and no lock was cleared.

The social warehouse has an **existing upstream refresh timeout**, with last
source `20261117T020500557904Z`, generated 10:15:02 CST. This collection reused the
same immutable social input validated at **13:01:02 CST**, while it was fresh.
Hash, scope, coverage and validation time are frozen. Every new collection retains
the original three-hour source freshness requirement; an unavailable/stale source
preserves the last verified portrait. Paid/source errors are never fabricated as
zero. This upstream issue is separate from the repaired memory failure.

## Hourly executor and actual Ad acceptance

- `meta-page-recovery-scan.timer`: enabled/active, every Beijing hour at **:05**.
  `meta-page-recovery-worker.service`: enabled/running. State:
  `/mnt/data-disk/meta-page-recovery/state.sqlite3`.
- Baseline **13:12:35 CST**, run `7e543de5f4aa41ac9c3d0c530eb84fd0`:
  pool 24 2128 Pages / 520 already abnormal; pool 25 326 / 46;
  pool 57 884 / 263. Total **3338 Pages / 829 pre-existing abnormal**.
  Pre-existing abnormal Pages did not create recovery jobs.
- The 14:05 scheduled batch completed successfully with no new abnormal Pages.
- At 15:05, one new abnormal DramaWave Page produced 222 discovered Ads:
  1 changed, 172 waiting (170 no purchase data, 2 Meta rate limits), 49 skipped
  (36 exact purchase=0, 13 inactive/paused parents or Ad). No unknown writes or
  failed jobs. These are frozen batch outcomes, not current Meta review status.
- Actual Ad **120250500369710760** retained its ID. Page
  **477068722157920 -> 1397107673489601**, Creative
  **1702350191592028 -> 1004887652692221**. A later read confirmed **ACTIVE**,
  correct Page/Creative and no issues. Exact purchase remained readable, **2 -> 3**
  over the same account-date window; the increase is not claimed as attributable
  solely to the new Page. `learning_stage_info` returned `last_sig_edit_ts` but
  no learning status, so this is not a guarantee against relearning.
- Ad proof: `verification/live-ad-readback-1505.json`. Original content, tracking,
  CTA and source/account identities are checked before updating only Ad.creative.
  Permission rejection and missing candidates affect only that credential/Page
  or Ad. The HotDrama acceptance sample had no authorized alternative and was
  unchanged; this is not a global HotDrama account restriction.
- Missing Page status is explicitly reported, not treated as recovery or zero.
  Resolved pool conflicts can resume their own incident. No platform permission
  or real restriction is bypassed.

## Test-group notifications and managed Pages

Recipient: **`oc_b633b6fba2c78f43ef1ebacd314d518c`**.

- Marked test: `om_x100b60893152b0a4dd21cf3743f637f`.
- Baseline: `om_x100b608a6a8890a8df8c17ef6eb553a`.
- 14:05: `om_x100b608aa7400ca4c0e801c9ec52b0b`.
- 15:05: `om_x100b608b9b82d4b0ddb28616bda01fc`.
- Own-message readbacks confirmed the exact group. One frozen summary follows
  each completed batch, including zero-new/failed scans. Billing/in-process is
  separate from review/deliverable. Uncertain sends are held for readback.
- Imported **2217** normal unconfigured Pages: pool 24 +1596, pool 57 +621,
  pool 25 +0. Managed scope **946 -> 3163**; all old configurations preserved.
  Three manually deleted Pages stayed deleted. No publishing task was created.
- Import receipt/online backup: `imports/20261117-pages.json` and
  `imports/20261117-pages.before.sqlite3` beneath the recovery data root.
- Portrait scope is the union of managed Pages and current three-pool members:
  3995. Main API, portrait reader and organic publishers were not restarted.

## Operation and rollback

```bash
cd /opt/meta-page-recovery/current
python3 -m features.meta_page_recovery status
systemctl status meta-page-recovery-worker.service meta-page-recovery-scan.timer
```

Latest frozen package rollback (drains current Ads and withdraws the recovery
executor/timer; prior package still contains the streaming memory fix):

```bash
python3 /mnt/data-disk/meta-page-recovery/deployments/20261117-c752f641/deploy.py rollback --deployment /mnt/data-disk/meta-page-recovery/deployments/20261117-c752f641
```

For complete feature-code withdrawal through earlier inactive installation
backups, use that frozen tool against suffixes, in this order:
`c752f641`, `6529a8af`, `c09d330b`, `fdaa7cc4`, `377d8e18`, `fe8fc360`,
`69e00177`, `ba6ec90f`, `0045c79b`, `349ab406`, `1c5edccc`, `a6d174e8`,
`97f9c92f`, `1dcc10f5`, `598be911`, `73bbb1db`. Later release drift stops rollback
for reconciliation. Restore no business database, token, receipt or already
confirmed Ad change. Keep imported configuration and published portraits.

The separate capacity receipts are `deployments/20261117-portrait-time-budget`
(3 GiB/2 hours) and `deployments/20261117-portrait-capacity` (initial 3 GiB/75 min).
Their frozen `portrait_capacity.py rollback --receipt RECEIPT` drains only the
portrait owner; reverse newest first if explicitly restoring old capacity. The
latest memory fix can remain when merely disabling the new Ad executor. Do not
return Ads to a blocked Page or restore an old whole SQLite over newer facts.
