# API

GET /api/social-analytics/report 与 /export.csv 的参数不变。Cookie、模块权限和服务端 actor 过滤不变。

网页 YouTube/X 新增内部优化：按可见目录候选 ID 与筛选拆分查询；不接受客户端 campaign ID 或系统权限参数。meta.indexed_effect_sources 标明采用索引读取的来源，quality.reconciliation 为 selected_visible_candidates_only；金额及行内容不变。

范围缺失与 SQL 错误均保留原质量标记。数据读取失败不按零统计。
