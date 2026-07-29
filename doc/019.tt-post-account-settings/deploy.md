# 部署与回滚

## 部署范围

仅 CPU 服务器：

- AI 后台 `app.py`
- `features/tt_posts/`
- `static/tt-account-settings.html`
- `static/tt-post-pool.html`
- `static/quick-nav.js`
- `static/navigation.json`
- TT CPU sidecar 与 AI 后台服务重启

GPU 服务器、媒体制作 profile、GPU sidecar 和隧道均不变。

## 部署前

1. 确认 GitHub 分支提交和工作区清洁。
2. 确认 Direct Post 三重门禁均关闭。
3. 备份 `/mnt/data-disk/tt-post-publisher/tt-post.sqlite3`。
4. 备份当前 CPU release 指针、`app.py` 和四个静态文件。
5. 记录 TT/X 服务与 timer 状态、当前队列数量。

## 部署

1. 从已推送提交创建不可变 CPU release。
2. 安装/同步 Python 代码和静态资源。
3. 切换 release 指针。
4. 重启 `tt-post-service` 与 `drama-material-api`。
5. 不重启或修改 GPU 服务。

## 生产执行记录

- 部署日期：2026-07-29。
- 分支：`codex/tiktok-account-settings-20260729`。
- 功能提交：`9fd643137dd8d33e2ec8a804b333d5ec0584bbde`。
- 当前 release：`/opt/tt-post/releases/9fd6431`。
- 上一 release：`/opt/tt-post/releases/2fd07d3`。
- 备份目录：`/mnt/data-disk/tt-post-publisher/backups/20260729T173903+0800-2fd07d3-to-9fd6431-account-settings`。
- 备份 SQLite 在线副本完整性为 `ok`，部署前旧库三张表及原 release、后台文件、静态文件均已记录。
- 首次切换因服务就绪竞态触发自动回滚；确认无启动错误后改用 30 秒就绪轮询，第二次切换成功。
- GPU 服务器未修改、未重启。

## 验证

1. `tt-post-service`、`drama-material-api` 和既有 timer 为 active。
2. SQLite `PRAGMA integrity_check` 为 `ok`，新增表存在。
3. 三重门禁仍关闭。
4. 新页面与发布池返回 200，导航可见。
5. 登录态页面能读取账号列表；不保存真实设置。
6. 发布池对未配置账号显示管理提示。
7. 队列数量、GPU publish ledger 和 TikTok Post 数均未增加。
8. X 侧服务和定时器无回归。

生产验证结果：

- 个号管理页：`https://ai.yingliangads.com/tt-account-settings.html`，HTTP 200。
- 发布池：`https://ai.yingliangads.com/tt-post-pool.html`，HTTP 200。
- Chrome 登录态页面显示 18 个可用账号、0 个已配置账号。
- 账号 640 的 `creator_info` 只读检测成功；未保存。
- 发布池对同一未配置账号显示管理提示且禁止保存任务。
- 数据库完整性 `ok`，账号配置数 0、队列数 0。
- 三重门禁均为 0，未调用 GPU 制作或 TikTok 发布。
- TT 服务、后台服务、runner timer、X 服务及 X timers 均正常。

## 回滚

1. 将 `/opt/tt-post/current` 原子切回 `/opt/tt-post/releases/2fd07d3`。
2. 从上述备份目录恢复后台 `app.py`、服务静态文件和 Nginx 静态文件。
3. 重启 `tt-post-service.service`、`tt-post-runner.timer` 与 `drama-material-api.service`，再核对健康状态和旧页面。
4. 新增表可保留；旧 release 会忽略。
5. 若 SQLite 完整性异常，停 TT sidecar 后恢复部署前备份，再启动并检查。
