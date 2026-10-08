# YouTube refreshed-token channel checks

Token refresh can return HTTP 200 with the required scopes while the immediately
following `channels.list(mine=true)` returns HTTP 401 `global/authError`. During
live read-only diagnosis, the same access token returned HTTP 401 and then HTTP
200 with the correct channel identity. A single failure previously disabled the
channel for the 300-second picker snapshot and advised reauthorization.

Channel selection and publisher identity checks now retry only this exact
read-only error, using the same token, for at most four GETs with 1/2/4-second
backoff. Selection replaces a still-rejected access token once and repeats the
same bounded checks, for at most two refreshes/eight GETs. Successful responses
must still match exactly one configured channel;
selection still requires full scopes, long-video qualification, and existing
thumbnail-failure checks. Exhausted retries remain ineligible/unknown with a
temporary-check message. Invalid refresh grants remain blocked. Quota/service
errors are distinct from permission errors. Upstream text and tokens are never
returned. Uploads, public-status updates and comments have no new retries.

The observations establish intermittent acceptance by the channel endpoint. They
do not establish Google's internal reason or prove token propagation delay.

Validation: new regression tests, existing channel-cache tests, shared YouTube
client tests, reviewed publishing engine tests, compilation and diff checks.
Production readback must confirm the real channel through the authenticated
picker and fresh submit validation without creating a video, task or comment.

Deploy the exact GitHub commit with
`scripts/deploy_youtube_channel_auth_retry.py --check`, then without `--check`.
The script checks both live file hashes, backs up code and the online SQLite
database, installs only the two Python files, restarts the API and, when the shared
client changes, sends SIGTERM only to the auto-worker main PID. The worker finishes existing preparation and
systemd starts a new process; confirm the PID change. Retain all current business
data. Roll back code with the same release script's `--rollback BACKUP_PATH`;
later file drift causes refusal. Never restore the SQLite snapshot over live data.

See [deployment-evidence.md](deployment-evidence.md) for the final live readback
and the exact two-stage procedure to restore the pre-repair code.
