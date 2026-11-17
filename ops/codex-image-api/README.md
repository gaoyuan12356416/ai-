# Asynchronous Codex image API

Two authenticated public endpoints under /api/image-generation/tasks. Each input URL
is one independent image; groups share prompts, while the batch shares one task ID.
Each `image_urls` entry may be an object with `image_url` and a non-empty `title`.
The queue freezes that image's title together with its group's prompt as the actual
generation request. Legacy URL strings keep their original prompt and canonical
idempotency hash. Titles participate in new-request hashes, so changing a title
requires a new `request_id`. No historical task rewrite is needed.
`image_id` is optional for each titled reference object. The caller supplies a
non-blank string (recommended for IDs with leading zeros or long numbers) or an
integer. Any ID is stored with the image and returned unchanged, with the same
JSON type, in its terminal result whether successful or failed. Results for omitted
IDs have `image_id: null`. The ID is correlation data, independent of generation,
and participates in the request hash only when supplied. Result ordering is unchanged.
`task_id` is always 35 ASCII characters: `ig_` plus `uuid.uuid4().hex` (32 lowercase
hexadecimal characters), matching `^ig_[0-9a-f]{32}$`.
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
A schema update adds the nullable `images.image_id_json` column on API startup;
existing rows and omission-based idempotency keys stay unchanged. The previous
worker can read and finish rows in the updated database and need not restart for
this API-only change. A code rollback preserves the column and existing IDs.
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
