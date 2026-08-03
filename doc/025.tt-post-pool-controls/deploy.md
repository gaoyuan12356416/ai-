# 部署与回滚

## 范围

仅 CPU 端：

- `features/tt_posts/core.py`
- `features/tt_posts/service.py`
- `static/tt-post-pool.html`
- TT CPU sidecar release 与 AI 后台、Nginx 的页面副本

GPU worker、GPU release、COS、媒体制作 profile 和环境变量不变。

## 部署前

1. 从 GitHub 已推送的精确提交创建不可变 CPU release。
2. 核对 `/mnt/data-disk` 的挂载 UUID、空间和写权限。
3. 记录当前 release、服务/timer、正式门禁、排期、可用素材、队列、run 和 publish ID 基线。
4. 使用 SQLite online backup 备份生产库，并备份 current 指针、服务代码和两份公开页面。
5. 候选 release 运行 TT core/service/UI/app contract 回归及 Python 编译。

## 部署

1. 原子切换 `/opt/tt-post/current` 到精确提交 release。
2. 从同一提交同步 `tt-post-pool.html` 到 AI 后台静态目录和 Nginx 公共目录，并核对三份 SHA-256。
3. 重启 `tt-post-service.service`；本次 `app.py` 无变化，不覆盖共享 monolith。
4. 验证 CPU sidecar、主 API、runner/prepare 单元及 `127.0.0.1:18830` GPU 隧道。
5. 完成登录态只读验收；不得提交排期开关或触发真实发布。

## 回滚

1. 将 `/opt/tt-post/current` 原子切回部署前 release。
2. 恢复备份的 TT 页面到 AI 后台静态目录和 Nginx 公共目录。
3. 重启 `tt-post-service.service`，核对健康、数据库完整性和页面哈希。
4. 默认不恢复 SQLite：本次无 schema 变更，正常部署不会修改业务数据。只有确认数据库损坏且停掉所有 TT writer 后，才使用在线备份恢复。

## 生产记录

- 上线时间：`2026-07-31 18:32:45 CST`。
- GitHub 功能提交：`2c8c5428474b907155d22f6b4733f9a7240eaf8e`，分支 `codex/tt-post-one-shot-canary-20260731`。
- CPU release：从 `/opt/tt-post/releases/a8d82571f17fb413016463d77acfc7f12e3d3013` 切换到 `/opt/tt-post/releases/2c8c5428474b907155d22f6b4733f9a7240eaf8e`。
- 回滚备份：`/mnt/data-disk/tt-post-publisher/backups/20260731T103245Z-a8d8257-to-2c8c542-tt-pool-controls`。
- 候选 release 共 `281/281` 个 TT 自动化用例通过；Python 编译和 `git diff --check` 通过。
- `/opt/tt-post/current/static/tt-post-pool.html`、`/root/drama_material_service/static/tt-post-pool.html` 和 `/usr/share/nginx/html/tt-post-pool.html` 的 SHA-256 均为 `99c71e8316b184b02566e6cb0b74fb8d6cb3d219be7f71272db75a50ecf8cc85`。
- `tt-post-service.service` 仅重启受影响的 CPU sidecar；`app.py` 未变化，主 API 未重启。GPU release、COS、媒体制作 profile 和 GPU 环境均未修改。
- 上线后 CPU sidecar、runner/prepare timer 与 path、GPU 隧道健康；每分钟自然 runner 均为 `status=ok`，没有由部署验收创建发布请求。
- 上线时 SQLite `integrity_check=ok`；排期 `1`、素材池 `5`、队列 `3`、schedule run `3`、素材 intake `4`。账号 `640` 保持 `enabled=1`、每天 `11:00`、版本 `1`，发布队列仍为已发布 `2`、失败 `1`。
- 登录态验收只操作本地草稿：取消勾选后按钮变为“关闭自动发布”，刷新制作状态不会覆盖草稿；整页刷新后服务端排期仍为启用。未点击“关闭自动发布”或“立即发布一条”。

## 2026-08-03 复核

- 生产仍运行精确 release `2c8c5428474b907155d22f6b4733f9a7240eaf8e`，三份页面哈希未变化；相关服务、timer、path 和 SocialKit 每小时同步均健康。
- 账号 `640` 当前可确认，账号发布设置仍为所有人可见并允许评论、Duet、Stitch；自动发布仍为每天 `11:00`、版本 `1`。
- 取消勾选后“关闭自动发布”按钮可用，制作状态刷新后草稿保持；整页刷新后恢复服务端已启用状态。复核没有保存开关或触发发布。
- 当前可立即发布素材为 `0`、制作中为 `0`，所以“立即发布一条”按预期禁用并明确提示素材池没有可立即发布素材。
- `2026-08-01 11:00` 的自然排期已公开发布素材 `5801636`，使用所有人可见并允许评论、Duet、Stitch；当前任务总数 `4`、已发布 `3`、失败 `1`。该任务不是部署验收触发的手动发布。
