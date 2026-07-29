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

## 验证

1. `tt-post-service`、`drama-material-api` 和既有 timer 为 active。
2. SQLite `PRAGMA integrity_check` 为 `ok`，新增表存在。
3. 三重门禁仍关闭。
4. 新页面与发布池返回 200，导航可见。
5. 登录态页面能读取账号列表；不保存真实设置。
6. 发布池对未配置账号显示管理提示。
7. 队列数量、GPU publish ledger 和 TikTok Post 数均未增加。
8. X 侧服务和定时器无回归。

## 回滚

1. 切回上一 CPU release。
2. 恢复备份静态文件并重启两个 CPU 服务。
3. 新增表可保留；旧 release 会忽略。
4. 若 SQLite 完整性异常，停 TT sidecar 后恢复部署前备份，再启动并检查。
