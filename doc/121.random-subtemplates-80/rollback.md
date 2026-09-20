# 默认目录回退操作

回退目标为 9 月 18 日目录 `24d6fad3174f765d3433c9ab71322f04241b11acf0224a4e1392f138295adf0c`。这是操作说明，本次未执行回退。

1. 在 CPU 和 HK 各创建新的、独立时间戳的 0700 备份目录，保存当前 env、三个 GPU 的 `99-zz-random-subtemplates.conf`、CPU `/etc/drama-synthesis/cpu.env` 和触发器状态。不要覆盖本次上线备份，也不要直接重跑含固定备份路径的上线脚本。
2. CPU 使用 `/mnt/data-disk/random-overlay-gpu/backups/20260908-cache/maintenance.py`，依次运行 `python3 <该路径> gate-on tt --apply` 和 `python3 <该路径> pause tt --apply`；记录并停止 `fb-auto-post-prepare.timer`。如已有 FB 领取客户端，按 `operations/pause_fb_claims.py` 的身份与持久化意图规则暂缓后续领取，使用新的备份路径。等待已接受的 GPU 制作及 HTTP 请求排空。不能因执行回退而直接终止仍有进展的制作。
3. 只改三个 `/etc/random-overlay-subtemplates/{drama,tt,fb}-20260920.env` 的默认 ROOT/SHA：Drama 根改为 `/data/drama-synthesis-gpu/assets/catalog-24d6fad3174f`，TT/FB 根改为 `/data/random-overlay-gpu/assets/catalog-24d6fad3174f`，各自 `_MANIFEST_SHA256` 改为上述完整旧 SHA。TT 和 Drama 的 `RANDOM_OVERLAY_ASSET_CATALOGS` 必须继续保留原始 `028326…`、`24d6fa…` 和 `b5df77…` 三个完整 SHA 映射。不要用旧 env 整体覆盖而丢失新版本信任。
4. CPU `/etc/drama-synthesis/cpu.env` 只将 `DRAMA_RANDOM_OVERLAY_MANIFEST_FILE` 改为 `/mnt/data-disk/drama-synthesis-catalog/catalog-24d6fad3174f/manifest.json`，将 `DRAMA_RANDOM_OVERLAY_MANIFEST_SHA256` 改为完整旧 SHA。保留其余配置和权限。应用代码指针继续为 Drama `9c4c3cb`、TT random `326e16d`、FB `e3bef98`。
5. 在 HK 窄重启 `drama-synthesis-gpu-worker.service`、`tt-gpu-publisher.service`、`fb-page-random-overlay-gpu.service`，待启动预检通过，再启动/重启 `drama-synthesis-gpu-tunnel.service`、`tt-gpu-reverse-tunnel.service`、`fb-page-random-overlay-tunnel.service`。CPU 重启 `drama-material-api.service` 和 `drama-material-job-worker.service`。核实 CPU 18788/healthz、18830/health、18836/health 可用，目录默认 SHA 为旧值、活动数量为 8/10/8/12；三个受信目录仍可读取。
6. 对本次暂停的 FB 客户端按原 PID/start_ticks/unit 精确 SIGCONT。CPU 控制器依次运行 `resume tt --apply`、`gate-off tt --apply`，按记录恢复 FB timer。核实七个 TT 触发器均恢复原状态，`tt-triggers.json` 的 restored 为 true、TT gate 清除，公共 `/api/ui/topbar` 返回 200。

保留全部 80 个素材、两个 RGBA 缓存、新旧 manifest、正在使用的配方以及已完成成片。禁止恢复旧数据库、重写历史记录、删除新目录或替换为不识别新 SHA 的旧应用代码。
