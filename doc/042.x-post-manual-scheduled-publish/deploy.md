# 部署文档

## 变更内容

- 手动弹窗新增立即/定时单次发布。
- SQLite 增加 manual timing 字段和素材 reservation 表/约束。
- 复用现有 `x-post-manual.timer`，不增加或手动触发发布 unit。

## 配置项

无新增 secret 或环境变量。时区固定 `Asia/Shanghai`，timer 仍为 15 秒轮询。

## 数据库变更

仅通过 `ensure_storage()` 增量执行：

- `x_post_manual_run`: `publish_mode`, `scheduled_at`, `scheduled_timezone`。
- `x_post_manual_material_reservation` 及索引/触发器。
- 上线门禁：active manual run/queue=0、unknown=0、在线备份 `integrity_check=ok`、迁移副本连续执行两次。

## 部署步骤

1. 推送精确 Git commit。
2. 记录 Sidecar/main/static/unit blob hash、release symlink、服务 PID 与 queue/log/Post 基线。
3. 验证 `/mnt/data-disk` 挂载和空间，创建在线 SQLite、token 非秘密 hash/mode、代码/static/unit 回滚包。
4. 在在线备份副本运行两次 `ensure_storage()`，校验旧行快照、索引、触发器和 integrity。
5. 从 GitHub commit 建 immutable release；先执行离线编译/测试。
6. 短暂停止 manual timer，确认没有 active manual run/queue；切换 Sidecar release，restart `x-post-automation.service`，先验证新 schema/internal health。
7. 把同一 Git `service.py`、`app.py` 和 accounts 模块同步到主 API runtime，restart `drama-material-api.service` 并验证 health。
8. 最后同步应用 static 与 `/usr/share/nginx/html/x-post-material-pool.html`，确认两份 hash 一致后恢复 manual timer。
9. 不创建任务、不启动 manual service；观察自然 `no_pending`。

## 验证步骤

- Sidecar `/health` 与 main `/api/ui/topbar` 均为 200，相关服务 active/enabled。
- 匿名合法 POST 仍到 Cookie gate；未知字段 fail closed。
- 两份 `service.py` 与 Git blob 一致，两个 static 页面 hash 一致。
- schema/索引/trigger 存在，旧 manual row 均为 immediate，integrity ok。
- queue/log/published/unknown/active/manual-run 计数与部署前一致。
- 登录态页面只读验证模式控件、北京时间输入和按钮文案；不点击最终提交。
- manual timer natural `no_pending`；schedule timers natural `no_due` 或正常计划状态。

## 回滚方案

1. 停止 manual timer，保持所有 X 发布账本和 token 不变。
2. 恢复上一 release symlink、主 API `service.py`/`app.py`、两处 static 和 unit 文件。
3. daemon-reload，窄重启 Sidecar/main API，恢复原 timer 状态。
4. 不删除新增列、reservation 表、索引或历史记录；旧代码忽略增量 schema。
5. 若上线后已存在 scheduled run/queue/Post，禁止恢复旧 SQLite；只回滚代码并保留 live ledger。

## 注意事项

- 不得用真实立即/定时任务作为部署 canary。
- 不得恢复已轮换 token 的备份为 active credential。
- 生产页面由 Nginx docroot 直接提供，漏同步 `/usr/share/nginx/html` 视为部署失败。
