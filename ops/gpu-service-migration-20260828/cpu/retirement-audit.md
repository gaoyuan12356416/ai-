# US automatic-start and cleanup audit

Read-only observation: 2026-08-28 15:49 +08:00, host `43.166.178.132`
(`VM-0-13-centos`). No cron, unit, service, process, or business job was changed
by this audit. Service states below are a snapshot; the coordinator is performing
separate migrations and must refresh them before acting.

## Additional cleanup entries

These entries run cleanup, not service startup. Preserve their source files and
configuration in the migration backup before disabling their exact trigger.
Do not stop `crond`, clear the root crontab, delete shared directories, or run the
cleanup scripts to verify retirement.

| Entry | Scope and recommended handling |
| --- | --- |
| `/etc/cron.d/gpu-drama-cleanup`, line 3 | Dedicated legacy GPU cleanup cron. Disable this dedicated cron file after the source business is fenced, to retain old work/history while the US host remains. It also cleans generic root package caches and old `/tmp` directories; this is not a reason to remove any system cleanup timer. |
| Root crontab (`/var/spool/cron/root`), line 3 | Independent ad-template stale-work cleanup. Disable only this exact line **after confirming the old template-production caller is frozen or migrated**. The 17 systemd masks alone do not stop an external SSH caller from executing this Python worker directly. |

Exact non-secret entries:

```cron
17 * * * * root flock -n /var/lock/gpu-drama-cleanup.lock /usr/local/sbin/gpu-drama-cleanup.sh
```

```cron
17 * * * * AD_MATERIAL_TEMPLATE_GPU_TMP_DIR=/data/ad-material-template-production/tmp AD_MATERIAL_TEMPLATE_GPU_CLEANUP_STALE_HOURS=6 /usr/bin/flock -xn /tmp/ad_template_material_gpu_cleanup.lock -c 'python3 /data/ad-material-template-production/gpu_ad_material_worker.py --cleanup-stale --max-age-hours 6' >> /data/ad-material-template-production/gpu_cleanup.log 2>&1
```

SHA256 values exclude the line-ending byte for individual entries:

| Object | SHA256 |
| --- | --- |
| `/etc/cron.d/gpu-drama-cleanup` complete file | `fe59362c5cdd74ba88487056fd8b63c550121d90ad16d3255046abb5de71f2d8` |
| Its line 3 | `96d4b79d2ef520a690259cda11519deeec0302f6e4ebfa9cb924c1d7e187139e` |
| Root crontab complete file | `aa8e8aee731cb302fd3c728ccb6b46853f0f743d2aea1f56336f4d32dc22d903` |
| Root crontab line 3 | `5508a02e46a5dde3206b6fb154c52216ddc8cb6ce6cda6fb1241ecdaee6ab68c` |
| `/usr/local/sbin/gpu-drama-cleanup.sh` | `0460472463db4c5eec9cf25db7b213386a7d81dee77172ea68222f7ab982bdb3` |
| `/data/ad-material-template-production/gpu_ad_material_worker.py` | `1603527441634435c5cc456310ced77c966598969af60d03b335a061381f815c` |

The general GPU cleanup retains public assets explicitly. It removes old work
directories, screenshot caches, old Codex session logs, root package caches and
old `/tmp` directories; its emergency threshold shortens some work retention to
three days. The `--dry-run` path still writes logs and can run `npm cache verify`,
so it was not executed for this read-only audit.

The template worker's `--cleanup-stale` branch returns before rendering. It
cleans only matching temporary workdirs and
`/data/kronos-web-runs/produced-ad-videos-*/*.part.*` older than six hours. The
shared `/data/kronos-web-runs` root and completed videos must be retained. Neither
cleanup process was running at observation time.

## Dependency-driven automatic starts

| Existing trigger | Relationship and affected workers |
| --- | --- |
| `gpu-worker-reverse-tunnel.service` | `Wants=` drama API, cover, square, landscape, portrait. Cover and three dimension units were disabled yet active because of this dependency. |
| `gpu-screenshot-batch-burst-tunnel.service` | `Wants=codex-screenshot-batch-burst.service`. |
| `tt-gpu-reverse-tunnel.service` | `Requires=tt-gpu-publisher.service`. |
| `tt-gpu-direct-outro-reverse-tunnel.service` | `Requires=tt-gpu-direct-outro.service`. |
| `x-post-media-repair-tunnel.service` | `Requires=x-post-media-repair.service`. |

The coordinator's 12-worker plus 5-tunnel persistent-mask scope covers these
relationships. Disabling only the worker units is insufficient. No additional
business timer, socket, path activation, `OnFailure`, `PartOf`, or external
`Wants`/`Requires` trigger was found in the inspected systemd configuration.
Normal `multi-user.target.wants` links exist for the enabled services.

