# Coordinator control tools

## Split screenshot/cover and temporary US ad lane

The legacy `materials` group remains available only for read-only inventory.
`source_fence.py materials --apply` always fails before inspecting or mutating
services. The reviewed scopes are `materials-images` (the six US image services
plus the old shared and burst tunnels) and `materials-ad` (US ad generation,
vision, and `gpu-ad-only-reverse-tunnel.service`).

The install-only template is
`control/units/gpu-ad-only-reverse-tunnel.service.in`. It has exactly two
loopback reverse forwards, 18796->8796 and 18797->8797. It uses fixed
`/etc/gpu-ad-only-tunnel/known_hosts`, `StrictHostKeyChecking=yes`,
`UpdateHostKeys=no`, and `ExitOnForwardFailure=yes`. Do not start it while the
old shared tunnel owns either CPU port. Back up prior unit states, validate the
rendered unit with `systemd-analyze verify`, and leave it stopped until the
approved handoff.

For `materials-images --apply`, the fresh checkpoint retains the common
`run_id`, `group`, `new_admission_closed`, `triggers_paused`, `cpu_drained`, and
`no_unknown` fields, and adds these exact non-secret facts:

```json
{
  "coordinator_host": "VM-0-108-centos",
  "ready": true,
  "split_mode": "us-ad-only",
  "ad_requests_drained": true,
  "legacy_shared_tunnel_stopped": true,
  "legacy_burst_tunnel_stopped": true,
  "cpu_image_ports_owned_by_local_units": true,
  "cpu_ad_ports_owned_by_us_ad_only_tunnel": true,
  "ad_services_healthy": true,
  "us_ad_baseline": "COPY THE EXACT OBJECT FROM THE CURRENT US DRY RUN"
}
```

Run the US dry run after the ad-only tunnel is stable. Its `us_ad_baseline`
contains unit/process identity and hashes, never credentials. The checkpoint
must carry that object unchanged. Both legacy tunnels must already be inactive
with zero MainPID/ControlPID. During every image mask and at completion, the
fence rechecks that the two US ad services and ad-only tunnel retain the same
PID start identity, cgroup, restart count, timestamps, unit hashes, and active
state. A change aborts without restarting an old image unit or tunnel.

For the later `materials-ad` fence, use `split_mode=hk-ad` and prove
`ad_only_tunnel_stopped`, `cpu_ad_ports_owned_by_hk_tunnel`,
`hk_ad_target_ready`, and `ad_requests_drained` are true. The US ad-only tunnel
must already be inactive with zero MainPID/ControlPID. Never operate the US
ad-only and HK ad tunnels at the same time.

The `drama` source fence is intentionally separate from `materials`. It owns
only `drama-material-api.service` on the US GPU host. The shared
`gpu-worker-reverse-tunnel.service` remains in `materials` and must not be
stopped, disabled, restarted, masked, or rewritten by the drama handoff.

## Drama drain checkpoint

After the existing `materials` public-write gate and pause record are active,
create a unique checkpoint on the CPU data disk:

```sh
python3 control/drama_drain.py \
  --output /mnt/data-disk/migrations/gpu-service-migration-20260828T1502/control/drama-drain-UTC.json
```

The command is read-only except for its private `0600` evidence file. It runs
two complete verification passes and requires their gate, pause, configuration,
process, socket, and database snapshots to be identical before setting
`checked_at` and `ready`. Each pass checks the exact nginx gate, the unrecovered
pause journal, both stopped test services, the exact paused screenshot cron,
the running CPU API environment and both configuration files, one
`GET 127.0.0.1:18788/healthz`, all TCP connections to the legacy `18787`
endpoint, and read-only drama job/lease counts. After the health connection is
closed, an established `18788` connection is treated as an in-flight business
request and blocks the checkpoint. Tokens are only compared in memory and are
never printed or hashed into evidence. The two health GETs are the only HTTP
requests; no job, render, publish, retry, or callback route is called.

Process drain proof reads only
`/sys/fs/cgroup/systemd/system.slice/drama-material-api.service/cgroup.procs`
and descendants of the API MainPID. Any API child, including FFmpeg, FFprobe,
Codex, or another process, blocks readiness. It deliberately does not scan all
host processes, so unrelated TikTok media work is not misclassified as drama.

## US drama history archive

The retired US history is copied locally from the two fixed source directories
into the US `/data` volume. Run the read-only inspection first, then perform the
copy only while the CPU `materials` gate and pause record remain active:

```sh
python3 control/drama_history.py
python3 control/drama_history.py --apply
python3 control/drama_history.py --verify
```

The sources are fixed to `/root/drama_material_jobs` and
`/usr/share/nginx/html/drama-materials`; the destination is fixed to
`/data/migrations/gpu-service-migration-20260828T1502/drama-history/archive`.
Source traversal and copying are anchored to no-follow directory file
descriptors and reject symlinks, special files, root replacement, and nested
mount boundaries. The command neither changes the source trees nor operates a
service. It conservatively requires enough space for the payload plus at least
30 GiB free on `/data`.

