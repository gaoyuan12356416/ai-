# 测试报告

## 测试结论

本地功能、合同、页面脚本和 X 全量回归全部通过；GitHub-first 生产部署、无真实发帖
验收和自然 timer 观察也全部通过，可以发布。

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
| 生产部署/健康/账本/timer 验收 | 10 | 10 | 0 | 0 |

## 缺陷情况

- BUG-001：功能根因、生产显示/候选重入和无真实发帖验收均通过，已关闭。
- BUG-002：全量回归发现的旧私有入口兼容问题已修复并关闭。

## 验证证据

- `python scripts/test_x_post_material_pool.py`：11/11。
- `python scripts/test_x_post_multi_schedule_store.py`：91/91。
- selector/schedule/manual/UI/app/error-catalog 专项合计：115/115。
- `python -m unittest discover -s scripts -p "test_x_post*.py"`：502/502，skip=1。
- `python scripts/test_x_accounts.py`：70/70。
- `py_compile`、HTML 内联脚本 `new Function` 语法解析、`git diff --check`：通过。
- 全部发布协作者为 fixture/mock/loopback 测试 handler，未发送真实 X Post。
- 生产 release 内专项回归：素材池 11/11、多排期 store 91/91、主 API 合同 30/30、
  error-catalog 1/1，全部通过。
- 生产服务：sidecar/main API 均 active、零 restart；sidecar health=ok，主 API 匿名权限门
  401，公网页面 hash 与部署文件一致。
- 生产 SQLite 快照：5/5 历史行显示 deferred、5/5 重入候选，目标 queue=0。
- 三个自然 timer 各运行一轮且 exit 0；部署前后 queue/log/unknown 恒为 627/627/0。

## 遗留风险

- 到点只表示时间门禁放行；媒体、语言、账号、Token 或 X 上游仍可能给出新的真实错误。
- 全量回归输出既有 SQLite 测试连接 `ResourceWarning`，退出码为 0；本需求未改该清理路径。
- 生产历史 5 行何时获得新检查时间取决于下一次自然素材排期，不手工 run-now。

## 发布结论

准入、部署和生产验收均通过。当前线上 sidecar/共享逻辑运行提交 `960816e`，主 API
app-only 合同运行提交 `6f8bdf0`；三个自然排期 timer 已恢复。继续保持禁止用真实 Post
做部署测试的边界。
