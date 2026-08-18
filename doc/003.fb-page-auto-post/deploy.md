# 部署文档

## 变更内容

新增独立 FB Page 自动发布 sidecar、scheduler/plan/prepare/runner/reconcile 与 metric refresh/repair timers、主 API 代理、两张静态页和 `fb_page_posts` 权限。当前仅为部署候选，未部署。

## 配置项

真实值仅放 `/etc/fb-auto-post.env`（root 可读），不得提交。关键项：只读 MySQL、CPU internal token、独立 GPU prepare-only token、互不相同的 `FB_AUTO_POST_DB_PATH`/`FB_AUTO_METRIC_DB_PATH`、容量上限、`FB_AUTO_PREPARE_AHEAD_SECONDS=14400`、Graph v22.0。首次部署强制 `FB_AUTO_POST_LIVE_ENABLED=0`。

## 数据库变更

无 MySQL DDL/DML。Sidecar 在 `/mnt/data-disk/fb-auto-post-publisher/fb-auto-post.sqlite3` 保存运行状态，在独立 `/mnt/data-disk/fb-auto-post-publisher/fb-auto-metric.sqlite3` 保存指标代次；路径相同或指向同一文件时启动失败。两库只做加法建表/索引；旧队列只读。Page Token 不进入 SQLite。

## 部署步骤

1. 本地完成全部验证，commit/push `codex/...`，记录完整 SHA；本任务尚未执行。
2. 服务器记录 `drama-material-api.service` 当前 SHA/PID，备份 `app.py`、`.env` 元数据（不复制到 Git）、两张静态页、navigation、units。
3. 若任一 SQLite 已存在，分别用 Python online backup 到时间戳目录，执行 `PRAGMA quick_check` 和 SHA-256；不得在回滚时覆盖更新后的发布事实。
4. 从 GitHub checkout 精确 SHA 到 `/opt/fb-auto-post/releases/<sha>`，原子切换 `/opt/fb-auto-post/current`。
5. 创建 `fb-auto-post` 系统用户、0700 数据目录、0600 env；sidecar unit 创建 0700 的 `/run/fb-auto-post`，指标锁固定为 `/run/fb-auto-post/metric.lock`；GPU Python 环境确认可导入 `qcloud_cos`（COS SDK 配置固定 timeout、KeepAlive=false、retry=0）；安装 unit，`systemctl daemon-reload`。
6. 保持 live gate=0，启动 sidecar；可启动 metric 只读 timer。scheduler/plan/prepare/runner/reconcile 会返回 gate closed，不手动创建运行。
7. Scheduler 必须在 60 秒内只完成 SQLite future due-slot 规划；耗时的 Page/素材冻结由 plan unit 完成，GPU 制作由 prepare unit 完成。验证 future frontier 为当前时间后 14,400 秒。
   - Graph execute/reconcile unit 每轮最多4任务并发，任务 lease=1200、loopback HTTP=1300、RuntimeMaxSec=1500，覆盖8个Token最坏路径。
   - GPU processor与prepare unit均按串行1任务运行，不以CPU线程数宣称GPU并行能力。
8. 增量部署主 API `app.py`、`features/fb_auto_posts/`、两张 HTML、quick-nav 和 navigation 合并；不得覆盖独立推进的 X/TT 包或线上 navigation 其他项。
9. 重启仅 `fb-auto-post-service.service` 和 `drama-material-api.service`；静态发布后验证 no-store。

部署前只读门禁：对 Page/group/token、旧队列冲突、素材候选 SQL 做 `EXPLAIN` 和小范围 SELECT；确认 `g.user_id`、`ads_setting` 黑名单、目标 app/product 映射和响应大小。指标 SQL 的修正版生产只读 EXPLAIN 已通过（ONLY_FULL_GROUP_BY 合法，未执行刷新）；不得打印 Token。

## 验证步骤

```bash
curl -sS http://127.0.0.1:18835/health
systemctl status fb-auto-post-service.service fb-auto-post-scheduler.timer fb-auto-post-plan.timer fb-auto-post-prepare.timer fb-auto-post-runner.timer fb-auto-post-reconcile.timer fb-auto-post-metric-hourly.timer fb-auto-post-metric-repair.timer --no-pager
journalctl -u fb-auto-post-service.service -n 200 --no-pager
curl -sS -o /dev/null -w '%{http_code}\n' https://ai.yingliangads.com/fb-auto-publish-templates.html
```

接受标准：health `live_enabled=false`；页面 Cookie/权限正确；35 组/计数与同刻只读查询一致；自然 timer 返回 gate closed/no work；SQLite `quick_check=ok`；旧队列和 Meta 帖子计数不变；日志/DOM/API 无 Token。

## 回滚方案

1. 停止全部 FB timer 与 sidecar；恢复主 API/静态/navigation/unit 备份或 checkout 上一 SHA。
2. 重启仅主 API，验证 X/TT 契约和页面。
3. 保留当前 FB SQLite（尤其 submitted/published/unknown/attempt）；代码回滚不得恢复旧 SQLite 覆盖新事实。
4. 若尚未发生任何任务且经确认可废弃，另行归档数据目录，不直接删除。

## 注意事项

- GitHub-first，备份先于切换；本任务没有 commit/push/deploy。
- `active/running` 不等于业务完成；必须核对 health、timer、ledger、旧队列、Graph 处理状态。
- 开 live gate、创建生产模板或真实 Page 帖子均需单独明确授权。
- Graph v22.0 已对既有视频对象完成只读 status 验证；真实发帖 canary 仍须单独审批，禁止用本部署验收创建帖子。
- GPU 使用独立 `fb-page-random-overlay-gpu.service` 与 `fb-page-random-overlay-tunnel.service`，端口 `8836→18836`、work root `/data/fb-page-random-overlay`、真实 H.264 profile `tt-post-random-overlay-h264-720x1280-v3`。unit 指向本仓库 `scripts/fb_random_overlay_gpu_worker.py`；不得在本任务启动。COS真实配置只放root只读env，key为 `.../fb-page-random-overlay-h264-v3/{sha前2位}/{sha}.mp4`。失败 job 仅在 `work_root/jobs` 下按严格 job 名清理，默认保留48小时、启动和每小时有界清理，不跟随符号链接且成功 manifest 永不删。开 gate 前须对实际资产 manifest、COS public-read只读回源、NVENC耗时做集成和吞吐基准，并据此评审 `FB_AUTO_MAX_JOBS_PER_SLOT`，无基准不得上调默认20。
- 当前没有已验证的GPU目录总字节硬水位；开 live gate 前必须以数据盘可用空间大于 `max_source_bytes × 单槽任务上限` 作为外部门禁并配置磁盘告警。未完成水位验收不得开启真实发布。