The atomic rename is the publication point. A successful `--apply` still marks
the archive as requiring a current read-only `--verify`; do not fence the old
API until that verification proves the published payload, manifest, receipt,
and still-live sources identical. If an error says `archive_published=true`, do
not run `--apply` again: retain the post-commit evidence and run the current
exact release with `--verify`. A pre-publication failure keeps its private
staging evidence and never overwrites an existing archive.

## US drama fence

Transfer the fresh checkpoint through the approved private migration path, then
use the exact pushed operations release on `VM-0-13-centos`:

```sh
python3 control/source_fence.py drama --checkpoint PRIVATE_CHECKPOINT
python3 control/source_fence.py drama --checkpoint PRIVATE_CHECKPOINT --apply
```

Before the first mutation, the controller verifies a single-threaded old API,
no child process, no established `8787` request, and a stable active shared
tunnel identity. It records the real FragmentPath and every DropInPath,
including file/symlink type, symlink target, and content hashes, then archives
their content under the US data disk. The `/data` path and each parent through
the evidence directory must not be symlinks, and `findmnt -T` must still resolve
the final directory to `/data`. The controller then stops, disables, retires,
and masks only the old API. Success requires `inactive/dead/masked`, MainPID and
ControlPID zero, an empty ControlGroup, no `8787` listener or process, and an
exact match of the shared tunnel PID, start timestamps, restart count, unit
hash, and active state. Evidence is retained in
`source-fence/drama-before.json` and `source-fence/drama-after.json`.

If mutation fails, the controller makes best-effort old-API-only stop and
disable attempts. When the verified unit backup exists, it also continues the
old API retirement and persistent mask so a future shared-tunnel dependency
cannot pull the API back. Its private failure receipt records the original
stage, exception type, command return codes, final service state, and whether
closure was actually proved. Only `final_closed: true` proves the old API is
closed. If the final `inactive/dead/masked`, zero-PID, empty-ControlGroup state
cannot be proved, the command raises an explicit `HIGH RISK` error; operators
must treat the maintenance window as unresolved. The shared tunnel is never a
systemctl command target. Inspect the partial state and obtain another fresh CPU
checkpoint before using `--resume`; never restore the old source while the HK
endpoint is active.

The existing cleanup cron is deliberately retained in this migration round.
Historical manifests and archives are handled separately; these drama control
tools do not modify cleanup behavior.

## Exact drama `0d2dc5e` release operator

`drama_release.py` is the reviewed code-only release operator for
`a1519413b23d20acab035853b0f5aeebee53e9ac` to
`0d2dc5ee90d056a58231fd0292186d73b0d083f8`. It defaults to a read-only dry
run. It never creates, retries, resumes or publishes a business job.

The CPU scope is exactly `drama-material-job-worker.service` and
`drama-material-api.service`. The worker must already be fully stopped, the API
must be a drained single-process service, both approved failed jobs must remain
`failed`, and the job/lease tables must have zero active rows. Apply backs up
and atomically exchanges only these two files, then keeps the worker stopped:

```text
/root/drama_material_service/app.py
/root/drama_material_service/features/drama_synthesis/async_runtime.py
```

The HK scope is exactly `drama-synthesis-gpu-worker.service` and
`drama-synthesis-gpu-tunnel.service`; both must already be stopped. It creates
an immutable release below `/data/drama-synthesis-gpu/releases`, atomically
exchanges `current`, and starts each exact unit at most once. The following
eight services are never systemctl targets and their FragmentPath, file hash,
PID, startticks, NRestarts and cgroup identity must remain identical at every
guard boundary: both FB units, all four TT units, and both X units.

For either host, first run the exact command without `--apply`. Required
arguments are deliberately verbose: run id, hostname, old/new SHA, fixed data
root, current `findmnt` source, fixed source checkout, every exact target and
protected unit. The dry-run prints `required_fragment_arguments`; copy every
`UNIT|ABSOLUTE_FRAGMENT|SHA256` value back as one `--fragment` argument only
after reviewing that fresh output. Apply refuses missing or changed fragment
bindings. The fixed GitHub checkout paths are:

```text
CPU /mnt/data-disk/migrations/gpu-service-migration-20260828T1502/drama-release/source/0d2dc5ee90d056a58231fd0292186d73b0d083f8
HK  /data/migrations/gpu-service-migration-20260828T1502/drama-release/source/0d2dc5ee90d056a58231fd0292186d73b0d083f8
```

The checkout must be clean, have the approved GitHub origin, and have both
`HEAD` and the fetched remote branch at the exact new commit. CPU backups and
receipts stay under `/mnt/data-disk/migrations/$RUN/drama-release/$NEW/cpu`;
HK receipts and the retained rollback symlink stay under
`/data/migrations/$RUN/drama-release/$NEW/hk`. Existing evidence, release,
backup, stage or swap names are never adopted or overwritten.

HK apply is not complete route acceptance by itself. Its result is
`deployed_local_route_pending` after exact local `8787/healthz` and listener
ownership checks. Run the read-only `drama_release.py route` on the CPU host to
prove `127.0.0.1:18788/healthz` and that the unique sshd reverse listener is
associated with the HK `43.154.250.89` SSH peer. Preserve both result hashes in
the coordinator report. No credential value is read or written by this tool.
