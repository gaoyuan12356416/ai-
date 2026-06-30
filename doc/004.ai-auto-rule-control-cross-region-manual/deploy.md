# 部署文档

## 变更内容
- AI 自动规则调控拆页。
- +8 跨区账户池批量创建向导。
- +8 跨区关停规则模板。
- 跨区调控绑定向导与策略保存。
- Preview 表格展示 `country`、`account_time_zone`、策略摘要。

## 配置项
- `DRAMA_JOB_DB_PATH`：作业状态库，线上实际路径为 `/root/drama_material_service/data/drama_material_jobs.sqlite3`。
- Meta token 配置仍通过调控中心 Token 页面维护。
- 本次不启用自动 runner，无新增 cron。

## 数据库变更
- `ad_control_rule_set`。
- `ad_control_account_group`。
- `ad_control_rule_group.rule_set_id`。
- `ad_control_rule_group.strategy_json`。
- `ad_control_action` 不因部署自动写入。

## 部署步骤
已按 GitHub-first 流程完成：
1. 本地验证。
2. commit/push 到 `codex/ai-auto-rule-control`。
3. 服务器拉取精确 commit。
4. 备份当前发布目录。
5. 部署 release。
6. 窄重启 `drama-material-api.service`。
7. 线上只读验证。

## 验证步骤
- `python -m py_compile app.py`
- `node --check static/quick-nav.js`
- `node --check static/ad-control-pages.js`
- `git diff --check`
- HTTP 检查 7 个页面和公共资源均返回 200。
- Playwright 检查公共顶吸、快速导航对象和导航渲染。
- 查询状态库：
  - `SELECT COUNT(*) FROM ad_control_action;` 返回 `0`。
  - `SELECT COUNT(*) FROM ad_control_rule_group WHERE COALESCE(enabled,0)=1 AND COALESCE(deleted,0)=0;` 返回 `0`。
- 查看 `journalctl -u drama-material-api.service`，无异常堆栈和 execute 写入日志。

## 回滚方案
- 回滚点：`/root/drama_material_service/backups/ad-control-cross-region-manual-20260630_181025-before-bcde108`
- 当前 release：`/root/drama_material_service/releases/ai-auto-rule-control-bcde108bef92fb733af03a8eebeb67d05dfd9571`
- 如需回滚：
  1. 停止 `drama-material-api.service`。
  2. 恢复备份目录到 `/root/drama_material_service`。
  3. 启动服务。
  4. 重新验证 `/api/ui/topbar`、七个页面和日志。

## 注意事项
- 线上状态库不是 `/root/drama_material_service/data/app.db`，而是 `/root/drama_material_service/data/drama_material_jobs.sqlite3`。
- 本次测试不做真实 execute，不会关闭广告。
- 后续启用 runner 前必须补同日禁止重启和隔天重启的执行链路测试。
