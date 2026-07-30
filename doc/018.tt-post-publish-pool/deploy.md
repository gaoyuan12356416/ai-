# 部署文档

## 变更内容

CPU 新增 AI 平台 TT 发布池、独立 sidecar/SQLite 和定时执行器；GPU 新增数据盘成片与 TikTok API sidecar及反向隧道。首发保持真实 Direct Post 关闭。

## 配置项

CPU root-only 环境：

```text
TT_POST_SERVICE_HOST=127.0.0.1
TT_POST_SERVICE_PORT=18829
TT_POST_INTERNAL_URL=http://127.0.0.1:18829
TT_POST_DB_PATH=/mnt/data-disk/tt-post-publisher/tt-post.sqlite3
TT_POST_MYSQL_HOST=101.32.56.53
TT_POST_MYSQL_PORT=63350
TT_POST_ACCOUNT_MYSQL_DATABASE=ads_ai
TT_POST_MATERIAL_MYSQL_DATABASE=kunlunads_dev
TT_POST_MYSQL_USER=<reader>
TT_POST_MYSQL_PASSWORD=<root-only>
TT_POST_GPU_URL=http://127.0.0.1:18830
TT_POST_INTERNAL_TOKEN=<CPU sidecar root-only bearer>
TT_POST_GPU_INTERNAL_TOKEN=<GPU sidecar root-only bearer>
TT_POST_GPU_CREDENTIAL_SEAL_KEY_B64=<root-only AES-GCM key>
TT_POST_LIVE_ENABLED=0
TT_POST_DIRECT_AUDIT_APPROVED=0
TT_POST_URL_PROPERTY_VERIFIED=0
```

主 API 使用独立的 root-only `/etc/tt-post-app.env`：

```text
TT_POST_ADMIN_SERVICE_URL=http://127.0.0.1:18829
TT_POST_ADMIN_TIMEOUT=600
TT_POST_INTERNAL_TOKEN=<与 CPU sidecar 相同的 root-only bearer>
```

将 `deploy/drama-material-api-tt-post.conf` 安装为
`/etc/systemd/system/drama-material-api.service.d/60-tt-post.conf`。缺少该
EnvironmentFile 时主 API 本身保持健康，但 TT 路由会 fail-close 为未配置。

GPU root-only 环境：

```text
TT_POST_GPU_ENABLED=1
TT_POST_GPU_HOST=127.0.0.1
TT_POST_GPU_PORT=8830
TT_POST_GPU_WORK_ROOT=/data/tt-post-publisher
TT_POST_GPU_FFMPEG_BIN=/opt/ffmpeg-nvenc/ffmpeg
TT_POST_GPU_FFPROBE_BIN=/opt/ffmpeg-nvenc/ffprobe
TT_POST_GPU_FIXED_OUTRO_PATH=/data/tt-post-publisher/assets/TT-new-outro.mp4
TT_POST_GPU_LOGO_PATH=/data/tt-post-publisher/assets/dramawave-logo-rounded.png
TT_POST_DEFAULT_SOURCE_TRIM_TAIL_SECONDS=4.333333
TT_POST_GPU_INTERNAL_TOKEN=<root-only>
TT_POST_GPU_CREDENTIAL_SEAL_KEY_B64=<same root-only AES-GCM key>
TT_POST_GPU_COS_BUCKET=<root-only deployment value>
TT_POST_GPU_COS_REGION=<root-only deployment value>
TT_POST_GPU_COS_DOMAIN=<HTTPS public origin>
TT_POST_GPU_COS_PREFIX=tt-post-prepared
TT_POST_GPU_MAX_DURATION_SECONDS=3600
TT_POST_LIVE_ENABLED=0
TT_POST_DIRECT_AUDIT_APPROVED=0
TT_POST_URL_PROPERTY_VERIFIED=0
```

## 数据库变更

不修改 MySQL。CPU 在独立 SQLite 中保留旧四表，并以只增方式新增：

- `tt_post_daily_schedule`
- `tt_post_recurring_pool`
- `tt_post_schedule_run`

部署前必须使用 SQLite online backup；在备份副本上连续初始化两次并确认 `PRAGMA integrity_check=ok` 后才允许切换 release。新排期默认没有记录且 `enabled=0`，升级本身不会自动发布。

## 部署步骤

1. 将整合分支推送 GitHub，记录 commit SHA。
2. CPU/GPU 分别从相同 commit 建立 immutable release。
3. 备份 CPU 现有 `app.py`、静态文件、nginx 目标文件和环境；不修改 X SQLite。
4. GPU 上传并校验固定片尾与圆角 Logo SHA，创建 `/data/tt-post-publisher`。
5. GPU 安装并启动 loopback sidecar和反向隧道；确认 CPU `127.0.0.1:18830`。
6. CPU 安装 TT sidecar、runner timer 与 `tt-post-runner.path`，初始化 SQLite；path 仅监控 `/run/tt-post/manual-kick`，与 timer 共用同一个 runner/flock。
7. 合并部署主后台路由和静态页，同时同步服务目录与 `/usr/share/nginx/html`；安装主 API 的 TT EnvironmentFile 和 systemd drop-in。
8. 仅重启相关新服务和主 API；不重启 X sidecar。
9. 保持三重 gate 为 0，执行关闭态验收。

