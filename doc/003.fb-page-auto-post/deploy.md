# 部署文档

## 变更内容

新增独立 FB Page 自动发布 sidecar、scheduler/plan/prepare/runner/reconcile 与 metric refresh/repair timers、主 API 代理、两张静态页和 `fb_page_posts` 权限。2026-08-18 已完成 closed-gate 生产部署，真实发布仍关闭。

## 生产部署结果（2026-08-18）

- GitHub 分支 `codex/fb-page-auto-post-20260817`；CPU/GPU current release 均为 `02dc36c090b02f968596991124bdac3a3c1585b8`。
- CPU current：`/opt/fb-auto-post/current`；GPU current：`/opt/fb-page-random-overlay/current`。
- 最终切换前 CPU online SQLite 备份：`/mnt/data-disk/fb-auto-post-deploy/backups/20260818T122418+0800-pre-02dc36c`；GPU service-state 备份：`/data/fb-page-random-overlay/backups/20260818T122155+0800-pre-02dc36c`。首次部署前完整文件备份仍保留在 `20260818T115333+0800-pre-7e8a436`（CPU）和 `20260818T115629+0800-pre-7e8a436`（GPU）。
- `FB_AUTO_POST_LIVE_ENABLED=0`；35 个Page池、4279个Page、1289个可发布、2990个缺健康Token。模板/run/task/due-slot/attempt/ledger 均为0。
- 指标 cache 已完成 2026-07-19 至 2026-08-17 的30个READY active pointer，共669,299行；hourly `:37` 与每日repair timers已启用。
- prepare-only GPU canary：3秒源素材，首次17.218秒，H.264 High 720×1280 成片3.33MB；公开HTTPS HEAD=200、`video/mp4`、size/SHA/profile元数据匹配；同job复用0.002秒。没有调用Graph。
- canary发现并关闭 BUG-005（MySQL/Python content排序）与 BUG-006（COS Metadata header前缀）。失败canary产生的无引用COS孤立对象和失败job目录已按精确身份清理并验证404；成功job仅保留manifest。

## 宏扩展生产部署结果（2026-08-20）

- GitHub/production release：`1b9fe57a90c9e64ab8ce05140fc6d0ed1d576c52`，current 为 `/opt/fb-auto-post/releases/<sha>`。
- 变更前备份：`/mnt/data-disk/fb-auto-post-deploy/backups/20260820T183309+0800-pre-1b9fe57a`，含两个SQLite online backup、env、gy.g2flow配置、两份静态页和SHA256清单；旧release为`02dc36c...`。
- 仅 `fb-auto-post-service.service` 从 PID `3083645` 换到 `587639`；Nginx仅reload，master PID仍`2164`。两者`NRestarts=0`，七个FB timers均active，部署后sidecar错误日志计数0。
- health为`live_enabled=false`；六张业务表均0、`quick_check=ok`、task已有`short_url/long_url`；公开目录wrapper=0。
- `/s2l/fb/1.html`不存在时404且带no-store/referrer/nosniff，非法路径404，POST 403；既有X、TT、TT-auto样本仍200。创建页200并显示`{{desc}}/{{url}}/AIpost`；列表页仍仅列表+创建入口，表单只在创建页。
- 生产release再跑92项FB测试全通过；未创建模板、due/run/task、短链文件或Meta帖子，主 API未重启。

## 模板启用查询兼容性修复（2026-08-21）

