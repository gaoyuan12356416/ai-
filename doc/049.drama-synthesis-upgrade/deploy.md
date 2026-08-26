# 部署与回滚文档

## 授权边界

根授权允许在本文全部 release blocker 和部署 gate 满足后，按 GitHub-first 流程部署生产代码。当前外部 blocker 尚未关闭，因此本候选仍为 HOLD，不得 push/deploy、写生产文件、重启服务或写生产数据库。真实 YouTube 发布/评论不随代码部署获得授权；任何真实视频/评论都必须另行取得精确授权，部署和 smoke 均不得触发。

## GitHub-first 发布物

- CPU/HK 必须从同一已审核 Git commit 部署。
- CPU 目录 `/root/drama_material_service`；HK 计划目录 `/data/app/drama_material_service`。
- systemd 候选：`drama-material-api.service`（CPU 原服务）、`drama-material-job-worker.service`（原 worker）、`drama-youtube-publish-worker.service`、`drama-material-hk-gpu.service`、`drama-material-hk-gpu-tunnel.service`。
- 不覆盖旧 GPU 18787 隧道，HK 使用 CPU remote port 18788。

## 配置

CPU：

```text
GPU_VIDEO_WORKER_URL=http://127.0.0.1:18788
GPU_VIDEO_WORKER_TOKEN=<server-only shared secret>
DRAMA_SHORT_LINK_ROOT=<blank until audited CloudFront/S3 publish mount exists>
DRAMA_YOUTUBE_WORK_ROOT=/mnt/data-disk/drama-youtube-publish
DRAMA_YOUTUBE_SOURCE_HOSTS=advertising-1306474899.cos.ap-hongkong.myqcloud.com,ai.yingliangads.com
```

HK：

```text
DRAMA_RANDOM_OVERLAY_ROOT=/data/fb-page-random-overlay/assets/v1
DRAMA_RANDOM_OVERLAY_MANIFEST_SHA256=028326ab211418934b026c227f2e3707553cce7560551dca3c0bfddc681d566f
DRAMA_RANDOM_OVERLAY_FFMPEG=/usr/bin/ffmpeg
DRAMA_RANDOM_OVERLAY_FFPROBE=/usr/bin/ffprobe
DRAMA_API_HOST=127.0.0.1
DRAMA_API_PORT=8788
```

不得将任何真实 token/secret 写入 Git、命令日志或测试结果。

## 数据库

无需 MySQL DDL。SQLite 由 `DRAMA_SYNTHESIS_STORE.ensure_storage()` 幂等新增：

- `drama_synthesis_recipe`
- `drama_synthesis_short_link`
- `drama_youtube_publish_task`
- `drama_youtube_publish_event`

上线前备份现有 SQLite 文件。回滚代码时保留新增表，旧代码不会读取；不要 drop 表，以保留发布/审计 identity。

## 部署前 dry-run

1. 在候选提交运行 focused tests、compile、JS syntax、diff check、秘密扫描。
2. 复核 CPU live commit/blobs 和服务状态未漂移；若漂移则停止，重新合成基线。
3. 将旧 GPU `/data/fb-page-random-overlay/assets/v1` 复制到 HK 同路径或明确新路径；核对：20 files、520,297,533 bytes、manifest SHA 和全部 file SHA。禁止只核对文件名。
4. 在 HK 本机只监听 `127.0.0.1:8788`，验证 internal catalog 分类与 profile；用合成测试文件做离线 render，不触发线上任务。
5. 启动独立隧道前确认 `/etc/x-post-media-repair-tunnel/id_ed25519_cpu_tunnel` 与 `known_hosts` 权限；remote 18788 必须空闲。保留现有 18787。
6. CPU 先以 curl 通过 18788 读取 catalog，再提交非生产 canary；验证 recipe/output identities。
7. 只有上述通过后才切 CPU `GPU_VIDEO_WORKER_URL` 并重启相关服务。
8. YouTube worker 先保持 disabled；确认 1479 eligible list、临时存储权限，并核对 source allowlist 保持精确两项 `advertising-1306474899.cos.ap-hongkong.myqcloud.com,ai.yingliangads.com` 后才能启用。该 allowlist 来自 CPU SQLite 当前 20 个 done jobs 的只读证据，不得添加通配符。不得用真实发布做 health check。
9. 启动 worker 前执行应用自带 `ensure_storage()`；它通过 `PRAGMA table_info` 检查并在旧 SQLite 表缺列时仅执行 `ALTER TABLE drama_youtube_publish_task ADD COLUMN lease_generation INTEGER NOT NULL DEFAULT 0`。先备份 SQLite，并只读核验旧行 generation=0；不需要独立 MySQL DDL。
10. disabled worker 下用 fake client 演练 token 频道 mismatch 和两 worker generation reclaim；确认没有真实 Google mutation，再进入后续授权 gate。
11. 公网已存在 `/s2l/1.html`。CDN owner 必须先核对既有 ID 命名空间和不可变对象；短链 publisher 未落实时 `DRAMA_SHORT_LINK_ROOT` 留空，对用户显示明确失败。

## 验证

- CPU/HK commit 相同；服务 `NRestarts` 稳定。
- `/api/gpu-video/random-overlay/catalog` token 门禁正确，manifest SHA 精确。
- 新任务四选框默认空；零输出拒绝；原三输出 smoke 正常。
- 自动/手动任务的冻结 recipe 在 retry 前后不变。
- API/SQLite 中不存在 OAuth secret；浏览器 DTO 不含 session URI。
- YouTube 仅通过 fake/disabled worker 验证队列，生产 smoke 禁止真实发布。

## 回滚

1. CPU 将 `GPU_VIDEO_WORKER_URL` 恢复旧值 `http://127.0.0.1:18787`，重启 API/job worker 并验证原三产物。
2. 停止/禁用 HK 新 tunnel 和 renderer；不改旧 GPU 服务或资产。
3. 停止/禁用 YouTube worker；ledger 保留，unknown 项禁止自动重试。
4. 部署前一 Git commit 回滚 CPU/static；不删除 additive SQLite 表。
5. 短链文件一旦发布不可变；回滚不能将同一 ID 指向另一目标。若 publisher canary 有误，停止新发布并由 CDN owner 处置缓存/对象。

## 发布阻断条件

- 当前外部 release blocker（须关闭）：
  - `page.dramabuzzs.com` 的 writer/owner 与受审计写入路径未落实，且既有数字 ID 命名空间尚未由 owner 冻结/核对。
- source allowlist 已关闭：CPU SQLite 只读检查当前 20 个 done jobs，仅发现 `advertising-1306474899.cos.ap-hongkong.myqcloud.com` 和 `ai.yingliangads.com`；生产配置只能使用这两个 hostname，不得通配。
- 条件部署 gate：CPU/HK baseline 无漂移；资产 hash 全量一致；18788 无冲突；备份与回滚点就绪；候选 commit 与已审核 commit 一致；秘密扫描无命中。
- 独立 QA 已对 `25b8af9` 给出代码 PASS，未发现候选 P0/P1。该结论不关闭上述短链外部 blocker，也不等于 production release PASS。
- blocker 和 gate 全部满足后，GitHub-first production deployment 已获根授权；真实 YouTube publish/comment 仍保持单独禁止，除非另有精确授权。