The following additional units were inactive, disabled, PID 0, and had no active
`WantedBy`/`RequiredBy` relationship. Preserve their state; this audit does not
expand the approved stop list:

- `codex-screenshot-simple-prompt-test.service`
- `gpu-simple-prompt-test-tunnel.service`
- `fb-page-random-overlay-gpu.service`
- `fb-page-random-overlay-tunnel.service`

## Preserve Kronos and system maintenance

- Keep `kronos-stock-web.service` enabled and running. It has no dependency on
  the 17 retiring units. Its executable is the Kronos environment's Gunicorn.
- Preserve root crontab line 2 (`*/5 ... /usr/local/qcloud/stargate/admin/start.sh`),
  `/etc/cron.d/sgagenttask`, `/etc/cron.d/yunjing` including its `@reboot` entry,
  Tencent TAT/security agents, SSH, networking and Nginx.
- Preserve `/etc/cron.d/0hourly`, anacron, daily logrotate and normal system cron.
- Preserve `certbot-renew.timer`, `dnf-makecache.timer`,
  `systemd-tmpfiles-clean.timer`, `unbound-anchor.timer`, and system sockets/path
  units. `nv_gpu_shutdown_pm.service` belongs to `shutdown.target`, not business
  startup; leave it unchanged.
- `/etc/rc.local` resolves to the normal rc script. Its active lines only touch
  `/var/lock/subsys/local` and run the Tencent IRQ affinity helper. Preserve it.
- Do not clear `/data/kronos-web-runs`, `/data/migrations`, or root authentication
  state as part of retiring GPU business services.

## Coverage and limitation

Inspected all root/user cron spool files present, system cron directories,
`rc.local`, system unit files and enablement links under `/etc`, `/usr/lib` and
`/run`, transient systemd units, root shell startup files, tmpfiles/logrotate
hooks, the `at` queue and supervisor process/configuration indicators. No
Supervisor, Monit, PM2, Docker/Podman, screen/tmux supervisor, root user-systemd
unit directory, or queued `at` job was found. The only unrelated restart hook
found was logrotate's `try-restart kvm_stat.service`.

This does not prove absence of future external SSH invocations. In particular,
`gpu_ad_material_worker.py` remains an executable production entrypoint outside
the 17 services. Confirm its configured CPU-side producer/cleanup callers before
declaring that separate template-production pipeline migrated. No CPU producer
configuration was changed or executed by this audit.

## CPU ad-template callers confirmed after the US audit

Read-only follow-up on `43.166.187.96`, 2026-08-28 approximately 16:03–16:17
+08:00. These are current uncommented cron entries, not historical scripts.
No cron was disabled or executed by this audit.

| Current CPU trigger | Actual producer path and host selection |
| --- | --- |
| Root crontab line 28, `7,22,37,52 * * * *`, `hotdrama_auto_ops.py` | Explicit `HOTDRAMA_AD_MATERIAL_TEMPLATE_PRODUCE_COMMAND=python3 /root/codex_test/ad_template_material_gpu_direct_producer.py`; explicit matching GPU delete command. `AD_MATERIAL_TEMPLATE_GPU_HOST` is not overridden, so the producer/delete/SCP default remains US `43.166.178.132`. |
| Root crontab line 67, `*/10 * * * *`, `run_dramawave_new_drama_template_prepare.sh` | Wrapper supplies `--template-produce-command` with the same direct producer as default. Its `dramawave_new_drama_publish_once.py` propagates that command into product/generic producer environment variables. No GPU host override is present, so it also remains US. |

The producer invokes SSH directly to
`/data/ad-material-template-production/gpu_ad_material_worker.py`, with the
existing CPU-to-GPU key referenced by path (never read or copied here). Its
temporary root is `/data/ad-material-template-production/tmp`; its output root
is `/data/kronos-web-runs`, with public base `http://gai.yingliangads.com`.
`hotdrama_auto_ops.py` also uses the default US host for its local SCP stage.
The producer's imported `dramawave_gaoyuan_auto_test.py` and
`ad_template_material_cache.py` contain no environment loader/assignment that
overrides this host. Relevant `crond` environment keys were absent.

The two live action services (`dramawave-new-drama-action.service`,
`hotdrama-ai-review-action.service`) had no producer/GPU-host/delete-command
environment override at inspection. Their direct entry scripts did not name the
direct GPU producer. The runtime/cache's producer-command default is empty;
therefore this audit does not label those action daemons as separate confirmed
US producer triggers.