- 模板1点击“启用”时，主 API审计记录 `fb_auto_source_query_failed`；生产只读复现确认旧队列冲突查询被 MySQL 5.7 以错误3065拒绝：`SELECT DISTINCT` 后按未投影的 `q.id/li.page_id` 排序。
- GitHub/production release：`9df5f8cac60ef9c7c6087601a62f760f846f62b1`。查询改为按已投影别名 `queue_id,overlap_page_id` 排序，并新增回归断言。
- 变更前备份：`/mnt/data-disk/fb-auto-post-deploy/backups/20260821T104837+0800-pre-9df5f8c`，包含上一release指针、受影响源码、env/unit和两个通过 `quick_check` 的SQLite online backup；SHA256清单全部通过。
- 本地及新release均通过92项FB专项测试；生产 `@@read_only=1` 的真实查询确认Page池62为13个Page、8个可发布Page、旧队列冲突0。模板当前1天指标窗口的 `2026-08-20` active generation为READY，容量门禁未超。
- 仅重启 `fb-auto-post-service.service`，新PID `1039754`、`NRestarts=0`；七个FB timers均active。health仍为 `live_enabled=false`，模板1仍为disabled，run/task/due/attempt/ledger和wrapper均为0，未调用Meta。
- “启用”查询缺陷已修复；“手动执行”仍按既有安全合同返回 `fb_auto_live_gate_closed`。开启 `FB_AUTO_POST_LIVE_ENABLED=1` 属于真实Graph发布授权，不包含在本次故障修复中。

## 单Page真实canary与素材扫描优化（2026-08-21）

- 用户明确授权从模板1的Page池随机选择一个可发布Page发送一条测试帖。随机意图冻结为Page `967347116442420`（`कहानी के दृश्य`）；失败后不重抽Page，整个canary严格限制为1 run、1 task、1 Graph对象。
- 第一次尝试在完整素材PRIMARY keyset扫描达到600秒截止后以`fb_auto_catalog_scan_timeout`停止；审计文件为`/mnt/data-disk/fb-auto-post-publisher/one-shot-audits/fb-one-page-canary-20260821T030437Z-b106dd39c646.json`，run/task/GPU/Meta均为0。
- GitHub/production release为`9f1f5b268766e1c25fbe3081bd0505978510b78e`。按主排序剧集的正spend或已定义ROAS指标集合走`idx_source_type_source_id`精确预筛，仍应用产品、语言、上映、黑名单、时长、描述及指标边界；只有精确过滤集合填满5000条候选时才跳过全表扫描，否则回退原完整扫描，避免优化改变选择语义。spend升序不走该捷径。
- 本地95项FB专项、66项X/TT合并基线和生产release 95项FB专项全部通过。生产只读真实模板基准为5000条候选/142.266秒、PRIMARY catalog调用0；修复前相同流程在600秒超时。
- 切换前online backup为`/mnt/data-disk/fb-auto-post-deploy/backups/20260821T112904+0800-pre-9f1f5b2`，旧release为`9df5f8c...`。原子切换后仅重启`fb-auto-post-service.service`，PID `1062207`、`NRestarts=0`、warning日志0。
- 重试审计文件为`/mnt/data-disk/fb-auto-post-publisher/one-shot-audits/fb-one-page-canary-retry-20260821T033040Z-70d39da53667.json`。run 1/task 1冻结素材`6281282`、content `XtTulNgWI1`；GPU成片profile=`tt-post-random-overlay-h264-720x1280-v3`，时长475.766667秒、大小285,917,510 bytes、SHA=`a416ee8628ca0f2eb8391795a87dd16d831ccff1e72e4283eed638015a116773`，COS HEAD 200且元数据一致。
- Graph第一个授权明确返回190，按同Page授权轮换合同使用第二个授权成功提交；对象`1051031017645759`在首次到期回查时确认`video_status=ready`和`publish_status=published`。永久链接为`https://www.facebook.com/reel/1051031017645759/`并实测HTTP 200。对账前submitted事实备份为`/mnt/data-disk/fb-auto-post-deploy/backups/20260821T115928+0800-pre-task1-reconcile`，`quick_check=ok`。
- 最终run=`completed`、task/ledger=`published`、unknown=0；只有1个run、1个task、1个ledger、2条授权attempt。短链`https://gy.g2flow.com/s2l/fb/1.html`返回200和`no-store`，目标为指定`https://www.dramawavew2a.com/ads/0/2049/view`并含`af_channel=AIpost`。
- 持久化`/etc/fb-auto-post.env`仍为`FB_AUTO_POST_LIVE_ENABLED=0`，health仍`live_enabled=false`。真实发布与回查只在边界清晰的一次性进程中临时打开；七个自然timer虽然active，但不会继续创建或发布其他任务。模板1本身保持enabled，未来自动执行仍被总开关阻断。

