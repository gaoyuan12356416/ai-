# Asynchronous Codex image API

Two authenticated public endpoints under /api/image-generation/tasks. Each input URL
is one independent image; groups share prompts, while the batch shares one task ID.
The API only writes/reads SQLite. A singleton worker orchestrates downloads,
unprivileged Codex subprocess units, image validation and verified COS uploads.

## Client documentation

Run `python build_docs.py OUTPUT_DIRECTORY` to produce a standalone Chinese HTML
guide and OpenAPI 3.0 JSON. Documents contain placeholders, never credentials.
HTTP access requires both source IP 101.32.30.18 and the existing screenshot API
Token. Token rotation is read directly from the existing backend environment file.

## Deployment

Commit and push to gaoyuan12356416/ai- first. Fetch the exact commit into the private
bare mirror under /mnt/data-disk/codex-image-api/source.git; export only
ops/codex-image-api into /opt/codex-image-api/releases/COMMIT. Verify the commit,
run `python3 -m unittest test_image_api.py`, then run `python3 install.py COMMIT`
from that release. The installer verifies the mounted data disk, saves affected
configuration and an online SQLite snapshot, installs the dedicated low-privilege
runner account, switches the release, starts only the two new services, and
validates/reloads Nginx. Other application and publishing services are unchanged.

The host relocates /opt to the data disk. Each child namespace therefore mounts
the resolved immutable code directory read-only, and only its image work directory
read-write. The rest of /mnt and /root are unavailable. Transient-unit UMask=0022
is required for systemd 239 namespace traversal; all task directories are created
0700 and runner.py sets umask 0077 before writes. The runner receives only a private
Codex login copy; COS and API credentials remain in the privileged orchestrator.
Codex runs with approvals bypassed only inside this external OS isolation.

## State and recovery

SQLite: /mnt/data-disk/codex-image-api/state.sqlite3. Generated files, receipts and
upload checkpoints: jobs/TASK/ORDINAL. Code rollback never restores an old ledger.
The worker has one filesystem lock and transactionally claims one image at a time.
A generation attempt is committed before its unit is launched. On restart, the
worker inspects that unit rather than spawning again. Each child runs outside the
worker service cgroup and survives a worker restart. A dead process without a
receipt is a visible failure; completed local images resume only the upload step.
COS keys are deterministic. HEAD checksum/size verification reconciles an unknown
PUT result before considering another upload. Successful outputs are never remade.

Inspect `systemctl status codex-image-api codex-image-worker`, loopback GET
http://127.0.0.1:18853/health, and private per-attempt logs. Never publish raw Codex
logs or authentication files. Local health is not a public API endpoint.

## Validation

`python3 canary.py submit LABEL` deliberately creates a real generation batch and
uploads a harmless reference fixture. `python3 canary.py status LABEL` reads its
status. Submission is idempotent per label; keep previous failed acceptance records.
It includes two different image prompts and one intentionally missing reference.
Do not schedule this command or rerun with new labels without a verification need.
The production allowlist can be tested from a disallowed source, including spoofed
forwarding headers. Successful calls from 101.32.30.18 must be documented separately
unless a test can actually originate there; loopback tests do not prove that path.

## Rollback

Run `python3 /opt/codex-image-api/current/ops/codex-image-api/rollback.py MANIFEST`
with the exact manifest.json path printed by the installer. It restores only this
feature's code/config, preserves SQLite/receipts/COS data, validates Nginx and reloads.
Rolling back to a previous release preserves active per-image units. Withdrawing
the initial installation waits for this service's in-flight images only, then
disables the two services and removes only the new route/config. Queued records
remain available for a later reinstall. The low-privilege user and data stay intact.
