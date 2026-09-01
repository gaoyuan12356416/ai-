# Screenshot/cover split cutover runbook

This runbook moves only screenshot and cover capacity to CPU while ad
generation and vision remain on the US GPU through a temporary ad-only tunnel.
The public materials gate and trigger pause remain active throughout. Do not
use this sequence for the later HK ad cutover.

## Preconditions and dedicated SSH identity

Use one newly generated ED25519 key for this temporary lane. Generate it on the
US host directly below `/etc/gpu-ad-only-tunnel`; never copy an existing US,
HK, X or TT private key. The final nodes must be root-owned, non-symlinks, and:

| Node | Mode | Source |
|---|---:|---|
| `/etc/gpu-ad-only-tunnel` | `0700` | Newly created on US |
| `id_ed25519_cpu_tunnel` | `0400` | Newly generated on US; never leaves US |
| `id_ed25519_cpu_tunnel.pub` | `0644` | Public half; this is the only key material transferred to CPU |
| `known_hosts` | `0644` | Built from CPU's ED25519 host public key obtained over the already pinned admin connection |
| `/etc/systemd/system/gpu-ad-only-reverse-tunnel.service` | `0644` | Exact GitHub template from the selected clean commit |

Do not trust `ssh-keyscan` by itself. Read the CPU host public key through the
existing strict admin SSH session, compare its fingerprint with the operator
workstation's already pinned `known_hosts`, then create exactly one US
`known_hosts` entry for `43.166.187.96`. Record only the public fingerprint and
file hashes in evidence.

On CPU, back up `authorized_keys` to the data disk and atomically add exactly
one line for the new public key. The options must be equivalent to:

```text
command="/usr/bin/sleep infinity",from="43.166.178.132",restrict,port-forwarding,permitlisten="127.0.0.1:18796",permitlisten="127.0.0.1:18797"
```

Reject duplicate fingerprints, any extra `permitlisten`, a non-regular
`authorized_keys`, ownership/mode drift, or a concurrent byte change. Run
`sshd -t` before and after the atomic edit; do not restart sshd. Evidence may
contain the public fingerprint and before/after hashes, never private bytes.

Render the unit, run `systemd-analyze verify`, then `daemon-reload`. Its
install-only acceptance state is `loaded`, `inactive/dead`, `disabled`,
`MainPID=ControlPID=0`, with no CPU port change. Before the window, use the
dedicated key for a bounded SSH control-master authentication check with no
forward; close it and prove no process or control socket remains. Do not test
18796/18797 while the old shared tunnel owns them.

## Fresh drain immediately before T0

Require all of the following in two consecutive samples:

1. The materials gate returns 503 for exact GET batch creation and POST
   screenshot, drama and ad creation routes. The pause journal is `paused`, its
   two test services are inactive, and current crontab matches `paused_sha256`.
2. CPU SQLite passes `quick_check` and foreign-key check. Screenshot/drama rows
   are terminal, no worker lease is running, and no ad task/asset is generating
   or regenerating.
3. CPU old local image workers and all six US image workers have one process,
   one thread and no children. US ad services are healthy on 8796/8797 and have
   no child, FFmpeg, FFprobe or Codex process.
4. No affected service connection is established. Freeze PID, process start
   ticks, cgroup PID set, restart count, active timestamps and unit hashes for
   both US ad services and both old tunnels.
5. CPU data disk UUID is
   `3e8ac4e8-7770-456d-9e89-2ec5dd405fa8`, writable, and has sufficient space.
   The three inactive target trees exist there; `/root/drama_screenshot_jobs`
   remains its existing data-disk symlink.

## Non-overlapping tunnel handoff

The shared and ad-only tunnels can never overlap because both bind 18796 and
18797. Keep the materials gate active and use this order:

1. Stop `gpu-worker-reverse-tunnel.service` and
   `gpu-screenshot-batch-burst-tunnel.service` together. Do not stop either ad
   worker. Do not disable or mask the old tunnels yet.
2. Within 5 seconds, require both old units `inactive`, zero PID/control PID,
   empty cgroups, and release of CPU 18787, 18790, 18792-18798. If release is
   incomplete, do not start another tunnel; inspect the remaining owner.
3. Start only `gpu-ad-only-reverse-tunnel.service`. Poll for at most 12 seconds.
   Require one US SSH PID and one cgroup PID, and CPU 18796/18797 loopback
   listeners owned by the same CPU sshd session whose peer is
   `43.166.178.132`. The unit command must contain exactly two reverse forwards.
4. Check exact JSON health locally on US 8796/8797 and through CPU 18796/18797.
   After those GETs close, require two empty established-connection samples.
   Recheck both ad service identities against the frozen pre-T0 values.
5. Enable the already running ad-only unit without restarting it. Require its
   PID/start ticks to remain unchanged and `UnitFileState=enabled`. The target
   ad outage should be seconds; if step 3 or 4 is not complete by T+12 seconds,
   begin rollback instead of waiting for the service restart loop.

If the new tunnel fails, explicitly stop it first and prove 18796/18797 are
free. Start only the old tunnels that were active in the frozen snapshot, then
verify their complete listener sets and all old health routes. Leave the gate
and pause active. Never start an old tunnel while any conflicting CPU listener
or ad-only process exists.

## CPU image takeover and source fence

After the ad-only lane is stable:

1. Run the exact pushed `migrate_cpu.py` with `--scope images` and a fresh
   images-only authorization to perform the final storage cutover. It may leave
   the six idle US image workers active only because both old tunnels are
   stopped. Verify all three compatibility paths now resolve to the expected
   `/mnt/data-disk/codex-workers/us-migrated/storage/...` directories; never
   touch ad public/work paths.
2. Start the three CPU migrated units on 18790/18795/18798. Verify local process
   ownership, health, four-slot pool behavior and existing public/file byte
   hashes. Do not create a production job.
3. From the exact clean GitHub checkout on US, run:

   ```sh
   python3 control/materials_split_checkpoint.py snapshot-us
   ```

   Transfer only the resulting mode-0600 JSON through the approved admin path
   into the CPU control `snapshots/` directory. Then, from the same exact clean
   commit on CPU, run:

   ```sh
   python3 control/materials_split_checkpoint.py checkpoint-cpu \
     --us-snapshot /mnt/data-disk/migrations/gpu-service-migration-20260828T1502/control/snapshots/US_SNAPSHOT.json
   ```

   The US snapshot is accepted for at most 120 seconds. The CPU helper performs
   two complete passes and emits a fresh mode-0600 checkpoint; it never changes
   services or reads credentials.
4. Transfer that checkpoint to US and immediately run the exact
   `source_fence.py materials-images` dry run and apply. The apply captures the
   current ad baseline again and aborts if any PID, start identity, cgroup,
   restart count, timestamp or unit hash differs. It masks only six image units
   and the two already stopped legacy tunnels.
5. Confirm those eight US units are inactive/masked with zero PID and no old
   listeners. Confirm the US ad services and ad-only tunnel still match the
   checkpoint and CPU 18796/18797 remain healthy.

## Rollback boundaries

Before CPU image units start, stop the ad-only tunnel, prove its ports free,
and restore the two original tunnels. After CPU image units start, first keep
admission fenced and drain/stop all three CPU migrated units; only then stop
ad-only and restore old tunnels. Leaving the data-disk compatibility symlinks
in place is the default rollback. Use storage rollback only if its manifest
proves no new data exists.

After the US source fence has retired/masked image units, use its exact backups
and rollback evidence; do not invent unit files or unmask before CPU units and
ad-only are stopped. At every boundary, one side must be fully closed and its
ports released before the other side starts.
