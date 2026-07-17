# 开发方案

## 查询层

- MySQL 和内存仓库统一在分页前排除已配对的计划预检观察记录。
- `COUNT(*)` 与列表查询使用同一合并条件，保证总数、页码和实际行数一致。
- 正式执行详情按 `preview_id` 回读预检时间，在一个时间线中展示“预检并锁定候选 → 正式执行完成”。

## 查询保护

- UI 默认 `page_size=20`，接口继续限制 `page_size<=100`。
- 执行日志接口限制 `page<=1000`。
- 有起止日期时，包含首尾日期在内最多查询 93 天。
- 新日志请求会中止浏览器中尚未完成的旧请求，服务端结果仍由请求序号防止乱序覆盖。

## 索引

- `ad_control_v3_execution(created_at, execution_id)`：管理员默认时间倒序。
- `ad_control_v3_execution(preview_id, run_mode, trigger_source)`：预检/正式批次配对。
- `ad_control_v3_execution_target(execution_id, action)`：动作筛选。
- `ad_control_v3_execution_target(execution_id, object_id)`：对象 ID 筛选。
- `ad_control_v3_execution_target(execution_id, product_value)`：产品筛选。

索引迁移只增加二级索引，不改数据和外键。
