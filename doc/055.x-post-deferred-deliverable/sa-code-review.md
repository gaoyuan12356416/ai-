# SA 代码评审

## 结论

通过。两个评审发现均已修复并完成全量回归；没有 schema、Token、发布时点或真实 X
写入范围变化。

## 评审范围

- `features/x_posts/service.py` 的三态派生、候选复检、FIFO 与原子清错。
- `app.py` 和素材池静态页的 API/UI 合同。
- 新增/修改测试以及 230 个稳定错误码目录。

## 问题清单

| 编号 | 严重级别 | 文件/位置 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| CR-001 | P0 | `service.py` 事务 FIFO | 仅把错误码加入可复检集合时，内部候选可能在没有当前源时间证据的情况下清错建队列 | 要求被选中的 deferred 行携带本轮 selector 返回的 `drama_deploy_time`，并在事务内确认已到点 | 已关闭 |
| CR-002 | P1 | `service.py` 兼容入口 | 三态重构移除 `_material_validation_is_blocking`，既有回归调用报 `AttributeError` | 保留薄兼容函数并委托三态判断 | 已关闭（BUG-002） |
| CR-003 | P1 | `error-catalog.md` | 手工清单可能漏掉低频内部错误 | 新增 AST 审计测试，逐码检查 8 个发布模块 | 已关闭 |

## 编译 / 验证结果

- `py_compile`：通过。
- 专项/契约/页面脚本检查：通过。
- `python -m unittest discover -s scripts -p "test_x_post*.py"`：502 项通过，
  条件跳过 1 项，失败 0。
- 全量回归仍输出既有 SQLite 测试连接 `ResourceWarning`，不影响退出码；本需求未改该
  测试清理路径。
