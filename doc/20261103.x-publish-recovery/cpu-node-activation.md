# CPU node transport activation

Scope: CPU node only. The GPU node is owned by an independent ongoing rollout.
The current CPU release is `95fdaa4c4db0ed19a465cb126d539274e95ea918`; prepare the
candidate manifest by copying the current private manifest and changing only
its `sha` to the exact pushed recovery commit. Do not change node identity,
state, environment, credentials, caps, target concurrency or enrollment.
The current private manifest is `/root/pipeline-node-cpu.json`; its real install
root is `/mnt/data-disk/root-storage-20260915/rootfs/opt/pipeline-dispatch`.
Use those manifest paths unchanged rather than reconstructing a `/opt` alias.

1. Stage the exact GitHub commit through `pipeline_node_install.py --manifest
   CANDIDATE --verify`. Do not pass `--enroll` or `--activate`. Staging builds a
   separate release/venv and verifies encode/catalog/COS compatibility without
   claiming or publishing a task. Persistent paths must remain on the mounted
   data disk.
2. Run `activate_pipeline_cpu_transport.py --current CURRENT --candidate
   CANDIDATE` from the candidate release. Default is read-only. It verifies clean
   exact releases, `.verified`, reviewed drain worker hashes, unchanged manifest
   and node environment, CPU identity, current PID/command/environment, effective
   `KillMode=mixed` / `TimeoutStopSec=infinity`, candidate executor imports and
   mocked upload regression. A generated certifi path may move to the new venv
   only when the CA contents are identical.
3. Add `--apply` after those checks. This backs up the CPU unit/private manifest/
   environment under the existing node state's `maintenance/cpu-transport-*`
   directory, installs only the CPU unit, atomically updates the private install
   manifest and queues one `systemctl restart --no-block` for the CPU unit. It
   does not change capacities, platform ledgers, credentials or the GPU unit.
4. Wait for the old parent and children to finish naturally. The reviewed parent
   treats SIGTERM as stop-after-current-job, signals workers to stop claiming,
   then joins every child. `KillMode=mixed` sends the initial signal only to the
   parent; infinite timeout prevents systemd from killing long-running children.
   A queued restart is **not** evidence that activation has completed.
5. Verify the new PID command references the exact candidate release, old child
   PIDs ended naturally, coordinator workers/heartbeats recover, node identity
   and configured capacities remain unchanged, and native/canonical receipts
   retain the same Post IDs with no extra publish attempts. Observe ordinary
   admitted work for successful receipts. Do not create a test Post.

The script rejects an already draining node instead of replacing another
rollout's pending restart. Candidate dependencies are exercised using its venv;
the transport tests simulate platform requests and issue no network writes.

Rollback must also use natural draining and the saved unit/manifest after
verifying the original staged release. This script requires the candidate to
pass the new transport regression, so the historical release without that test
is not an automatic rollback target. Never restore task databases, outboxes,
node credentials or capacities. Restoring the old transport code does not
authorize retrying an uncertain post.

## X sidecar / Auto service deployment boundary

The Auto HTTP server supports SIGTERM shutdown and waits for request threads
(`daemon_threads=False`, `block_on_close=True`). The X OAuth/publish sidecar uses
daemon request threads and lacks a graceful SIGTERM handler in the inspected
runtime. Stopping it also stops Auto through `Requires=`. Do not restart X while
schedule run 1039 is scanning or distributed publishers depend on its broker.

Create an immutable candidate from the **actual** active runtime copy, apply only
the reviewed file diff from GitHub and verify file hashes; do not replace the
composite runtime with a whole historical branch. Stop new timer/path triggers
only, preserve active oneshot processes, and wait for the current schedule and
requests to complete. Drain new X publisher admissions through an existing
supported control without resetting queues or changing claims. Only after all
current X writes and broker-dependent work finish should the sidecar switch,
then restore X first and Auto second, health-check both and restore the exact
previous trigger states. Unknown outcomes remain fenced throughout.
