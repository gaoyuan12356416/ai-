# 部署文档

## 变更内容

- 批量能力检测与原子保存 API。
- 个号管理页批量选择和统一设置模式。
- AI 后台同源代理与安全审计。

## 配置项

无新增环境变量。继续使用现有 TT sidecar、账号快照、GPU `creator_info` 和三重发布门禁。

## 数据库变更

无 schema 变更。批量保存复用 `tt_post_account_setting`，事务失败时整批回滚。

## 部署步骤

1. 本地测试和代码评审通过。
2. 提交并推送 GitHub 分支。
3. 记录当前 CPU release、服务、门禁、账号配置版本和队列数量。
4. 在数据盘创建在线 SQLite 备份和旧 release/静态文件备份。
5. 从精确 GitHub 提交创建不可变 `/opt/tt-post/releases/<commit>`。
6. 运行生产 TT 测试，切换 `/opt/tt-post/current`。
7. 同步 `app.py`、服务静态页和 Nginx 静态页。
8. 仅重启 `tt-post-service.service` 和 `drama-material-api.service`；timer 保持启用。
9. 不修改或重启 GPU。

## 验证步骤

1. 服务、timer、X 发布相关服务状态正常。
2. SQLite 完整性 `ok`，账号 640 的版本与队列数部署前后不变。
3. 三重 Direct Post 门禁仍为 0。
4. 新增接口未登录返回 401。
5. Chrome 登录态进入批量模式，选择多个账号并完成只读能力检测。
6. 不点击批量保存，复核数据库账号配置和队列 0 增量。
7. 发布池和单账号编辑回归正常。

## 回滚方案

1. 将 `/opt/tt-post/current` 原子切回上一 release。
2. 恢复备份的 `app.py`、服务静态页和 Nginx 静态页。
3. 重启 `tt-post-service.service`、`tt-post-runner.timer` 和 `drama-material-api.service`。
4. 批量功能无 schema 变更，正常代码回滚无需恢复数据库。
5. 仅在 SQLite 完整性异常时停写后恢复在线备份。

## 注意事项

- 生产浏览器验收禁止点击“批量保存”。
- 审计不得记录 Token、Authorization 或 GPU 内部凭据。
- SQLite 与主要回滚证据必须位于已确认挂载的数据盘；共享 monolith 另按 live feature guard 在 `/root/drama_material_service/backups/` 保留完整文件备份。

## 生产执行记录

- 日期：2026-07-29。
- 分支：`codex/tiktok-account-settings-20260729`。
- GitHub 提交：`779ac3bdb1f14031eac1ca1ee353bfa0a883c9c7`。
- 当前 CPU release：`/opt/tt-post/releases/779ac3b`。
- 上一 CPU release：`/opt/tt-post/releases/9fd6431`。
- 数据盘 UUID：`3e8ac4e8-7770-456d-9e89-2ec5dd405fa8`。
- 数据与运行文件备份：`/mnt/data-disk/tt-post-publisher/backups/20260729T182910+0800-9fd6431-to-779ac3b-account-settings-batch`。
- 共享后台完整备份：`/root/drama_material_service/backups/20260729T182955+0800-pre-tt-account-settings-batch`，共 198 个文件。
- 发布前 SQLite 在线备份完整性为 `ok`；原 release、后台文件和浏览器静态页均已记录。
- 候选与部署后 live feature guard 均通过：4 个功能、16 条文件规则。
- 服务器 TT 自动化 138/138 通过后才执行原子切换。
- 仅重启 `tt-post-service.service` 和 `drama-material-api.service`；runner timer 保持启用，GPU 未连接、未修改、未重启。

## 生产验证结果

- `tt-post-service.service`、`tt-post-runner.timer`、`drama-material-api.service` 均为 active。
- `x-post-automation.service`、`x-post-schedule.timer`、`x-post-schedule-claim.timer` 均为 active。
- 个号管理页和发布池 HTTP 200；两个新增接口未登录请求均返回 401。
- SQLite `PRAGMA integrity_check=ok`；账号设置行数 1，账号 640 保持版本 1、更新时间不变，队列数 0。
- `TT_POST_LIVE_ENABLED`、`TT_POST_DIRECT_AUDIT_APPROVED`、`TT_POST_URL_PROPERTY_VERIFIED` 均为 0。
- 登录态页面显示 17 个当前可发布账号、0 个当前列表内已配置账号。
- 批量选择 17 个待配置账号，实时检测成功；共同最长视频 600 秒，共同观看范围为所有人、互相关注的朋友、仅自己。
- 评论、Duet、Stitch 均可由运营选择；批量默认不继承任何账号设置，观看范围为空，互动和披露全部关闭。
- 未选择观看范围时“批量保存”保持禁用；验收直接退出批量模式，未提交保存。
- 验收后账号 640 版本、设置行数、队列数和三重门禁再次核对均无变化。