## 配置项

真实值仅放 `/etc/fb-auto-post.env`（root 可读），不得提交。关键项：只读 MySQL、CPU internal token、独立 GPU prepare-only token、互不相同的 `FB_AUTO_POST_DB_PATH`/`FB_AUTO_METRIC_DB_PATH`、容量上限、`FB_AUTO_PREPARE_AHEAD_SECONDS=14400`、Graph v22.0。首次部署强制 `FB_AUTO_POST_LIVE_ENABLED=0`。

2026-08-20 宏扩展新增 `FB_AUTO_POST_SHORT_LINK_ROOT=/mnt/data-disk/fb-auto-post-public/s2l/fb`。该公开目录与 0700 的 SQLite 私有目录分离；父目录和 wrapper 分别为 0755/0644。`gy.g2flow.com` TLS server 引入 `deploy/nginx-fb-auto-short-domain-location.conf`，精确服务 `/s2l/fb/{正整数}.html`。

## 数据库变更

无 MySQL DDL/DML。Sidecar 在 `/mnt/data-disk/fb-auto-post-publisher/fb-auto-post.sqlite3` 保存运行状态，在独立 `/mnt/data-disk/fb-auto-post-publisher/fb-auto-metric.sqlite3` 保存指标代次；路径相同或指向同一文件时启动失败。两库只做加法建表/索引；旧队列只读。Page Token 不进入 SQLite。

## 部署步骤

1. 本地完成全部验证，commit/push `codex/...`，记录完整 SHA。
2. 服务器记录 `drama-material-api.service` 当前 SHA/PID，备份 `app.py`、`.env` 元数据（不复制到 Git）、两张静态页、navigation、units。
3. 若任一 SQLite 已存在，分别用 Python online backup 到时间戳目录，执行 `PRAGMA quick_check` 和 SHA-256；不得在回滚时覆盖更新后的发布事实。
4. 从 GitHub checkout 精确 SHA 到 `/opt/fb-auto-post/releases/<sha>`，原子切换 `/opt/fb-auto-post/current`。
5. 创建 `fb-auto-post` 系统用户、0700 数据目录、0600 env；sidecar unit 创建 0700 的 `/run/fb-auto-post`，指标锁固定为 `/run/fb-auto-post/metric.lock`；GPU 固定使用现有 `/root/miniconda3/envs/drama-voice/bin/python`，并确认该环境可导入 `qcloud_cos`（COS SDK 配置固定 timeout、KeepAlive=false、retry=0）；安装 unit，`systemctl daemon-reload`。
6. 保持 live gate=0，启动 sidecar；可启动 metric 只读 timer。scheduler/plan/prepare/runner/reconcile 会返回 gate closed，不手动创建运行。
7. Scheduler 必须在 60 秒内只完成 SQLite future due-slot 规划；耗时的 Page/素材冻结由 plan unit 完成，GPU 制作由 prepare unit 完成。验证 future frontier 为当前时间后 14,400 秒。
   - Graph execute/reconcile unit 每轮最多4任务并发，任务 lease=1200、loopback HTTP=1300、`TimeoutStartSec=1500`，覆盖8个Token最坏路径；oneshot 不使用该生产 systemd 会忽略的 `RuntimeMaxSec`。
   - GPU processor与prepare unit均按串行1任务运行，不以CPU线程数宣称GPU并行能力。
8. 增量部署主 API `app.py`、`features/fb_auto_posts/`、两张 HTML、quick-nav 和 navigation 合并；不得覆盖独立推进的 X/TT 包或线上 navigation 其他项。
9. 重启仅 `fb-auto-post-service.service` 和 `drama-material-api.service`；静态发布后验证 no-store。

宏扩展不改 `app.py`、navigation 或主 API 进程：部署时备份 current symlink、两个 SQLite online backup、`/etc/fb-auto-post.env` 元数据、Nginx live配置和两份创建页；从已推送 SHA 创建 immutable release，补充 env、创建独立公开根，`nginx -t` 后 reload，切换 sidecar并只重启 `fb-auto-post-service.service`，最后原子安装两份静态 HTML。保持 `FB_AUTO_POST_LIVE_ENABLED=0`。