## 验证步骤

1. CPU/GPU release symlink指向目标 SHA。
2. GPU `/health`、CPU 内部健康和反向隧道均通过。
3. SQLite `PRAGMA integrity_check=ok`。
4. 公网 `/tt-post-pool.html` 为 200，登录后账号数与只读快照一致。
5. 通过素材预览触发 GPU 成片并确认文件位于 `/data`；素材 4665764（2087 秒）能通过 TT 预校验，且 X 140 秒回归不变。
6. 确认三项门禁与品牌媒体门禁均为关闭态，TikTok init 调用计数为 0。
7. 关闭态保存每日时间与素材池后，手动按钮明确显示阻断，不消费素材、不创建可执行 queue。
8. 搜索日志、SQLite、manifest，确认无 Token。
9. X 发布池页面、timer和最近任务保持正常。

## 回滚方案

1. 停止并禁用 `tt-post-runner.path`、TT runner timer 和 CPU sidecar；停止 path 前先删除无业务数据的 `/run/tt-post/manual-kick`。
2. 主 API和静态页恢复部署前备份或切回上一 immutable release。
3. CPU/GPU symlink 分别切回部署前记录的 immutable release。
4. TT SQLite 新三表和已生成 run/queue 保留只读审计，不删除、不降表；回滚不触碰 X SQLite和快照同步。
5. 验证主 API、X sidecar、X timers和 18820 隧道。

## 注意事项

- 禁止在命令行、systemd 状态、journal、调试响应中输出真实 Token。
- 禁止将 `TT_POST_LIVE_ENABLED` 单独打开；三重 gate 必须全部满足且经过独立变更审批。
- 当前品牌片尾只允许关闭态成片/人工流程验收，不代表 TikTok Direct Post 合规；其 manifest 固定为 `direct_post_eligible=false`，不能靠修改三重 gate 绕过。
- TT GPU 全局制作上限为 3600 秒，但最终权威仍是所选账号实时 `max_video_post_duration_sec`；不得同步放宽 X 的 140 秒素材合同。

## 2026-07-29 部署记录

- GitHub 分支：`codex/tiktok-post-pool-20260729`
- CPU 当前提交：`5cfc65715c8d6b91653c9ed8c397b823a6d3bd4d`
- CPU 当前 release：`/opt/tt-post/releases/5cfc657`
- 切换时间：2026-07-29 18:48:36 CST
- 切换前 CPU release：`/opt/tt-post/releases/779ac3b`
- GPU 当前 release：`/opt/tt-post-gpu/releases/18148b2`
- 本次 CPU 更新前备份：`/root/tt-post-backups/20260729T183935+0800-9fd6431-batch-caption`
  - 目录名包含 `9fd6431`，但备份中 `current` 实际捕获的是 `/opt/tt-post/releases/779ac3b`，回滚以实际捕获的 release 为准。
- 页面：`https://ai.yingliangads.com/tt-post-pool.html`
- TT 发布池静态页 SHA-256：`5eb01246d3e2c8b5ba619f70ffa89132bd5879c59656fa63d3b1c5acfde68cea`，release、主服务静态目录、nginx 三处一致
- TT 个号设置页 SHA-256：`54a73f9fa26f827ff80b3e447c49ee7f62ec12c258aace9b34c4dd6dd64ce88f`，本次未改变
- 部署版本全回归：`275/275` 通过
  - TT：`154/154`（Core 38、Service 52、GPU 25、发布池 UI 18、个号设置 UI 11、App contract 10）
  - X：`93/93`
  - 素材状态：`28/28`
- Direct Post 三重 gate：全部为 `0`
- 线上登录态浏览器验收：
  - 批量素材框可用，规范化后 20 位 ID 在前端即被拦截
  - 当前默认描述模板完整显示且允许编辑
  - 排期间隔默认 10 分钟
  - 发布池只读消费 TT 个号设置，未配置设置时建队按钮禁用
  - 既有 TT 个号设置原子批量保存能力保留
  - 验收未创建任务
- 公网响应：200，`Cache-Control: no-store`
- SQLite：`PRAGMA integrity_check=ok`，`material=0`、`queue=0`、`event=0`、`settings=1`
- TikTok 发布初始化：`0`
- 真实 TikTok Post：`0`

本次更新只替换 CPU release 和后台静态页；GPU 继续运行
`/opt/tt-post-gpu/releases/18148b2`。三项 Direct Post 门禁未开启，浏览器验收未创建任务、未调用 TikTok 发布初始化接口，也未发布帖子。
