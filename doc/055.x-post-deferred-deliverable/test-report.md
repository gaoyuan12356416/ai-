# 测试报告

## 测试结论

本地功能、合同、页面脚本和 X 全量回归通过；生产无真实发帖验收待 GitHub-first 部署后
回填。

## 测试范围

deferred 三态、历史候选重入、边界前/后门禁、FIFO 证据、原子清错建队列、API/UI、
错误目录、账号 sidecar 和既有 X 发布全链路。

## 执行统计

| 类型 | 数量 | 通过 | 失败 | 阻塞 |
| --- | --- | --- | --- | --- |
| 本需求专项/契约/UI | 217 | 217 | 0 | 0 |
| X 全量命名回归 | 502 | 502 | 0 | 0（条件跳过 1） |
| X 账号/Token/sidecar 回归 | 70 | 70 | 0 | 0 |
| Python/页面脚本语法 | 2 | 2 | 0 | 0 |

## 缺陷情况

- BUG-001：功能根因已修复，本地关闭条件满足；待生产验收最终关闭。
- BUG-002：全量回归发现的旧私有入口兼容问题已修复并关闭。

## 验证证据

- `python scripts/test_x_post_material_pool.py`：11/11。
- `python scripts/test_x_post_multi_schedule_store.py`：91/91。
- selector/schedule/manual/UI/app/error-catalog 专项合计：115/115。
- `python -m unittest discover -s scripts -p "test_x_post*.py"`：502/502，skip=1。
- `python scripts/test_x_accounts.py`：70/70。
- `py_compile`、HTML 内联脚本 `new Function` 语法解析、`git diff --check`：通过。
- 全部发布协作者为 fixture/mock/loopback 测试 handler，未发送真实 X Post。

## 遗留风险

- 到点只表示时间门禁放行；媒体、语言、账号、Token 或 X 上游仍可能给出新的真实错误。
- 全量回归输出既有 SQLite 测试连接 `ResourceWarning`，退出码为 0；本需求未改该清理路径。
- 生产历史 5 行何时获得新检查时间取决于下一次自然素材排期，不手工 run-now。

## 发布建议

本地准入通过。按 GitHub-first、SQLite online backup、双运行时 `service.py` 同步、最小
service restart 和自然 timer 观察流程上线；禁止用真实 Post 做部署测试。
