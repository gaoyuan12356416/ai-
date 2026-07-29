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
TT_POST_LIVE_ENABLED=0
TT_POST_DIRECT_AUDIT_APPROVED=0
TT_POST_URL_PROPERTY_VERIFIED=0
```

## 数据库变更

不修改 MySQL。CPU 首次启动在独立 SQLite 中幂等创建 `tt_post_material_pool`、`tt_post_queue`、`tt_post_event`。

## 部署步骤

1. 将整合分支推送 GitHub，记录 commit SHA。
2. CPU/GPU 分别从相同 commit 建立 immutable release。
3. 备份 CPU 现有 `app.py`、静态文件、nginx 目标文件和环境；不修改 X SQLite。
4. GPU 上传并校验固定片尾与圆角 Logo SHA，创建 `/data/tt-post-publisher`。
5. GPU 安装并启动 loopback sidecar和反向隧道；确认 CPU `127.0.0.1:18830`。
6. CPU 安装 TT sidecar、claim/runner timer，初始化 SQLite。
7. 合并部署主后台路由和静态页，同时同步服务目录与 `/usr/share/nginx/html`；安装主 API 的 TT EnvironmentFile 和 systemd drop-in。
8. 仅重启相关新服务和主 API；不重启 X sidecar。
9. 保持三重 gate 为 0，执行关闭态验收。

## 验证步骤

1. CPU/GPU release symlink指向目标 SHA。
2. GPU `/health`、CPU 内部健康和反向隧道均通过。
3. SQLite `PRAGMA integrity_check=ok`。
4. 公网 `/tt-post-pool.html` 为 200，登录后账号数与只读快照一致。
5. 通过素材预览触发 GPU 成片并确认文件位于 `/data`，不创建测试队列。
6. 确认三项门禁与品牌媒体门禁均为关闭态，TikTok init 调用计数为 0。
7. 搜索日志、SQLite、manifest，确认无 Token。
8. X 发布池页面、timer和最近任务保持正常。

## 回滚方案

1. 停止并禁用 TT claim/runner、CPU sidecar、GPU sidecar和 18830 隧道。
2. 主 API和静态页恢复部署前备份或切回上一 immutable release。
3. CPU/GPU symlink切回各自上一 release。
4. TT SQLite保留只读审计，不删除；回滚不触碰 X SQLite和快照同步。
5. 验证主 API、X sidecar、X timers和 18820 隧道。

## 注意事项

- 禁止在命令行、systemd 状态、journal、调试响应中输出真实 Token。
- 禁止将 `TT_POST_LIVE_ENABLED` 单独打开；三重 gate 必须全部满足且经过独立变更审批。
- 当前品牌片尾只允许关闭态成片/人工流程验收，不代表 TikTok Direct Post 合规；其 manifest 固定为 `direct_post_eligible=false`，不能靠修改三重 gate 绕过。

## 2026-07-29 部署记录

- GitHub 分支：`codex/tiktok-post-pool-20260729`
- CPU 当前运行提交：`2fd07d3e1a6a7ef13982263cbf44297ef4a94156`
- CPU 当前 release：`/opt/tt-post/releases/2fd07d3`
- GPU 当前 release：`/opt/tt-post-gpu/releases/18148b2`
- 本次 CPU 更新前备份：`/root/tt-post-backups/20260729T170730+0800-18148b2-fixed-caption`
- 页面：`https://ai.yingliangads.com/tt-post-pool.html`
- 已部署静态页 SHA-256：`7f298293d6a4202687d3db7809cb3aadca6fa59651d3731f6bbb9984de3ce7e2`
- 部署版本全回归：`212/212` 通过
- Direct Post 三重 gate：全部为 `0`
- 线上登录态浏览器验收：
  - 账号：`700` / `@dramawave998`
  - 素材：`5824343`
  - 真实 `content_id`：`Y9v1yQcFqM`
  - 描述框：`readonly=true`
  - 页面显示的描述与固定模板逐字一致，仅 Drama ID 使用真实 `content_id`
  - 发布池任务总数：`0`
- TikTok 发布初始化：`0`
- 真实 TikTok Post：`0`
- 实际 GPU/COS 成片：
  `https://advertising-1306474899.cos.ap-hongkong.myqcloud.com/tt-post-prepared/56/568fde32b0bde91935a12af7bf732ffe537be99cc0e5fea94a1a2091d72ed492.mp4`

本次更新只替换 CPU release 和后台静态页；GPU 继续运行
`/opt/tt-post-gpu/releases/18148b2`。三项 Direct Post 门禁未开启，浏览器验收未创建任务、未调用 TikTok 发布初始化接口，也未发布帖子。
