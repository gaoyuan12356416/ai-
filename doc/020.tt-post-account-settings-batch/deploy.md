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
- 备份必须位于已确认挂载的数据盘，不得写入未挂载目录或根盘。