部署前只读门禁：对 Page/group/token、旧队列冲突、素材候选 SQL 做 `EXPLAIN` 和小范围 SELECT；确认 `g.user_id`、`ads_setting` 黑名单、目标 app/product 映射和响应大小。指标 SQL 的修正版生产只读 EXPLAIN 与30日实际刷新均已通过；不得打印 Token。

## 验证步骤

```bash
curl -sS http://127.0.0.1:18835/health
systemctl status fb-auto-post-service.service fb-auto-post-scheduler.timer fb-auto-post-plan.timer fb-auto-post-prepare.timer fb-auto-post-runner.timer fb-auto-post-reconcile.timer fb-auto-post-metric-hourly.timer fb-auto-post-metric-repair.timer --no-pager
journalctl -u fb-auto-post-service.service -n 200 --no-pager
curl -sS -o /dev/null -w '%{http_code}\n' https://ai.yingliangads.com/fb-auto-publish-templates.html
```

接受标准：health `live_enabled=false`；页面 Cookie/权限正确；35 组/计数与同刻只读查询一致；自然 timer 返回 gate closed/no work；SQLite `quick_check=ok`；旧队列和 Meta 帖子计数不变；日志/DOM/API 无 Token。

宏扩展附加接受标准：`fb_auto_task` 存在 `short_url/long_url` 加法列；六张业务表计数仍为0；不存在的合法短链返回404且带 no-store/security headers，非法路径返回404，POST被拒绝；TT/X既有 `/s2l` 路由不变；创建页可见 `{{desc}}/{{url}}/AIpost`。不得为验收创建 wrapper、模板、run 或 Graph Post。

## 回滚方案

1. 停止全部 FB timer 与 sidecar；恢复主 API/静态/navigation/unit 备份或 checkout 上一 SHA。
2. 重启仅主 API，验证 X/TT 契约和页面。
3. 保留当前 FB SQLite（尤其 submitted/published/unknown/attempt）；代码回滚不得恢复旧 SQLite 覆盖新事实。
4. 若尚未发生任何任务且经确认可废弃，另行归档数据目录，不直接删除。

含 `{{url}}` 的任务存在时不得单独回滚 publisher 而保留待发布任务；必须先关闭 gate/全部FB timers并审计 planned/ready/running/submitted/unknown。历史 wrapper、Nginx `/s2l/fb/` route 与当前 SQLite 必须保留，避免已发布短链失效。

## 注意事项

- GitHub-first，备份先于切换；上述 production SHA 已完成 commit/push/deploy。
- `active/running` 不等于业务完成；必须核对 health、timer、ledger、旧队列、Graph 处理状态。
- 开 live gate、创建生产模板或真实 Page 帖子均需单独明确授权。
- Graph v22.0 已完成一次用户明确授权的单Page真实发帖canary；该授权已消费完毕，禁止据此继续创建帖子、扩大Page范围或打开持久化总开关。
- GPU 使用独立 `fb-page-random-overlay-gpu.service` 与 `fb-page-random-overlay-tunnel.service`，端口 `8836→18836`、work root `/data/fb-page-random-overlay`、真实 H.264 profile `tt-post-random-overlay-h264-720x1280-v3`。unit 指向本仓库 `scripts/fb_random_overlay_gpu_worker.py`。COS真实配置只放root只读env，key为 `.../fb-page-random-overlay-h264-v3/{sha前2位}/{sha}.mp4`。失败 job 仅在 `work_root/jobs` 下按严格 job 名清理，默认保留48小时、启动和每小时有界清理，不跟随符号链接且成功 manifest 永不删。实际资产/COS/NVENC单任务canary已通过；无并发吞吐基准前不得上调默认20。
- GPU `/data` 当前可用101G，高于 `2GB × 20 = 40GB` 的外部门禁快照；仍需配置持续磁盘告警，并在开启live前复核当时水位。
