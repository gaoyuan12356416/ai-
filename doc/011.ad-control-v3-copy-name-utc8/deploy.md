# 部署文档

## 变更内容

V3 三层复制名称后缀、UTC+8 API/UI/日期筛选/runner 日志。

## 配置项

无新增配置。固定业务展示时区 `Asia/Shanghai`（UTC+8）。

## 数据库变更

无 DDL、无历史回填。UTC 存储保持不变。

## 部署步骤

1. 从精确复现线上组合态的 GitHub 合并基线 `7f9cdf0` 开发；其 app 来自 `4ecaa75`，V3 runtime 来自 `63f1d1c`。
2. 本地精确测试、commit、push；服务器只从 GitHub 获取目标提交。
3. 发布窗口暂停 V3 runner timer，记录发布前状态；紧急停止新复制时关闭 `AD_CONTROL_V3_LIVE_COPY_ENABLED` 与 `AD_CONTROL_V3_RUNNER_LIVE_RELEASED`，不影响 pause/observe。
4. 在数据盘备份 app、V3 runtime、runner、unit/env、SQLite 和状态/hash。
5. 服务器目标提交完整测试；exact deployer `--check` 必须为 `would_change`。
6. 应用 overlay，只重启 `drama-material-api.service`；验证完成后恢复 runner timer 的发布前状态。
7. 重复 overlay 检查 `unchanged`，验证 live flags、HTTP、日志和 V2/playable guard。

## 验证步骤

- Stub 三层复制命名和失败隔离。
- API 时间偏移、响应头、UTC+8 日期 SQL 边界。
- 浏览器规则列表/执行日志显示 UTC+8。
- 生产不主动创建 ACTIVE 广告；如执行 PAUSED Canary，必须使用明确隔离对象并核对 lineage。

## 回滚方案

1. 60 秒内关闭 live copy 和 runner live gate，停止新复制；pause/observe 保持。
2. 使用 exact deployer 从目标提交回滚到发布 source commit，恢复完整 runtime manifest。
3. 仅重启 API；若 runner 脚本变化则等待/触发一次只读健康检查。
4. 不删除 intent、lineage、created_data 或历史执行日志；已创建对象按 lineage 精确 PAUSE。

## 注意事项

- 线上是共享 monolith，source commit 必须与当前线上 app 完全一致；发现漂移立即中止。
- 名称更新增加 Meta 写次数，生产配额和熔断不得绕过。
- 不把账号本地计划改成 UTC+8；仅后台统计展示固定 UTC+8。
