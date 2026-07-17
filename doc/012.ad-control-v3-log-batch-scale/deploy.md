# 部署与回滚

## 部署

1. 从精确 Git commit 构建生产 staging，并运行 V3 测试、Python 编译、JS 语法和 deployer 校验。
2. 备份 `/root/drama_material_service` 的 V3 运行文件、运行环境基线和 `ads_ai` schema/index/row-count 基线。
3. 使用 writer 角色执行 `sql/004_add_execution_log_query_indexes.sql`，随后由 reader 回读索引并执行 `EXPLAIN`。
4. 使用 `deploy/apply_ad_control_v3.py` 仅覆盖 V3 运行文件。
5. 重启 `drama-material-api.service`；runner timer 不需要重启，不触发 Meta Canary。
6. 验证动态执行日志页面、分页、筛选、详情以及相同 Preview 的合并显示。

## 回滚

- 代码：使用 deployer 从目标 commit 回退到发布前 commit。
- DDL：索引通常可保留；如必须回退，执行 `sql/904_drop_execution_log_query_indexes.sql`。
- 数据：不删除、不恢复任何执行日志；代码和索引回滚均不改变审计数据。

## 2026-07-17 实际发布记录

- Runtime commit：`a4dad6d2ff708b04a434945b5c18e9f6caf2fdef`。
- 完整检查点：`/mnt/data-disk/ai-ad-control-v3/backups/predeploy-log-scale-20260717T174639+0800-a4dad6d`。
- Exact overlay 检查点：`/mnt/data-disk/ai-ad-control-v3/backups/ad-control-v3-f55be78cf536-to-a4dad6d2ff70`。
- 调度器仅在DDL和overlay窗口暂停，发布后恢复；未启用规则组，未产生Meta写入。
