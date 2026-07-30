# 部署文档

## 变更内容

仅更新 CPU TT Post release 和 AI 后台静态页：

- CPU sidecar 增加 durable material intake 和内部 prepare 状态机。
- 新增独立 `tt-post-prepare.service/path/timer`。
- preview/入池接口改为快速返回。
- 页面增加后台预制作状态。

本需求不修改 GPU release、不切换 GPU 存储后端、不打开 TikTok 发布门禁。

## 配置项

在既有 `/etc/tt-post.env` 中增加或核对：

```text
TT_POST_PREPARE_RUNNER_ID=tt-post-prepare-primary
TT_POST_PREPARE_LEASE_SECONDS=180
TT_POST_PREPARE_RENEW_INTERVAL_SECONDS=30
TT_POST_PREPARE_INTERNAL_TIMEOUT=60
TT_POST_GPU_PREPARE_TIMEOUT=9000
TT_POST_PREPARE_PROCESS_TIMEOUT=9300
TT_POST_PREPARE_RUNNER_LOCK_PATH=/run/tt-post/prepare-runner.lock
TT_POST_PREPARATION_KICK_PATH=/run/tt-post/prepare-kick
```

单位均为秒。必须满足：

- `lease_seconds >= 3 × renew_interval_seconds`
- `process_timeout >= gpu_prepare_timeout + 60`
- `tt-post-prepare.service TimeoutStartSec=9600s > process_timeout`
- lock/kick 必须位于 `/run/tt-post/`

继续保持：

```text
TT_POST_LIVE_ENABLED=0
TT_POST_DIRECT_AUDIT_APPROVED=0
TT_POST_URL_PROPERTY_VERIFIED=0
```

Token、MySQL 密码、GPU seal key 仍仅保存在 root-only `/etc/tt-post.secrets`，不得写入 Git、unit 或命令行。

## 数据库变更

SQLite `/mnt/data-disk/tt-post-publisher/tt-post.sqlite3` 以只增方式新增 `tt_post_material_intake` 及其索引。既有 `tt_post_recurring_pool`、queue、run 表不重建、不删字段、不放宽约束。

部署前：

1. 记录数据库大小、SHA256 和 `PRAGMA integrity_check`。
2. 使用 SQLite online backup 生成带 UTC 时间戳的备份。
3. 在备份副本上用新代码连续初始化两次，确认幂等且 `integrity_check=ok`。
4. 记录既有各表行数；不得因上线清理 queued/ready/history。

## 部署步骤

1. 完成自动化、SA review 与测试报告，提交并推送 GitHub；记录完整 commit SHA。
2. 读取当前 CPU `current` symlink、服务状态、静态页 SHA、三个 gate 值、SQLite 行数与最近 TT runner journal。
3. 备份：
   - `/opt/tt-post/current` 指向；
   - `/mnt/data-disk/tt-post-publisher/tt-post.sqlite3` online backup；
   - `/etc/tt-post.env`、`/etc/tt-post.secrets`（不输出内容）；
   - `/etc/systemd/system/tt-post-*.{service,path,timer}`；
   - `/root/drama_material_service/static/tt-post-pool.html`；
   - `/usr/share/nginx/html/tt-post-pool.html`。
4. 从目标 GitHub SHA 创建 immutable CPU release，不在服务器直接编辑源码。
5. 安装新 release 中：
   - `scripts/tt_post_prepare_runner.py`
   - `deploy/tt-post-prepare.service`
   - `deploy/tt-post-prepare.path`
   - `deploy/tt-post-prepare.timer`
6. 合并新增非秘密配置并校验 timeout 关系；保持三个 gate 为 0。
7. `systemctl daemon-reload`，先切换 CPU release，再重启 `tt-post-service.service`，触发 additive schema 初始化。
8. 校验数据库完整性、新表/索引和旧表行数。
9. 启用并启动 `tt-post-prepare.path` 与 `tt-post-prepare.timer`；不要停用/复用既有 `tt-post-runner.*`。
10. 同步静态页到 release、主服务静态目录和 Nginx 目录，核对三处 SHA 一致。
11. 执行关闭态 canary：快速校验一条未使用素材，加入 queued，观察独立 runner 转为 ready。不得调用 run-now 或真实 publish。
12. 观察至少一个 timer 周期，确认 idle tick 正常且既有 publish runner/timer 未受阻。

## 验证步骤

### 服务与配置

- `tt-post-service.service` active。
- `tt-post-prepare.path`、`tt-post-prepare.timer` active；timer 下一次触发时间存在。
- `tt-post-runner.path/timer` 继续 active，未因 prepare 长任务处于阻塞。
- CPU release symlink 对应 GitHub 精确 SHA，服务器 release 无热改。
- `127.0.0.1:18829` 正常；内部 prepare 路径未暴露公网。
- journal 无 bearer、Token、seal key 或完整敏感 URL。

### 数据库

- `PRAGMA integrity_check=ok`。
- `tt_post_material_intake` 表、唯一索引和状态约束存在。
- 旧表部署前后行数与状态无异常减少。
- canary 入池先为 queued，完成后 intake ready 且 linked recurring pool available；无重复行。

### 功能

- preview 在预期短时内返回 `validated/not_started`，同时 GPU job/manifest 数不因 preview 增加。
- material-pool POST 立即返回 `queued/publish_ready=false`。
- path kick 能唤醒 runner；删除/忽略 kick 后 timer 仍可兜底。
- 页面刷新后仍能看到 queued/preparing/ready。
- ready 之前 available count 不增加，ready 后只增加 1。
- 同请求重放返回同一 intake，不新增 GPU job。

### 禁止真实发布的证据

- 三个 gate 均为 0。
- canary 不调用 `/run-now`、due publish 或 TikTok init。
- TT queue/publish event/publish ID 在 canary 前后无新增真实发布记录。
- GPU 仅执行 prepare；如现有后端为 COS，可产生新成片对象，但不得产生 TikTok 帖子。

## 回滚方案

1. 保持三个发布 gate 为 0。
2. 停止并禁用 `tt-post-prepare.path`、`tt-post-prepare.timer`；等待/停止当前 prepare oneshot，记录其 intake ID 和状态。
3. 将 CPU `current` symlink 切回部署前 release，恢复旧 systemd unit/环境备份并 daemon-reload。
4. 重启旧 `tt-post-service.service`，恢复旧静态页三处文件。
5. 新增 intake 表保留只读审计，不 drop、不改写成 ready。旧 release 会忽略它，不影响既有 recurring pool。
6. 若某 intake 已原子完成为 ready，其 recurring pool 行遵循既有发布池规则保留；不得删除以“回滚”历史。
7. 核对 SQLite integrity、旧表行数、主 API、TT account/settings、既有 runner/path/timer。
8. 记录回滚原因、时间、release SHA、备份路径、受影响 intake/material ID。

## 注意事项

- path kick 只用于低延迟，timer 才是漏触发兜底；两者必须与同一个 prepare service 配套。
- oneshot 不得声明/清理 sidecar 持有的同名 `RuntimeDirectory`，避免删除 `/run/tt-post/prepare-kick`。
- 不得把 prepare runner 合并进到点发布 runner。
- 不得为了 canary 打开任何 TikTok gate、点击立即发布或制造真实帖子。
- 不得在活跃 prepare 时删除 SQLite 行、GPU job、COS 对象或 claim 状态。
- 若回滚后保留 queued intake，后续重新部署新版本会继续恢复处理，这是预期的 durable 行为。
