# Coordinator control tools

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
