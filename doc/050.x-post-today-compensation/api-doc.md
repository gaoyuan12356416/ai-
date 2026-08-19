# API 文档

## 接口列表

内部运维 CLI：`scripts/x_post_schedule_drama_scope_compensate.py`，不新增 HTTP 接口。

## 请求/响应

必填：`--original-run-id`、`--actor`、`--deployed-commit`、`--compensation-publish-time`。应用模式还必须提供 `/mnt/data-disk/x-post-automation/recoveries/...` 下的 `--report-path`；`--validate-only` 为零写模式。输出单行 JSON。

## 错误码

- `x_post_drama_scope_compensation_not_allowed`：日期、错误类型、commit 或时刻不符合。
- `x_post_drama_scope_compensation_conflict`：run、计划、配置、库存、账本或并发状态漂移。
- `x_post_drama_scope_compensation_report_required`：应用模式缺少独立报告路径。

## 兼容性说明

仅新增表和方法；既有 HTTP、队列、发布与归因合同不变。
素材检查复用既有 `/internal/posts/material-pool/check`，新增安全错误码
`material_language_not_scheduled`，不新增字段或路由。
