# 测试报告

## 测试结论

本地针对性验证通过；Linux 及生产验收结果在部署后补录。本文件不提前宣称上线。

## 测试范围

Page/素材锁、历史 schema 迁移、同族/跨族串行、晚回执、过期租约、known-ID GET、Excel/素材池入口，以及统一调度既有 X/TT/YouTube 行为。

## 执行统计

| 类型 | 数量 | 通过 | 失败 | 阻塞 |
| --- | --- | --- | --- | --- |
| 原生隔离与入口反例 | 23 | 23 | 0 | 0 |
| 统一层定向与既有回归 | 105 | 105 | 0 | 0 |

## 缺陷情况

修复三个问题记录 BUG-001/002/003；QA 反例与修复闭环见 sa-code-review.md。

## 验证证据

- `python -m unittest tests.test_fb_page_task_isolation`
- dispatch suite 包含 `scripts.test_pipeline_fb_task_isolation`、source_locks、adapters、core、coordinator、legacy_convergence、native_imports、youtube_resources。
- 所改 Python 编译与 `git diff --check` 通过。
- 两条既有目标任务的平台列表逐条只有 1 个匹配视频；原始 task、短链、快照保留，native 与 dispatch 均 published。运营明细保留在私有维护目录。

## 遗留风险

身份缺失的历史未知记录无法安全缩小防重范围，仍需核对。明确失败不会自动新建重试；后续不同素材继续。原 Excel 跳过行的配置未持久化，由用户重传完整文件。

## 发布建议

最终独立评审、Linux候选迁移/测试与线上健康检查全通过后方可记录已部署。
