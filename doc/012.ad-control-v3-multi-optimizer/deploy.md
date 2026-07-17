# 部署文档

## 变更内容

V3 普通用户支持多个强身份优化师别名；规则组按关系表保存，扫描按别名逐一查询并合并。

## 配置项

无新增环境变量。沿用现有 V3 数据库、源数据读库、Meta 熔断和 runner 配置。

## 数据库变更

仅执行 `sql/004_add_rule_group_optimizer_scope.sql` 到 ads_ai 63353。先创建关系表，再回填现有规则；不修改 kunlunads_dev。

## 部署步骤

1. 核对线上 commit/hash 仍为发布基线，停止 V3 runner timer。
2. 在数据盘备份 app、V3 文件、runner、systemd、SQLite、环境配置和数据库结构/计数。
3. 从 GitHub 拉取精确目标 commit 到新的 staging 目录并在服务端跑测试。
4. 执行 SQL 004；校验关联组数等于规则组数。
5. 用精确 overlay 工具合并到共享 monolith，原子替换并重启 `drama-material-api.service`。
6. 使用王鹏当前有效会话调用 meta 和规则列表，仅输出脱敏后的状态/优化师 ID。
7. 恢复 runner timer，观察下一次自然运行；不得手工触发真实暂停/复制。

## 验证步骤

- API 服务 active，V3 页面与 assets 返回 200。
- 王鹏 `/meta` 返回 `[387,686]`，规则列表返回 200。
- ads_ai 关联表结构正确、旧规则无缺失关联。
- 服务日志无 409 ambiguous、SQL 表不存在或 5xx。
- 部署脚本第二次执行返回 unchanged。

## 回滚方案

- 使用数据盘 checkpoint 和精确 overlay 工具恢复到源提交，重启 API。
- 新关联表为加法迁移，已有数据后不删除；旧代码会忽略该表。
- Meta 侧本次验证无写入，不存在需撤销的广告对象。

## 注意事项

- 必须先建表再部署代码。
- 不覆盖整个 app.py，不使用 stash/reset，不删除生产审计数据。
- 若王鹏身份仍非严格 `[387,686]`，立即回滚代码并保留数据库审计表。
