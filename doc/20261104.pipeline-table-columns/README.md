# 渠道处理效率表列顺序及成功计数

## 需求与实现

用户要求列顺序为：预约中、待执行、执行中、已完成、失败、吞吐、平均耗时、最老等待，保留渠道/阶段首列。

真实成功计数使用现有 `completed`：制作排除 `metadata_only`，发布来自唯一平台成功回执 `pipeline_publication`。失败及未解决异常使用 `abnormal` 并列出原原因；不把待核对写成原生失败，不改领取、账号锁、重试和发布事实。吞吐单位为条/小时，均值列只显示平均值。

截图显示旧 HTML 表头和新 JS 数据顺序混用：异常落入“已完成”、真实完成落入“失败/待核对”。线上静态文件已是 bc70cd5，查询接口 2026-11-04 10:48 北京时间确认 Facebook 发布 completed=0、abnormal=145（unknown=141、blocked_by_unknown=4）。本次表头和数据列共用 JS 列定义，每次刷新重建表头，兼容旧 HTML；HTML 也同步顺序并更新脚本缓存版本。

## 验证与评审

- `node --check static/pipeline-monitor.js`、`git diff --check` 通过。
- `python -X utf8 -m unittest scripts.test_pipeline_queue_states scripts.test_pipeline_monitor`：24 项通过，覆盖权限、统计窗口及真实回执计数。
- `scripts/test_pipeline_monitor_ui.js` 在真实 Chrome 使用本地模拟接口通过。验证每列位置和值、完成 0/异常 145、旧表头自动修复、管理员/普通用户、未登录、刷新、移动端和旧深链。截图已检查。
- 只修改 HTML、JS 和 UI 测试；无需改后端统计或重启任何服务。业务数值仍随任务正常执行变化。

## 部署与回滚

隔离分支 `codex/pipeline-table-columns-20261104` 基于 2923e45；两份线上静态文件已逐字节核对为 bc70cd5（本地 CRLF 规范为 Git LF 后一致）。

先提交并 push GitHub，然后 CPU 43.166.187.96 从 GitHub fetch 精确 SHA，运行该 SHA 的 `scripts/deploy_pipeline_monitor_static.py --sha TARGET --expected-sha bc70cd5ab35aa32770eb066527d1d559c7fa85bf` 预检，通过后加 `--apply`。

只更新 `/root/drama_material_service/static` 与 `/usr/share/nginx/html` 的监控静态资源；JS/CSS 先于 HTML，备份保留在数据盘。CSS 字节不变。公开 HTTPS 文件逐字节核对，服务主 PID 和容量配置指纹前后核对。

回滚使用同一脚本反向版本保护：`--sha bc70cd5ab35aa32770eb066527d1d559c7fa85bf --expected-sha TARGET --apply`。仅回退这些静态文件，不回退数据库。若线上文件已被后续任务修改，保护检查应停止，不能覆盖他人改动。

技能上下文无需更新，本次为局部展示修复。