CPU root crontab line 51 runs template-cache maintenance at `23 4 * * *`.
Its `delete_command()` is product/generic environment-only and defaults to an
empty string. No delete command was present in that cron or daemon environment;
the inspected implementation skips `gpu_direct` remote deletion when it is
empty. Preserve this CPU cache-maintenance job; do not misidentify it as an
active remote-US delete command based solely on its filename.

No direct producer/template-prepare/cleanup process was present in the sampled
CPU process table. That snapshot does not prevent the next cron tick from
starting one. The 17 US unit masks cannot fence these external SSH calls.
The material API maintenance gate also cannot intercept these direct SSH calls.
The HotDrama cron also runs wider operations, so do not silently stop the entire
cron as a GPU service fix. The coordinator must explicitly handle this separate
template pipeline's producer host, copyback path and cleanup host together, or
record it as still using US. Keep Kronos and completed shared output files.

| Current CPU object | SHA256 |
| --- | --- |
| Root crontab line 28, no newline | `09da56a3ab57299933d851510a43c5670092b921811bc90faf7f7d9c7536381d` |
| Root crontab line 67, no newline | `51dfdd4eeaacdc0e65c0facf635eeb727ff2002e19d500e03bb04bf3b3df1461` |
| Root crontab line 51, no newline | `c344b0f68e5053394e19eeda56f972a367a60bf861360dd4bc4db226a5847f70` |
| `ad_template_material_gpu_direct_producer.py` | `2eb061140397eda75435bdbae939e0f8237629383d5c30688c77e9b74fa1ca03` |
| `ad_template_material_gpu_delete.py` | `38761ef1251f571077b82a485f465c163073e278703c6097a19dd37dfb009c1a` |
| `hotdrama_auto_ops.py` | `a123dd161586d9e2e39cb46568df2b83c1b04dc6f2d844d6f509214ac59d97c1` |
| `ad_template_material_cache_maintenance.py` | `1b70e2338b00c80b743770693b4e151fbcc2a1cfdf2317d081bd6988d2cfdff1` |

## HK running drama: progress and CPU directory dependency

Read-only follow-up on `43.154.250.89`. No process was killed, traced, or paused;
no `/proc/.../fd` file or pipe was opened. For regular files only, descriptor
symlink names and the textual `fdinfo` position were inspected.

Job `679e7c49acbf4af79f78bf60d76c5dd7` was still rendering in ffmpeg PID
`1708285`, parent `1188891`, under `drama-synthesis-gpu-worker.service`.
The active release is `e1f5a1d04cfb510df9c2444ac592adec2827508b`.
Its main input is a local merged H.264/AAC MP4, 1280×720, about 25 fps;
safe metadata-only ffprobe reported **5573.906333 seconds (92m53.906s)**.
The configured encoder is `h264_nvenc`, preset `p5`, duration `-t 5573.906333`.

The output
`/data/drama-synthesis-gpu/results/public/679e7c49acbf4af79f78bf60d76c5dd7/material_random_template.mp4`
kept growing with current modification times:

| HK local time (+08:00) | Output bytes |
| --- | ---: |
| 16:03:24 | 1,214,251,056 |
| 16:05:05 | 1,235,484,720 |
| 16:10:03 | 1,302,855,728 |
| 16:15:05 | 1,374,158,896 |

During the first 101-second interval the process also accumulated about 202
CPU-seconds. This is evidence of continued work, not a finished job. The command
has `-loglevel error` and no `-progress`; existing logs showed the earlier concat
stage but no reliable frame/time progress. The main input's descriptor position
was 1,884,913,728 of 5,139,047,136 bytes at 16:05; this must **not** be presented
as an accurate timeline percentage or ETA.

All current ffmpeg inputs, assets and outputs are ordinary HK-local `/data`
paths. `/data` resolves to the HK root filesystem, consistent with the user's
later approved expanded-root storage arrangement, not a CPU share. The cover
marker already exists and names the COS cover object; this render is past the
cover-wait stage. No current ffmpeg input points at CPU root/public directories.

Inspection of the actual release's `app.py` and the parent process's allowlisted
environment confirms the remaining path: `render_random_output()` returns,
`publish_asset()` sends the video to COS, `write_gpu_video_result()` writes a
local manifest under `/data/drama-synthesis-gpu/results/manifests`, then cleanup
targets only this job's HK-local work/public directories. The `_gpu_worker`
progress path explicitly skips database persistence. There is no CPU shared
directory dependency in this remaining flow. This does not change the agreed
rule to wait for this actual render to finish naturally before the coordinated
cutover; a CPU job row marked failed is not evidence that the HK render drained.
