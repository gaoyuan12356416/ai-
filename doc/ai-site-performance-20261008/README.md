# AI backend access and independent health repairs

Observed on 2026-10-08: origin static/API responses were sub-millisecond to
13 ms, while this operator's public homepage requests took 8.8–17 seconds and
two exceeded 20 seconds. The Ashburn origin used CUBIC; the measured client
connections showed ~300–340 ms RTT and significant retransmission. Homepage
HTML was 288586 bytes without compression; gzip level 5 reduces it to ~58835.

## Release scope

- Nginx gzip for HTML and static text assets. HTML/auth/API/JSON cache policies
  stay as configured. Root public JS/CSS caches for five minutes; only an
  explicit content-hash `v` parameter gets immutable caching. Future static
  releases must update their version parameter.
- TT readiness accepts systemd month/year timespans after long host uptime,
  while preserving stopped-trigger and stale-scheduler failures.
- TT Featured needs a named `tt-drama-featured:rx` ACL on the current release
  directory, which was root-only mode 0700. Do not make the tree world-readable.
- The existing kernel's BBR module may be tested for new TCP connections.
  Preserve the current qdisc. Persist the included sysctl file only after
  measured readback; revert to the backed-up congestion-control value on failure.
- YouTube materials fail in drama metadata enrichment with MySQL error 3024
  (the existing 8-second read-only statement bound). The indexed read spans ~22000 episode rows for ~80 drama IDs. Read
  at most 20 IDs per statement, deduplicate encoded metadata across batches,
  and retain the global 1000-distinct-record bound; do not raise the timeout, change SQL Gate capacity, or bypass the
  gate for production reads. Keep exact ID/language/ambiguity and write gates.

## Deployment and rollback

Developed in a clean worktree. The running main API is a composite runtime and
is not a Git checkout; deploy only explicitly reviewed files after comparing
their current hashes. Never replace the whole app, environment or publishing
release tree. Fetch the exact pushed commit into a data-disk checkout.

Pre-change backup: `/mnt/data-disk/ai-site-performance-20261008/backup-183154`.
Nginx install: `python3 scripts/deploy_ai_site_static.py --backup <backup>`.
It fences the original config hash, checks `nginx -t`, reloads, verifies the
decompressed public shell hash and JS cache header, and restores files on failure.

Nginx rollback: restore `<backup>/etc/nginx/default.d/drama-material-api.conf`
to its original path, remove only the two newly introduced `ai-site-*` config
files, run `nginx -t`, and gracefully reload Nginx.

TT rollback: restore the backed-up `automation_health.py` to the original
resolved TT release and remove the newly added Featured ACL entry. Restart only
the cache-owning sidecar after draining active work. Keep queues, source media,
frozen recipes, tokens, SQLite and publish ledgers intact. Do not replay jobs.

BBR rollback: set `net.ipv4.tcp_congestion_control=cubic` and remove only
`/etc/sysctl.d/90-ai-site-transfer.conf` if this release created it. Existing
TCP sockets keep their own algorithm until closed.

## Validation

Run `scripts/test_tt_automation_timespan.py`, syntax checks and `git diff --check`.
Linux Nginx validation and live HTTP response hashes are mandatory. Follow with
public before/after timings and systemd health under actual service identities.
Record final evidence and the deployed commit in the deployment report.
